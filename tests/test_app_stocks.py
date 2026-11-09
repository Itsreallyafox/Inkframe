"""The Stocks app: Yahoo chart parsing, the parallel fetch, the cache, the stamps,
the status line and every layout.

All tests are offline.  ``tests/fixtures/stocks`` holds trimmed real Yahoo
Finance chart responses recorded on 2026-11-09 at 14:42 UTC (AAPL and ^GSPC
over one month from ``query1``, ^DJI over five days from ``query2``, the 404
body for an unknown symbol) and ``quotes-sample.json``, a twelve-quote cache
derived from them.  ``stocks_data.transport`` is replaced with an
``httpx.MockTransport`` whenever a test wants the "network" path, so the
exact requests (host, path, headers) are visible; an autouse fixture makes
the real transport fail loudly.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import numpy as np
import pytest

from inkframe import USER_AGENT
from inkframe.apps import APP_IDS, load_app
from inkframe.apps import stocks as S
from inkframe.apps import stocks_data as SD
from inkframe.apps import stocks_layouts as L
from inkframe.apps.base import (
    App,
    FontLibrary,
    RenderContext,
    render_to_frame,
    schema_defaults,
    validate_settings,
)
from inkframe.display.palette import BLACK, BLUE, GREEN, N_COLORS, RED, WHITE

FIX = Path(__file__).parent / "fixtures" / "stocks"
FONTS = FontLibrary()  # shared: fonts are cached per size, which keeps the suite quick
EST = timezone(timedelta(hours=-5))
#: Monday 9 November 2026, 9:45 EST: the regular session is open (the probes are from 9:42)
NOW = datetime(2026, 11, 9, 9, 45, tzinfo=EST)
NOW_UTC = NOW.astimezone(timezone.utc)
#: the time of the last price in the AAPL and ^GSPC fixtures (``regularMarketTime``)
AS_OF = datetime.fromtimestamp(1794235363, timezone.utc)
SAMPLE: dict[str, Any] = json.loads((FIX / "quotes-sample.json").read_text(encoding="utf-8"))
ALL: list[str] = SAMPLE["symbols"]
DEFAULTS = S.DEFAULT_SYMBOLS
Q1, Q2 = SD.HOSTS


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Boom(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("a test tried to use the real network")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SD, "transport", Boom())


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replaces the pause before a retry: no test sleeps, and a test that asks for
    this fixture sees the delays the app wanted (from any worker thread)."""
    delays: list[float] = []
    lock = threading.Lock()

    def pause(seconds: float) -> None:
        with lock:
            delays.append(seconds)

    monkeypatch.setattr(SD, "_sleep", pause)
    return delays


def body_for(symbol: str) -> dict[str, Any]:
    """A real response for ``symbol``: the recorded one for AAPL, ^GSPC and ^DJI,
    else the AAPL response renamed."""
    name = {"^GSPC": "chart_gspc_1mo.json", "^DJI": "chart_dji_5d_query2.json"}.get(
        symbol, "chart_aapl_1mo.json"
    )
    body = fixture(name)
    body["chart"]["result"][0]["meta"]["symbol"] = symbol
    return body


class FakeYahoo:
    """An ``httpx.MockTransport`` handler: serves a fixture body per symbol and
    records every request.  ``errors[symbol]`` is consumed first, one per call:
    an HTTP status (an int, with the recorded 404 body for 404 and an HTML
    page otherwise), a ready ``httpx.Response`` or an exception to raise."""

    def __init__(self, errors: dict[str, list[Any]] | None = None) -> None:
        self.errors = {k: list(v) for k, v in (errors or {}).items()}
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            symbol = unquote(request.url.path.rsplit("/", 1)[1])
            queue = self.errors.get(symbol)
            item = queue.pop(0) if queue else None
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, int):
            if item == 404:
                return httpx.Response(404, json=fixture("chart_notfound_404.json"))
            return httpx.Response(item, text="<html>service unavailable</html>")
        return httpx.Response(200, json=body_for(symbol))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def hosts(self, symbol: str) -> list[str]:
        return [r.url.host for r in self.requests if unquote(r.url.path).endswith("/" + symbol)]

    def symbols(self) -> list[str]:
        return [unquote(r.url.path.rsplit("/", 1)[1]) for r in self.requests]


def net(monkeypatch: pytest.MonkeyPatch, errors: dict[str, list[Any]] | None = None) -> FakeYahoo:
    fake = FakeYahoo(errors)
    monkeypatch.setattr(SD, "transport", fake.transport())
    return fake


def ctx(
    tmp_path: Path,
    *,
    symbols: list[str] | None = None,
    labels: list[str] | None = None,
    now: datetime = NOW,
    online: bool = True,
    variant: int | None = None,
    **settings: Any,
) -> RenderContext:
    data = {
        "symbols": list(DEFAULTS if symbols is None else symbols),
        "labels": list(S.DEFAULT_LABELS if labels is None else labels),
        **settings,
    }
    return RenderContext(
        settings=validate_settings(S.SETTINGS_SCHEMA, data),
        now=now,
        cache_dir=tmp_path,
        fonts=FONTS,
        online=online,
        variant=variant,
    )


def prime(
    cache_dir: Path,
    symbols: list[str],
    *,
    fetched_at: datetime = NOW_UTC,
    drop: set[str] = frozenset(),
    yrange: str = "1mo",
    as_of: datetime | None = None,
    session: str | None = None,
) -> None:
    """Put the sample quotes for ``symbols`` in the cache, as a fetch would."""
    payload = copy.deepcopy(SAMPLE)
    payload["fetched_at"] = fetched_at.astimezone(timezone.utc).isoformat()
    payload["symbols"] = list(symbols)
    payload["range"] = yrange
    quotes = [q for q in payload["quotes"] if q["symbol"] in symbols and q["symbol"] not in drop]
    for q in quotes:
        if as_of is not None:
            q["as_of"] = as_of.astimezone(timezone.utc).isoformat()
        if session is not None:
            q["session"] = session
    payload["quotes"] = quotes
    (cache_dir / SD.CACHE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def frame_of(c: RenderContext) -> np.ndarray:
    frame = render_to_frame(S.APP, c)
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert int(frame.max()) < N_COLORS
    return frame


def colours(frame: np.ndarray) -> set[int]:
    return {int(v) for v in np.unique(frame)}


def board_of(*quotes: SD.Quote, stale: bool = False, online: bool = True) -> SD.Board:
    return SD.Board(
        [SD.Item(q.symbol, q.label, q) for q in quotes],
        fetched_at=NOW_UTC,
        stale=stale,
        frame_online=online,
    )


def quote(**kw: Any) -> SD.Quote:
    base = dict(
        symbol="AAPL",
        label="AAPL",
        price=349.71,
        change=7.34,
        change_pct=2.144,
        currency="USD",
        as_of=AS_OF,
        session="open",
        closes=(336.64, 340.97, 342.37, 349.71),
        prev_close=342.37,
    )
    base.update(kw)
    return SD.Quote(**base)


# --- contract and settings -----------------------------------------------------------------


def test_app_satisfies_the_contract() -> None:
    app = load_app("stocks")
    assert app is S.APP and isinstance(app, App)
    assert app.id == "stocks" and app.name == "Stocks" and app.needs_dither is False
    assert app.variants == 3 and len(L.LAYOUTS) == 3
    assert "sparkline" in app.description
    assert APP_IDS.index("stocks") == APP_IDS.index("trivia") + 1
    assert callable(app.validate)


def test_settings_schema_defaults() -> None:
    assert schema_defaults(S.SETTINGS_SCHEMA) == {
        "symbols": ["AAPL", "NVDA", "GOOG", "^GSPC", "^IXIC", "^DJI", "T", "F", "META", "AMD"],
        "labels": ["^GSPC=S&P 500", "^IXIC=Nasdaq", "^DJI=Dow"],
        "range": "1mo",
        "show_percent": True,
        "layout": "rotate",
    }
    S.validate(schema_defaults(S.SETTINGS_SCHEMA))
    # the schema itself refuses an empty symbol list (the field is required)
    with pytest.raises(ValueError, match="symbols: at least one entry"):
        validate_settings(S.SETTINGS_SCHEMA, {"symbols": []})


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"symbols": [f"S{i}" for i in range(13)]}, "symbols: at most 12 symbols, got 13"),
        ({"symbols": ["AAPL!"]}, "symbols: entry 1 'AAPL!' must be 1-12 characters from"),
        ({"symbols": ["AAPL", "ABCDEFGHIJKLM"]}, "symbols: entry 2 'ABCDEFGHIJKLM' must be 1-12"),
        ({"symbols": ["aapl", " AAPL "]}, "symbols: AAPL is listed twice"),
        ({"labels": ["S&P 500"]}, "labels: entry 1 'S&P 500' must be SYMBOL=Name"),
        ({"labels": ["^GSPC=S&P 500", "=Dow"]}, "labels: entry 2 '=Dow' must be SYMBOL=Name"),
        ({"labels": ["^GSPC="]}, "labels: entry 1 '^GSPC=' must be SYMBOL=Name"),
        ({"labels": ["AA PL=Apple"]}, "labels: entry 1 'AA PL=Apple' has a symbol that is not"),
    ],
)
def test_every_validation_message_names_the_field(patch: dict[str, Any], message: str) -> None:
    settings = validate_settings(S.SETTINGS_SCHEMA, patch)
    with pytest.raises(ValueError) as e:
        S.validate(settings)
    assert str(e.value).startswith(message)


def test_validate_accepts_what_yahoo_knows() -> None:
    S.validate(
        {
            "symbols": ["aapl", "^GSPC", "BTC-USD", "BRK-B", "7203.T", "EURUSD=X", "  nvda "],
            "labels": ["^gspc = S&P 500", "BTC-USD=Bitcoin (USD)"],
        }
    )
    S.validate({})  # nothing to check


def test_clean_symbols_and_labels() -> None:
    assert SD.clean_symbols([" aapl ", "AAPL", "bad!", "", "^gspc"]) == ["AAPL", "^GSPC"]
    assert len(SD.clean_symbols([f"S{i}" for i in range(20)])) == 12
    assert SD.parse_labels(["^GSPC=S&P 500", "junk", "=x", "t= AT&T  Inc "]) == {
        "^GSPC": "S&P 500",
        "T": "AT&T Inc",
    }
    assert SD.parse_label("A=B=C") == ("A", "B=C")


# --- parsing -------------------------------------------------------------------------------


def test_parse_aapl_from_the_real_response() -> None:
    q = SD.parse_chart(fixture("chart_aapl_1mo.json"), "AAPL", "Apple", NOW_UTC)
    assert q.symbol == "AAPL" and q.label == "Apple" and q.currency == "USD"
    assert q.price == 349.71
    assert q.as_of == AS_OF and q.as_of.tzinfo is not None
    assert q.session == "open"  # fetched at 9:45 EST, inside the regular period
    assert len(q.closes) == 22 and q.closes[0] == pytest.approx(336.64, abs=0.01)
    assert q.closes[-1] == pytest.approx(349.71, abs=0.01)
    # the day change is against the previous session's close (the bar before
    # today's), which matches Yahoo's own regularMarketChangePercent of 2.144 %;
    # chartPreviousClose (336.64) is the close before the whole month
    assert q.prev_close == pytest.approx(342.37, abs=0.01)
    assert q.change == pytest.approx(7.34, abs=0.01)
    assert q.change_pct == pytest.approx(2.144, abs=0.01)
    assert q.direction == 1


def test_parse_an_index_and_the_query2_five_day_shape() -> None:
    g = SD.parse_chart(fixture("chart_gspc_1mo.json"), "^GSPC", "S&P 500", NOW_UTC)
    assert g.symbol == "^GSPC" and g.price == 7481.56
    assert g.change == pytest.approx(104.70, abs=0.01)  # Yahoo's fulldayChange is 104.7
    assert g.change_pct == pytest.approx(1.419, abs=0.01)
    assert len(g.closes) == 22
    d = SD.parse_chart(fixture("chart_dji_5d_query2.json"), "^DJI", "Dow", NOW_UTC)
    assert d.price == 51995.19 and len(d.closes) == 5
    assert d.change == pytest.approx(709.46, abs=0.01)
    assert d.change_pct == pytest.approx(1.383, abs=0.01)


def test_parse_drops_null_closes_and_keeps_the_baseline() -> None:
    body = fixture("chart_aapl_1mo.json")
    closes = body["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    closes[3] = None
    closes[-1] = None  # today's bar not yet written, the live price still in meta
    q = SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC)
    assert len(q.closes) == 20
    assert q.prev_close == pytest.approx(342.37, abs=0.01)
    assert q.change == pytest.approx(7.34, abs=0.01)


def test_parse_session_and_weekend_shapes() -> None:
    body = fixture("chart_aapl_1mo.json")
    # fetched after the close: the session is closed, the change unchanged
    evening = datetime(2026, 11, 9, 19, 0, tzinfo=EST)
    q = SD.parse_chart(body, "AAPL", "AAPL", evening)
    assert q.session == "closed" and q.change == pytest.approx(7.34, abs=0.01)
    # a weekend shape where Yahoo already announces Monday's period: the last
    # price is from before it, so the previous session is the bar before the last
    body = fixture("chart_aapl_1mo.json")
    r = body["chart"]["result"][0]
    day = 86400
    r["meta"]["currentTradingPeriod"]["regular"]["start"] += day
    r["meta"]["currentTradingPeriod"]["regular"]["end"] += day
    q = SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC)
    assert q.session == "closed"
    assert q.prev_close == pytest.approx(342.37, abs=0.01)
    assert q.change == pytest.approx(7.34, abs=0.01)
    # no period at all: fulldayChange, then chartPreviousClose
    body = fixture("chart_aapl_1mo.json")
    meta = body["chart"]["result"][0]["meta"]
    del meta["currentTradingPeriod"]
    assert SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC).change == pytest.approx(7.34, abs=0.01)
    del meta["fulldayChange"]
    q = SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC)
    assert q.prev_close == pytest.approx(336.64, abs=0.01)
    del meta["chartPreviousClose"]
    q = SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC)
    assert q.change is None and q.change_pct is None and q.direction == 0
    assert SD.fmt_change(q) == SD.DASH


def test_parse_the_404_error_body_and_junk() -> None:
    with pytest.raises(ValueError, match="No data found, symbol may be delisted"):
        SD.parse_chart(fixture("chart_notfound_404.json"), "NOPE", "NOPE", NOW_UTC)
    for junk in (None, "x", {}, {"chart": None}, {"chart": {"result": None, "error": None}}):
        with pytest.raises(ValueError):
            SD.parse_chart(junk, "X", "X", NOW_UTC)
    with pytest.raises(ValueError, match="no result"):
        SD.parse_chart({"chart": {"result": [], "error": None}}, "X", "X", NOW_UTC)
    with pytest.raises(ValueError, match="no price"):
        SD.parse_chart({"chart": {"result": [{"meta": {}}]}}, "X", "X", NOW_UTC)
    # no series at all still gives a quote (flat sparkline from the price)
    body = fixture("chart_aapl_1mo.json")
    del body["chart"]["result"][0]["indicators"]
    del body["chart"]["result"][0]["timestamp"]
    q = SD.parse_chart(body, "AAPL", "AAPL", NOW_UTC)
    assert q.closes == () and q.change == pytest.approx(7.34, abs=0.01)


def test_quote_json_round_trip_and_cache_files(tmp_path: Path) -> None:
    q = SD.parse_chart(fixture("chart_gspc_1mo.json"), "^GSPC", "S&P 500", NOW_UTC)
    assert SD.Quote.from_json(json.loads(json.dumps(q.to_json()))) == q
    SD.write_cache(tmp_path, NOW_UTC, ["^GSPC", "AAPL"], "1mo", [q])
    cached = SD.read_cache(tmp_path)
    assert cached is not None
    assert cached.fetched_at == NOW_UTC and cached.symbols == ["^GSPC", "AAPL"]
    assert cached.range == "1mo" and cached.quotes == {"^GSPC": q}
    (tmp_path / SD.CACHE_NAME).write_text("{not json", encoding="utf-8")
    assert SD.read_cache(tmp_path) is None
    (tmp_path / SD.CACHE_NAME).write_text(json.dumps({"fetched_at": 5}), encoding="utf-8")
    assert SD.read_cache(tmp_path) is None
    assert SD.read_cache(tmp_path / "missing") is None


# --- formatting and the status line -------------------------------------------------------


def test_formatting() -> None:
    assert SD.fmt_price(349.71) == "349.71"
    assert SD.fmt_price(7481.56) == "7,481.56"
    assert SD.fmt_price(9999.994) == "9,999.99"
    assert SD.fmt_price(51995.19) == "51,995"
    assert SD.fmt_price(119665.3) == "119,665"
    assert SD.fmt_price(11.5) == "11.50"
    assert SD.fmt_price(-3.5) == "−3.50"
    assert SD.fmt_price(None) == SD.DASH
    up = quote(change=7.34, change_pct=2.144)
    down = quote(change=-3.96, change_pct=-1.291)
    flat = quote(change=0.0, change_pct=0.0)
    assert SD.fmt_change(up) == "+2.14 %"
    assert SD.fmt_change(down) == "−1.29 %"  # a real minus sign, never a hyphen
    assert SD.fmt_change(flat) == "0.00 %"
    assert SD.fmt_change(up, percent=False) == "+7.34"
    assert SD.fmt_change(down, percent=False) == "−3.96"
    assert SD.fmt_change(quote(change=709.461, change_pct=1.383), percent=False) == "+709.46"
    assert SD.fmt_change(quote(change=3463.75, change_pct=2.98), percent=False) == "+3,463.75"
    assert SD.fmt_change(None) == SD.DASH
    assert "-" not in SD.fmt_change(down) + SD.fmt_change(down, percent=False)
    assert L.change_colour(up) == GREEN and L.change_colour(down) == RED
    assert L.change_colour(flat) == BLACK and L.change_colour(None) == BLACK
    assert SD.fmt_clock(datetime(2026, 11, 9, 15, 42)) == "3:42 PM"
    assert SD.fmt_clock(datetime(2026, 11, 9, 0, 5)) == "12:05 AM"
    assert SD.fmt_clock(datetime(2026, 11, 9, 12, 0)) == "12:00 PM"


def test_status_line_open_saturday_and_evening() -> None:
    # the session is open and the data fresh: LIVE with the time of the last price
    assert SD.status_line(board_of(quote()), NOW) == "LIVE · 9:42 AM"
    # a Saturday render: the last price is Friday's close
    friday_close = datetime(2026, 11, 13, 16, 0, 2, tzinfo=EST)
    saturday = datetime(2026, 11, 14, 9, 30, tzinfo=EST)
    b = board_of(quote(as_of=friday_close, session="closed"))
    assert SD.status_line(b, saturday) == "FRI CLOSE · 4:00 PM"
    # a weekday evening: today's close, the session over
    evening = datetime(2026, 11, 9, 19, 30, tzinfo=EST)
    b = board_of(quote(as_of=datetime(2026, 11, 9, 16, 0, 1, tzinfo=EST), session="closed"))
    assert SD.status_line(b, evening) == "AS OF 4:00 PM"
    # a stale cache from an open session is not "live" any more
    assert SD.status_line(board_of(quote(), stale=True), NOW) == "AS OF 9:42 AM"
    # the first quote *with data* speaks; none at all is silent
    b = SD.Board([SD.Item("X", "X", None), SD.Item("AAPL", "AAPL", quote())], fetched_at=NOW_UTC)
    assert SD.status_line(b, NOW) == "LIVE · 9:42 AM"


def test_live_ends_at_the_bell() -> None:
    """A fresh cache fetched while the session was open is not "LIVE" once the
    regular session has ended (the card may be drawn from the cache for up to
    15 minutes after the close); an older cache without the end still trusts
    the session flag."""
    bell = datetime(2026, 11, 9, 16, 0, tzinfo=EST)
    last = datetime(2026, 11, 9, 15, 55, tzinfo=EST)
    q = quote(as_of=last, session="open", session_end=bell.astimezone(timezone.utc))
    assert SD.status_line(board_of(q), bell - timedelta(minutes=5)) == "LIVE · 3:55 PM"
    assert SD.status_line(board_of(q), bell) == "AS OF 3:55 PM"
    assert SD.status_line(board_of(q), bell + timedelta(minutes=5)) == "AS OF 3:55 PM"
    legacy = quote(as_of=last, session="open", session_end=None)
    assert SD.status_line(board_of(legacy), bell + timedelta(minutes=5)) == "LIVE · 3:55 PM"
    # the end survives the cache file, and the parser takes it from the regular period
    again = SD.Quote.from_json(q.to_json())
    assert again.session_end == bell
    assert SD.Quote.from_json(legacy.to_json()).session_end is None
    parsed = SD.parse_chart(fixture("chart_aapl_1mo.json"), "AAPL", "Apple", NOW_UTC)
    assert parsed.session_end == datetime.fromtimestamp(1794258000, timezone.utc) == bell
    assert SD.status_line(SD.Board([SD.Item("X", "X", None)]), NOW) == ""
    # the frame's zone decides the day: 9:42 EST on Monday is 15:42 in Berlin
    berlin = NOW.astimezone(timezone(timedelta(hours=1)))
    assert SD.status_line(board_of(quote()), berlin) == "LIVE · 3:42 PM"


def test_stamp_wording_matches_weather() -> None:
    fresh = board_of(quote())
    assert SD.stamp_line(fresh, NOW) == ("UPDATED 9:45 AM", BLACK)
    assert SD.stamp_line(board_of(quote(), stale=True), NOW) == (
        "COULDN'T UPDATE, DATA FROM 9:45 AM",
        RED,
    )
    assert SD.stamp_line(board_of(quote(), stale=True, online=False), NOW) == (
        "OFFLINE, DATA FROM 9:45 AM",
        RED,
    )
    old = board_of(quote(), stale=True)
    old.fetched_at = datetime(2026, 11, 6, 16, 12, tzinfo=EST)
    assert SD.stamp_line(old, NOW) == ("COULDN'T UPDATE, DATA FROM FRI 4:12 PM", RED)
    assert SD.stamp_line(SD.Board(), NOW) == ("", BLACK)


# --- fetching ------------------------------------------------------------------------------


def test_fetch_every_symbol_with_our_user_agent(tmp_path: Path, sleeps: list[float]) -> None:
    fake = net(pytest.MonkeyPatch())
    monkey = pytest.MonkeyPatch()
    monkey.setattr(SD, "transport", fake.transport())
    try:
        board = SD.load(
            DEFAULTS, {"^GSPC": "S&P 500"}, "1mo", now=NOW, cache_dir=tmp_path, online=True
        )
    finally:
        monkey.undo()
    assert board.state == "ok" and not board.stale and board.fetched_at == NOW_UTC
    assert [i.symbol for i in board.items] == DEFAULTS
    assert all(i.quote is not None for i in board.items)
    assert board.items[3].label == "S&P 500" and board.items[3].quote.label == "S&P 500"
    assert board.items[0].label == "AAPL"
    # one request per symbol, on the first host, with our headers and Yahoo's parameters
    assert sorted(fake.symbols()) == sorted(DEFAULTS) and len(fake.requests) == len(DEFAULTS)
    for r in fake.requests:
        assert r.url.host == Q1 and r.url.scheme == "https"
        assert r.url.path.startswith("/v8/finance/chart/")
        assert r.headers["user-agent"] == USER_AGENT and "python-httpx" not in USER_AGENT
        assert r.headers["accept"] == "application/json"
        assert r.url.params["range"] == "1mo" and r.url.params["interval"] == "1d"
    assert any(r.url.raw_path.endswith(b"/%5EGSPC?range=1mo&interval=1d") for r in fake.requests)
    assert sleeps == []
    cached = SD.read_cache(tmp_path)
    assert cached is not None and set(cached.quotes) == set(DEFAULTS)
    assert cached.symbols == DEFAULTS and cached.range == "1mo"


def test_range_setting_maps_to_yahoo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = net(monkeypatch)
    SD.load(["AAPL"], {}, "1w", now=NOW, cache_dir=tmp_path, online=True)
    SD.load(["AAPL"], {}, "3mo", now=NOW, cache_dir=tmp_path, online=True)
    assert [r.url.params["range"] for r in fake.requests] == ["5d", "3mo"]


def test_transient_failures_retry_once_on_query2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = net(
        monkeypatch,
        {
            "NVDA": [500],
            "GOOG": [429],
            "T": [httpx.ConnectError("no route")],
            "F": [httpx.Response(200, text="<html>not json</html>")],
            "META": [404],  # the provider's answer: never retried
            "AMD": [httpx.Response(403, text="<html>forbidden</html>")],  # a 4xx: never retried
        },
    )
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert board.state == "ok" and not board.stale
    for s in ("NVDA", "GOOG", "T", "F"):
        assert fake.hosts(s) == [Q1, Q2], s
    assert fake.hosts("META") == [Q1] and fake.hosts("AMD") == [Q1]
    assert fake.hosts("AAPL") == [Q1]
    assert sleeps == [SD.RETRY_DELAY] * 4
    got = {i.symbol: i.quote for i in board.items}
    assert got["NVDA"] is not None and got["GOOG"] is not None
    assert got["T"] is not None and got["F"] is not None
    assert got["META"] is None and got["AMD"] is None  # the dash and "no data"
    assert "META" in board.detail or "AMD" in board.detail
    # the cache holds only what came through; the failed ones stay "no data" until
    # the next refresh
    cached = SD.read_cache(tmp_path)
    assert cached is not None and "META" not in cached.quotes and "AAPL" in cached.quotes
    assert cached.symbols == DEFAULTS


def test_never_more_than_one_request_per_symbol_per_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = net(monkeypatch, {"AAPL": [503, 503], "NVDA": [503, 429]})
    board = SD.load(["AAPL", "NVDA"], {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert fake.hosts("AAPL") == [Q1, Q2] and fake.hosts("NVDA") == [Q1, Q2]
    assert sleeps == [SD.RETRY_DELAY] * 2
    assert board.state == "unreachable" and board.detail


def test_no_retry_when_the_budget_has_no_room(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fake = net(monkeypatch, {"AAPL": [503, 503]})  # one per fetch_quote call below
    clock = [1000.0]
    monkeypatch.setattr(SD, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(SD.FetchError):
        SD.fetch_quote(
            "AAPL",
            "AAPL",
            "1mo",
            fetched_at=NOW_UTC,
            deadline=clock[0] + SD.RETRY_DELAY + 1.0,
            http_timeout=10.0,
        )
    assert fake.hosts("AAPL") == [Q1] and sleeps == []
    # with room, the retry happens and the per-request timeout follows the budget
    q = SD.fetch_quote(
        "AAPL", "AAPL", "1mo", fetched_at=NOW_UTC, deadline=clock[0] + 6.0, http_timeout=10.0
    )
    assert q.price == 349.71 and sleeps == [SD.RETRY_DELAY]
    assert fake.hosts("AAPL") == [Q1, Q1, Q2]  # the first call above, then this one and its retry
    with pytest.raises(SD.FetchError, match="out of time"):
        SD._budget(clock[0] + 0.2, 10.0)
    assert SD._budget(clock[0] + 3.0, 10.0) == 3.0 and SD._budget(clock[0] + 30.0, 10.0) == 10.0


def test_fetch_json_maps_every_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def transient(item: Any) -> bool:
        net(monkeypatch, {"X": [item]})
        try:
            SD.fetch_json(Q1, "X", {"range": "1mo", "interval": "1d"}, 4.0)
        except SD.FetchError as e:
            return e.transient
        raise AssertionError("expected a FetchError")

    assert transient(503) is True
    assert transient(429) is True
    assert transient(httpx.ReadTimeout("slow")) is True
    assert transient(httpx.ConnectError("refused")) is True
    assert transient(httpx.Response(200, text="<html>challenge</html>")) is True
    assert transient(httpx.Response(403, text="<html>forbidden</html>")) is False
    assert transient(httpx.TooManyRedirects("loop")) is False
    # a 4xx with a JSON body comes back for parse_chart to report
    net(monkeypatch, {"X": [404]})
    body = SD.fetch_json(Q1, "X", {}, 4.0)
    assert body["chart"]["error"]["code"] == "Not Found"
    assert SD.chart_url(Q2, "^GSPC") == "https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC"


def test_symbols_are_fetched_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four symbols, four workers: every request must be in flight at once for the
    barrier to open; a sequential fetch would break it and fail every symbol."""
    barrier = threading.Barrier(4, timeout=5.0)

    class Gate(FakeYahoo):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            barrier.wait()
            return super().__call__(request)

    monkeypatch.setattr(SD, "transport", httpx.MockTransport(Gate()))
    board = SD.load(
        ["AAPL", "NVDA", "GOOG", "META"], {}, "1mo", now=NOW, cache_dir=tmp_path, online=True
    )
    assert board.state == "ok" and all(i.quote is not None for i in board.items)


def test_the_budget_caps_a_slow_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        release.wait(5.0)
        return httpx.Response(200, json=body_for("AAPL"))

    monkeypatch.setattr(SD, "transport", httpx.MockTransport(slow))
    t0 = time.monotonic()
    try:
        results = SD.fetch_all(["AAPL", "NVDA"], {}, "1mo", fetched_at=NOW_UTC, budget=0.6)
    finally:
        release.set()
    assert time.monotonic() - t0 < 3.0
    assert set(results) == {"AAPL", "NVDA"}
    assert all(isinstance(v, TimeoutError) for v in results.values())
    assert SD.fetch_all([], {}, "1mo", fetched_at=NOW_UTC) == {}


# --- the cache window and the fallbacks -------------------------------------------------------


def test_cache_for_fifteen_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = net(monkeypatch)
    frame_of(ctx(tmp_path, variant=0))
    assert len(fake.requests) == 10
    frame_of(ctx(tmp_path, variant=1, now=NOW + timedelta(minutes=10)))
    assert len(fake.requests) == 10  # served from quotes.json
    frame_of(ctx(tmp_path, variant=2, now=NOW + timedelta(minutes=16)))
    assert len(fake.requests) == 20
    # a clock that stepped back more than five minutes does not trust the cache
    frame_of(ctx(tmp_path, variant=0, now=NOW - timedelta(minutes=10)))
    assert len(fake.requests) == 30


def test_cache_is_keyed_by_symbols_and_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = net(monkeypatch)
    SD.load(["AAPL", "NVDA"], {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert len(fake.requests) == 2
    SD.load(["AAPL", "NVDA"], {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert len(fake.requests) == 2
    SD.load(["AAPL", "NVDA", "GOOG"], {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert len(fake.requests) == 5
    SD.load(["AAPL", "NVDA", "GOOG"], {}, "3mo", now=NOW, cache_dir=tmp_path, online=True)
    assert len(fake.requests) == 8
    # a label change never costs a request
    labels = {"AAPL": "Apple"}
    b = SD.load(["AAPL", "NVDA", "GOOG"], labels, "3mo", now=NOW, cache_dir=tmp_path, online=True)
    assert len(fake.requests) == 8 and b.items[0].label == "Apple"
    assert b.items[0].quote is not None and b.items[0].quote.label == "Apple"


def test_a_failed_refresh_shows_the_cache_with_the_red_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    fetched = NOW_UTC - timedelta(hours=3)
    prime(tmp_path, DEFAULTS, fetched_at=fetched)
    fake = net(monkeypatch, {s: [503] * 4 for s in DEFAULTS})  # two fetches of two attempts
    c = ctx(tmp_path, variant=0)
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert board.state == "ok" and board.stale and board.frame_online
    assert board.fetched_at == fetched and len(board.quotes) == 10
    assert SD.stamp_line(board, NOW) == ("COULDN'T UPDATE, DATA FROM 6:45 AM", RED)
    assert len(fake.requests) == 20 and sleeps == [SD.RETRY_DELAY] * 10
    frame = frame_of(c)
    assert RED in colours(frame)
    # the cache file is left alone (nothing new to write)
    assert SD.read_cache(tmp_path).fetched_at == fetched


def test_offline_mode_uses_the_cache_without_a_request(tmp_path: Path) -> None:
    prime(tmp_path, DEFAULTS, fetched_at=NOW_UTC - timedelta(days=1))
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=False)
    assert board.stale and not board.frame_online and len(board.quotes) == 10
    assert SD.stamp_line(board, NOW) == ("OFFLINE, DATA FROM SUN 9:45 AM", RED)
    assert RED in colours(frame_of(ctx(tmp_path, online=False, variant=1)))
    # a fresh cache in offline mode is simply fresh
    prime(tmp_path, DEFAULTS)
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=False)
    assert not board.stale and SD.stamp_line(board, NOW) == ("UPDATED 9:45 AM", BLACK)


def test_unreachable_card_online_and_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net(monkeypatch, {s: [503] * 4 for s in DEFAULTS})  # two fetches of two attempts
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert board.state == "unreachable" and board.detail
    frame = frame_of(ctx(tmp_path, variant=0))
    assert RED in colours(frame) and GREEN not in colours(frame)
    monkeypatch.setattr(SD, "transport", Boom())
    frame = frame_of(ctx(tmp_path, variant=2, online=False))
    assert RED in colours(frame)
    # an empty cache file is no cache
    SD.write_cache(tmp_path, NOW_UTC, DEFAULTS, "1mo", [])
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=False)
    assert board.state == "unreachable"


def test_a_symbol_that_fails_alone_renders_as_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net(monkeypatch, {"GOOG": [404], "^DJI": [404]})
    for v in range(3):
        frame = frame_of(ctx(tmp_path, variant=v))
        assert colours(frame) <= set(range(N_COLORS)) and GREEN in colours(frame)
    board = SD.load(DEFAULTS, {}, "1mo", now=NOW, cache_dir=tmp_path, online=True)
    assert [i.symbol for i in board.items if i.quote is None] == ["GOOG", "^DJI"]


def test_empty_symbol_list_is_a_card(tmp_path: Path) -> None:
    c = ctx(tmp_path, online=False)
    c.settings["symbols"] = []  # bypassing the schema, as a hand-edited row could
    frame = frame_of(c)
    assert BLACK in colours(frame) and GREEN not in colours(frame)
    assert SD.load([], {}, "1mo", now=NOW, cache_dir=tmp_path, online=False).state == "empty"


# --- layouts -----------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [0, 1, 2])
@pytest.mark.parametrize("count", [12, 10, 3, 1])
def test_every_layout_renders_from_the_fixture_data(
    tmp_path: Path, variant: int, count: int
) -> None:
    symbols = ALL[:count]
    prime(tmp_path, symbols)
    labels = S.DEFAULT_LABELS + ["BTC-USD=Bitcoin"]
    frame = frame_of(ctx(tmp_path, symbols=symbols, labels=labels, online=False, variant=variant))
    got = colours(frame)
    assert got <= set(range(N_COLORS))  # exact palette colours, nothing else
    assert WHITE in got and BLACK in got and BLUE in got  # paper, type, baselines
    assert GREEN in got  # AAPL is up in every subset
    directions = {q["symbol"]: q["change"] for q in SAMPLE["quotes"] if q["symbol"] in symbols}
    assert (RED in got) == any(v < 0 for v in directions.values())
    # the card is not blank: ink in the body, not only the header
    assert (frame[70:360] != WHITE).sum() > 800


def test_layouts_fit_long_labels_and_the_absolute_change(tmp_path: Path) -> None:
    prime(tmp_path, ALL)
    labels = [f"{s}=Standard & Poor's 500 Composite Index of {s}" for s in ALL]
    for v in range(3):
        frame_of(ctx(tmp_path, symbols=ALL, labels=labels, online=False, variant=v))
        frame_of(ctx(tmp_path, symbols=ALL, online=False, variant=v, show_percent=False))


def test_board_price_and_change_never_collide() -> None:
    """A four-digit price with decimals beside a four-digit absolute change is 161 px
    of mono 16/15 in the 158 px text slot of a two-column board row: the change
    drops to mono 14; an absurdly long pair ellipsizes the price instead."""
    from inkframe.apps.draw import Canvas

    style = L.Style(FONTS)
    room = (L.W - 2 * L.M - 20) // 2 - 90 - 14  # text_right - x0 of a two-column row
    cases = [
        (quote(price=9999.99, change=-1234.56, change_pct=-10.99), False),
        (quote(price=9999.99, change=9999.99, change_pct=99.99), False),
        (quote(price=9999.99, change=-9999.99, change_pct=-100.0), True),
        (quote(price=119665.0, change=-10234.0, change_pct=-7.88), False),
        (quote(price=12345678.9, change=-1234567.8, change_pct=-9.1), False),
    ]
    for q, percent in cases:
        change = SD.fmt_change(q, percent)
        price, f_p, f_c = L.fit_row(style, SD.fmt_price(q.price), change, room)
        assert f_p.getlength(price) + f_c.getlength(change) + 6 <= room, (price, change)
        assert f_c.size >= 14
    # the everyday case keeps the full sizes; only the giant pair loses digits
    price, f_p, f_c = L.fit_row(style, "349.71", "+2.14 %", room)
    assert (price, f_p.size, f_c.size) == ("349.71", 16, 15)
    giant, _ = cases[4]
    price, _, f_c = L.fit_row(style, SD.fmt_price(giant.price), SD.fmt_change(giant, False), room)
    assert price.endswith("…") and f_c.size == 14

    # on the card: white paper between the price and the change of every row
    quotes = [
        quote(symbol=s, label=s, price=9999.99, change=-1234.56, change_pct=-10.99) for s in ALL
    ]
    c = Canvas(background=WHITE)
    L.render_board(c, board_of(*quotes), NOW, style, percent=False, yrange="1mo")
    f = c.to_frame()
    price, f_p, f_c = L.fit_row(style, "9,999.99", SD.fmt_change(quotes[0], False), room)
    col_w = (L.W - 2 * L.M - 20) // 2
    for k in range(12):
        col, row = divmod(k, 6)
        x0 = L.M + col * (col_w + 20)
        left = x0 + int(f_p.getlength(price))
        right = x0 + room - int(f_c.getlength(SD.fmt_change(quotes[0], False)))
        y = 66 + row * 48
        assert right - left >= 6
        assert (f[y + 26 : y + 44, left:right] == WHITE).all(), (k, left, right)
        assert (f[y + 26 : y + 44, x0:left] == RED).sum() == 0  # the change stays right of it


def test_preview_states_are_the_cards_they_name(tmp_path: Path) -> None:
    """``scripts/render_previews.py`` renders every Stocks card into one throw-away
    data dir: the unreachable card must not inherit the stale card's cache."""
    from PIL import Image

    from inkframe.display.palette import MEASURED_DEFAULT
    from scripts.render_previews import render_all

    written = {p.name: p for p in render_all(["stocks"], tmp_path)}
    assert set(written) == {f"stocks-{n}.png" for n in ("v0", "v1", "v2", "stale", "unreachable")}

    def count(name: str, index: int) -> int:
        img = np.asarray(Image.open(written[name]).convert("RGB"))
        return int((img == np.array(MEASURED_DEFAULT.rgb[index], dtype=np.uint8)).all(-1).sum())

    assert count("stocks-unreachable.png", GREEN) == 0  # no quotes on the notice card
    assert count("stocks-unreachable.png", RED) > 0  # the flat-line glyph's red point
    assert count("stocks-stale.png", GREEN) > 0 and count("stocks-stale.png", RED) > 0
    for v in range(3):
        assert count(f"stocks-v{v}.png", GREEN) > 0  # the layouts show the fixture quotes
    assert isinstance(SD.transport, Boom)


def test_hero_picks_the_indices_first() -> None:
    items = [SD.Item(s, s) for s in ("AAPL", "NVDA", "^GSPC", "^IXIC", "T", "^DJI", "F")]
    heroes, rest = L._heroes(items)
    assert [i.symbol for i in heroes] == ["^GSPC", "^IXIC", "^DJI"]
    assert [i.symbol for i in rest] == ["AAPL", "NVDA", "T", "F"]
    heroes, rest = L._heroes(items[:2] + items[4:5])
    assert [i.symbol for i in heroes] == ["AAPL", "NVDA", "T"] and rest == []
    heroes, rest = L._heroes([items[0], items[2], items[4], items[6]])
    assert [i.symbol for i in heroes] == ["^GSPC", "AAPL", "T"]
    assert [i.symbol for i in rest] == ["F"]


def test_sparkline_scales_and_marks_the_last_point() -> None:
    from inkframe.apps.draw import Canvas

    c = Canvas(background=WHITE)
    box = (100, 100, 400, 200)
    L.sparkline(c, box, quote(closes=(10.0, 12.0, 11.0, 13.0), prev_close=11.0, change=2.0))
    f = c.to_frame()
    region = f[100:201, 100:401]
    assert GREEN in colours(region) and BLACK in colours(region) and BLUE in colours(region)
    assert (f[:100] != WHITE).sum() == 0 and (f[201:] != WHITE).sum() == 0  # stays in its box
    assert (f[:, :100] != WHITE).sum() == 0 and (f[:, 401:] != WHITE).sum() == 0
    # one close: a flat line; a baseline outside the padded range is skipped
    c = Canvas(background=WHITE)
    L.sparkline(c, box, quote(closes=(10.0,), prev_close=50.0, change=-40.0))
    f = c.to_frame()
    rows = np.where((f[100:201, 100:401] == BLACK).any(axis=1))[0]
    assert len(rows) <= 3 and RED in colours(f) and BLUE not in colours(f)
    # no closes at all: the price alone
    c = Canvas(background=WHITE)
    L.sparkline(c, (0, 0, 50, 20), quote(closes=(), prev_close=None, change=None))
    assert BLACK in colours(c.to_frame())


def test_variant_choice(tmp_path: Path) -> None:
    base = ctx(tmp_path, online=False)
    assert S.pick_variant(base, {"layout": "rotate"}) == NOW.hour % 3
    for name, idx in (("board", 0), ("hero", 1), ("tiles", 2)):
        assert S.pick_variant(base, {"layout": name}) == idx
    forced = ctx(tmp_path, online=False, variant=5)
    assert S.pick_variant(forced, {"layout": "board"}) == 2
    seeded = ctx(tmp_path, online=False)
    seeded.seed = 7
    assert S.pick_variant(seeded, {"layout": "rotate"}) == 1
    for hour in range(24):
        c = ctx(tmp_path, online=False, now=NOW.replace(hour=hour))
        assert S.pick_variant(c, {"layout": "rotate"}) == hour % 3


def test_render_is_deterministic_and_quick(tmp_path: Path) -> None:
    prime(tmp_path, DEFAULTS)
    c = ctx(tmp_path, online=False, variant=0)
    t0 = time.monotonic()
    a = frame_of(c)
    b = frame_of(c)
    assert time.monotonic() - t0 < 4.0
    assert np.array_equal(a, b)
