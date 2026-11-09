"""Data side of the Stocks app: Yahoo Finance's chart endpoint, parsed and cached.

Nothing here draws (``stocks_layouts`` does).  The flow for one render is

    symbols + labels + range  ->  cache fresh (15 min, same symbols and range)?
        yes: quotes from ``quotes.json``
        no:  every symbol in parallel (4 threads, one 9.5 s budget, one retry
             of a transient failure on the second host)  ->  parse_chart
             all failed  ->  the cache regardless of age (stale)  ->  or nothing

Provider.  Yahoo Finance's *unofficial*, keyless chart endpoint
(``https://query1.finance.yahoo.com/v8/finance/chart/<symbol>?range=1mo&interval=1d``):
one request per symbol gives the last price, the session's times, the
previous closes for the day change and the closes for the sparkline.  It
answers 429 to an empty or a ``python-httpx`` User-Agent and 200 to ours
(probed 2026-11-09), so every request carries ``inkframe.USER_AGENT`` and
a 429 counts as transient.  The one retry goes to ``query2`` (the same API
on a second host) rather than hitting the same host again.

There is no second provider.  Stooq's free daily CSV, which the 0.2 plan
named as the fallback, now sits behind a JavaScript browser-verification
challenge (its CSV URL returns an HTML page with a proof-of-work script,
probed the same day), so it is unusable from the frame.  Degradation is the
cache, then the "unreachable" card.

``transport`` is the one seam to the network: ``None`` means a real
``httpx`` transport; tests install an ``httpx.MockTransport`` and see the
exact requests (URL, host and headers) the app makes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

import httpx

from inkframe import USER_AGENT

log = logging.getLogger("inkframe.apps.stocks")

PROVIDER = "Yahoo Finance"
#: the first host takes every first attempt, the second every retry
HOSTS: tuple[str, str] = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
CHART_PATH = "/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
#: the ``range`` setting -> Yahoo's ``range`` parameter (daily bars)
RANGES: dict[str, str] = {"1w": "5d", "1mo": "1mo", "3mo": "3mo"}
RANGE_LABELS: dict[str, str] = {"1w": "1 WEEK", "1mo": "1 MONTH", "3mo": "3 MONTHS"}
CACHE_NAME = "quotes.json"
CACHE_VERSION = 1
#: reuse fetched quotes this long without touching the network
CACHE_WINDOW = timedelta(minutes=15)
#: the whole fetch (every symbol, retries included) must finish within this
BUDGET = 9.5
#: seconds to wait before the one retry of a transient failure
RETRY_DELAY = 2.0
#: parallel requests
WORKERS = 4
#: most symbols on one card (every layout has room for twelve)
MAX_SYMBOLS = 12
#: what a symbol may look like after upper-casing: AAPL, ^GSPC, BTC-USD, BRK-B, 7203.T, EURUSD=X
SYMBOL_RE = re.compile(r"^[A-Z0-9.^=-]{1,12}$")
#: the pause before a retry (tests replace it so nothing sleeps)
_sleep = time.sleep
#: ``None`` for the real network; tests install an ``httpx.MockTransport``
transport: httpx.BaseTransport | None = None

MINUS = "−"
DASH = "–"
DOT = "·"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class FetchError(Exception):
    """The network, the HTTP status or the response body let us down.

    ``transient`` says whether trying again (on the other host) may help: a
    transport error, a timeout, an HTTP 5xx, a 429 or a 200 that is not JSON.
    Not a 4xx with a body (the provider's answer: an unknown symbol), a
    redirect loop or no time left."""

    def __init__(self, *args: object, transient: bool = True) -> None:
        super().__init__(*args)
        self.transient = transient


# --- symbols and labels -----------------------------------------------------------------


def normalise_symbol(raw: Any) -> str:
    """``" aapl "`` -> ``"AAPL"`` (what Yahoo and the cache key use)."""
    return str(raw or "").strip().upper()


def clean_symbols(raw: Sequence[Any]) -> list[str]:
    """The symbols a render uses: normalised, de-duplicated, invalid ones dropped
    (logged), at most ``MAX_SYMBOLS``.  ``validate`` refuses them up front; this
    is the belt for settings that reached the database some other way."""
    out: list[str] = []
    for item in raw or ():
        sym = normalise_symbol(item)
        if not sym:
            continue
        if SYMBOL_RE.fullmatch(sym) is None:
            log.warning("stocks: ignoring symbol %r", item)
            continue
        if sym not in out:
            out.append(sym)
    if len(out) > MAX_SYMBOLS:
        log.warning("stocks: %d symbols configured; showing the first %d", len(out), MAX_SYMBOLS)
    return out[:MAX_SYMBOLS]


def parse_label(raw: Any) -> tuple[str, str] | None:
    """``"^GSPC=S&P 500"`` -> ``("^GSPC", "S&P 500")``; ``None`` when the entry is not
    ``SYMBOL=Name`` with both halves present."""
    text = str(raw or "")
    if "=" not in text:
        return None
    sym, name = text.split("=", 1)
    sym, name = normalise_symbol(sym), " ".join(name.split())
    if not sym or not name:
        return None
    return sym, name


def parse_labels(raw: Sequence[Any]) -> dict[str, str]:
    """The ``labels`` setting as ``{symbol: name}``; malformed entries are skipped."""
    out: dict[str, str] = {}
    for item in raw or ():
        parsed = parse_label(item)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


# --- model ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """One symbol as the card shows it."""

    symbol: str
    #: what the card prints: the user's label, else the symbol
    label: str
    price: float
    #: day change in price and in percent; ``None`` when no previous close is known
    change: float | None
    change_pct: float | None
    currency: str
    #: time of the last price (aware, UTC)
    as_of: datetime
    #: ``open`` while the regular session was running when the data was fetched, else ``closed``
    session: str
    #: daily closes for the sparkline, oldest first, nulls dropped
    closes: tuple[float, ...]
    #: the previous session's close (the sparkline's baseline)
    prev_close: float | None = None
    #: when the regular session the price belongs to ends (aware, UTC); ``None`` when
    #: the provider did not say (an older cache file): ``LIVE`` then trusts ``session``
    session_end: datetime | None = None

    @property
    def direction(self) -> int:
        """1 up, -1 down, 0 flat or unknown."""
        if self.change is None or self.change == 0:
            return 0
        return 1 if self.change > 0 else -1

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "label": self.label,
            "price": self.price,
            "change": self.change,
            "change_pct": self.change_pct,
            "currency": self.currency,
            "as_of": self.as_of.astimezone(timezone.utc).isoformat(),
            "session": self.session,
            "closes": list(self.closes),
            "prev_close": self.prev_close,
            "session_end": (
                self.session_end.astimezone(timezone.utc).isoformat()
                if self.session_end is not None
                else None
            ),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Quote:
        as_of = _parse_iso(d.get("as_of"))
        if as_of is None:
            raise ValueError("bad as_of")
        return cls(
            symbol=normalise_symbol(d["symbol"]),
            label=str(d.get("label") or d["symbol"]),
            price=float(d["price"]),
            change=_num(d.get("change")),
            change_pct=_num(d.get("change_pct")),
            currency=str(d.get("currency") or ""),
            as_of=as_of,
            session="open" if d.get("session") == "open" else "closed",
            closes=tuple(c for c in (_num(v) for v in d.get("closes") or ()) if c is not None),
            prev_close=_num(d.get("prev_close")),
            session_end=_parse_iso(d.get("session_end")),
        )


@dataclass(frozen=True)
class Item:
    """One configured symbol on the card: its quote, or ``None`` (a dash, "no data")."""

    symbol: str
    label: str
    quote: Quote | None = None


@dataclass
class Board:
    """What the renderer gets: the items plus how fresh and complete they are."""

    items: list[Item] = field(default_factory=list)
    #: when the quotes were fetched (``None`` if never)
    fetched_at: datetime | None = None
    #: ``True`` when the quotes come from an old cache because fetching failed or
    #: was not allowed; the card carries the red stamp
    stale: bool = False
    #: whether the render was allowed to use the network (``False`` only in offline
    #: mode, ``INKFRAME_OFFLINE=1``): picks "OFFLINE" over "COULDN'T UPDATE"
    frame_online: bool = True
    #: ``ok`` | ``unreachable`` (nothing to show) | ``empty`` (no symbols configured)
    state: str = "ok"
    #: the last failure, for the log and the unreachable card
    detail: str = ""

    @property
    def quotes(self) -> list[Quote]:
        return [i.quote for i in self.items if i.quote is not None]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- parsing ----------------------------------------------------------------------------


def parse_chart(body: Any, symbol: str, label: str, fetched_at: datetime) -> Quote:
    """Turn one ``/v8/finance/chart`` response into a ``Quote``.

    Raises ``ValueError`` for the provider's error object (``chart.error``, as
    the 404 for an unknown symbol carries), a null result or a shape without
    a price.

    The day change is the last price against the *previous session's* close,
    read from the daily series: the close of the last bar before the current
    trading period (``meta.currentTradingPeriod.regular.start``).
    ``meta.chartPreviousClose`` is not that: it is the close before the
    requested range (a month ago for ``range=1mo``), so it is only the last
    resort, after ``meta.fulldayChange``.
    """
    if not isinstance(body, Mapping):
        raise ValueError("response is not a JSON object")
    chart = body.get("chart")
    if not isinstance(chart, Mapping):
        raise ValueError("response has no chart")
    err = chart.get("error")
    if isinstance(err, Mapping) and err:
        desc = err.get("description") or err.get("code") or "provider error"
        raise ValueError(str(desc))
    results = chart.get("result")
    if not isinstance(results, Sequence) or isinstance(results, str) or not results:
        raise ValueError("no result for this symbol")
    r = results[0]
    if not isinstance(r, Mapping):
        raise ValueError("malformed result")
    meta = r.get("meta") if isinstance(r.get("meta"), Mapping) else {}
    price = _num(meta.get("regularMarketPrice"))
    if price is None:
        raise ValueError("no price in the response")

    # the daily series (nulls dropped, pairs kept aligned)
    stamps = r.get("timestamp")
    quote0: Any = {}
    ind = r.get("indicators")
    if isinstance(ind, Mapping):
        q = ind.get("quote")
        if isinstance(q, Sequence) and not isinstance(q, str) and q and isinstance(q[0], Mapping):
            quote0 = q[0]
    raw_closes = quote0.get("close") if isinstance(quote0, Mapping) else None
    pairs: list[tuple[float | None, float]] = []
    if isinstance(raw_closes, Sequence) and not isinstance(raw_closes, str):
        ts = list(stamps) if isinstance(stamps, Sequence) and not isinstance(stamps, str) else []
        for i, raw in enumerate(raw_closes):
            close = _num(raw)
            if close is None:
                continue
            pairs.append((_num(ts[i]) if i < len(ts) else None, close))
    closes = tuple(c for _, c in pairs)

    # the current session, for the session flag and the previous close
    period = meta.get("currentTradingPeriod")
    regular = period.get("regular") if isinstance(period, Mapping) else None
    start = _num(regular.get("start")) if isinstance(regular, Mapping) else None
    end = _num(regular.get("end")) if isinstance(regular, Mapping) else None
    fetched_unix = fetched_at.astimezone(timezone.utc).timestamp()
    session = (
        "open"
        if start is not None and end is not None and start <= fetched_unix < end
        else ("closed")
    )

    as_of_unix = _num(meta.get("regularMarketTime"))
    if as_of_unix is None and pairs and pairs[-1][0] is not None:
        as_of_unix = pairs[-1][0]
    as_of = (
        datetime.fromtimestamp(as_of_unix, timezone.utc)
        if as_of_unix is not None
        else fetched_at.astimezone(timezone.utc)
    )

    prev: float | None = None
    if start is not None and pairs:
        before = [c for t, c in pairs if t is not None and t < start]
        if as_of_unix is not None:
            in_period = as_of_unix >= start
        else:
            in_period = any(t is not None and t >= start for t, _ in pairs)
        if in_period:
            # the price belongs to the current period: the previous session is the
            # last bar before it (today's own bar may be missing or null)
            prev = before[-1] if before else None
        elif len(before) >= 2:
            # the price is from an earlier session (a weekend with Monday's period
            # announced): the last bar is that session, the one before it the previous
            prev = before[-2]
    if prev is None:
        delta = _num(meta.get("fulldayChange"))
        if delta is not None:
            prev = price - delta
    if prev is None:
        prev = _num(meta.get("chartPreviousClose"))

    change = price - prev if prev is not None else None
    change_pct = (change / prev * 100.0) if change is not None and prev else None

    return Quote(
        symbol=normalise_symbol(meta.get("symbol") or symbol),
        label=label,
        price=price,
        change=change,
        change_pct=change_pct,
        currency=str(meta.get("currency") or ""),
        as_of=as_of,
        session=session,
        closes=closes,
        prev_close=prev,
        session_end=datetime.fromtimestamp(end, timezone.utc) if end is not None else None,
    )


# --- network ----------------------------------------------------------------------------


def chart_url(host: str, symbol: str) -> str:
    return f"https://{host}{CHART_PATH.format(symbol=url_quote(symbol, safe=''))}"


def fetch_json(host: str, symbol: str, params: Mapping[str, Any], timeout: float) -> Any:
    """GET one chart from ``host`` and decode the JSON body.  The only function
    that touches the network (through ``transport``).  Raises ``FetchError``;
    a 4xx with a JSON body (the provider's "Not Found") is returned for
    ``parse_chart`` to report."""
    try:
        with httpx.Client(
            transport=transport,
            headers=HEADERS,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=True,
        ) as client:
            r = client.get(chart_url(host, symbol), params=dict(params))
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
        raise FetchError(
            f"{type(e).__name__}: {e}", transient=isinstance(e, (httpx.TransportError, OSError))
        ) from None
    if r.status_code == 429 or r.status_code >= 500:
        raise FetchError(f"HTTP {r.status_code} from {host}", transient=True)
    try:
        return r.json()
    except ValueError:
        raise FetchError(
            f"HTTP {r.status_code} from {host}: response is not JSON",
            transient=r.status_code == 200,
        ) from None


def _budget(deadline: float, http_timeout: float) -> float:
    left = deadline - time.monotonic()
    if left < 0.5:
        raise FetchError("out of time", transient=False)
    return max(0.5, min(float(http_timeout), left))


def fetch_quote(
    symbol: str,
    label: str,
    yrange: str,
    *,
    fetched_at: datetime,
    deadline: float,
    http_timeout: float,
) -> Quote:
    """One symbol: the first host, then (a transient failure, time permitting)
    the second host once after ``RETRY_DELAY``.  Raises ``FetchError`` or
    ``ValueError`` (the provider's answer did not parse)."""
    params = {"range": RANGES.get(yrange, "1mo"), "interval": "1d"}
    try:
        body = fetch_json(HOSTS[0], symbol, params, _budget(deadline, http_timeout))
    except FetchError as e:
        if not e.transient or deadline - time.monotonic() <= RETRY_DELAY + 1.0:
            raise
        log.info(
            "stocks: %s failed: %s; retrying once on %s in %g s", symbol, e, HOSTS[1], RETRY_DELAY
        )
        _sleep(RETRY_DELAY)
        body = fetch_json(HOSTS[1], symbol, params, _budget(deadline, http_timeout))
    return parse_chart(body, symbol, label, fetched_at)


def fetch_all(
    symbols: Sequence[str],
    labels: Mapping[str, str],
    yrange: str,
    *,
    fetched_at: datetime,
    budget: float = BUDGET,
    http_timeout: float = 10.0,
) -> dict[str, Quote | Exception]:
    """Fetch every symbol in parallel (``WORKERS`` threads).  Returns
    ``{symbol: Quote | Exception}`` and never waits much longer than ``budget``
    in total; a symbol still pending at the deadline gets a ``TimeoutError``.
    Each symbol is requested at most once per host."""
    results: dict[str, Quote | Exception] = {}
    if not symbols:
        return results
    deadline = time.monotonic() + budget

    def attempt(symbol: str) -> Quote:
        return fetch_quote(
            symbol,
            labels.get(symbol, symbol),
            yrange,
            fetched_at=fetched_at,
            deadline=deadline,
            http_timeout=http_timeout,
        )

    pool = ThreadPoolExecutor(max_workers=min(len(symbols), WORKERS), thread_name_prefix="stocks")
    try:
        futures = {pool.submit(attempt, s): s for s in symbols}
        done, pending = wait(futures, timeout=budget + 0.5)
        for fut in done:
            try:
                results[futures[fut]] = fut.result()
            except Exception as e:  # noqa: BLE001 - any failure is "this symbol failed"
                results[futures[fut]] = e
        for fut in pending:
            results[futures[fut]] = TimeoutError(f"no answer within {budget:g} s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# --- cache ------------------------------------------------------------------------------


@dataclass
class Cached:
    fetched_at: datetime
    symbols: list[str]
    range: str
    quotes: dict[str, Quote]


def cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / CACHE_NAME


def read_cache(cache_dir: Path) -> Cached | None:
    """The cached quotes, or ``None`` if missing or corrupt."""
    path = cache_path(cache_dir)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = _parse_iso(d["fetched_at"])
        if fetched_at is None:
            raise ValueError("bad fetched_at")
        quotes = {}
        for entry in d.get("quotes", []):
            q = Quote.from_json(entry)
            quotes[q.symbol] = q
        return Cached(
            fetched_at,
            [normalise_symbol(s) for s in d.get("symbols", [])],
            str(d.get("range") or "1mo"),
            quotes,
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("stocks: ignoring unreadable cache %s: %s", path, e)
        return None


def write_cache(
    cache_dir: Path,
    fetched_at: datetime,
    symbols: Sequence[str],
    yrange: str,
    quotes: Sequence[Quote],
) -> None:
    """Atomically write the cache file (best effort: a full disk only logs)."""
    path = cache_path(cache_dir)
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "symbols": list(symbols),
        "range": yrange,
        "quotes": [q.to_json() for q in quotes],
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("stocks: could not write cache %s: %s", path, e)


# --- loading ----------------------------------------------------------------------------


def _items(
    symbols: Sequence[str], labels: Mapping[str, str], quotes: Mapping[str, Quote]
) -> list[Item]:
    out = []
    for s in symbols:
        label = labels.get(s, s)
        q = quotes.get(s)
        out.append(Item(s, label, replace(q, label=label) if q is not None else None))
    return out


def load(
    symbols: Sequence[str],
    labels: Mapping[str, str],
    yrange: str,
    *,
    now: datetime,
    cache_dir: Path,
    online: bool,
    http_timeout: float = 10.0,
) -> Board:
    """Quotes for ``symbols`` from the cache window, the network, or a stale cache.

    Never raises for network trouble: the ``Board`` says what it is (``stale``,
    ``state``) and the card shows that."""
    symbols = list(symbols)
    if not symbols:
        return Board(state="empty", frame_online=online)
    if yrange not in RANGES:
        yrange = "1mo"
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cached = read_cache(cache_dir)
    if cached is not None:
        age = now_utc - cached.fetched_at
        if (
            cached.quotes
            and cached.symbols == symbols
            and cached.range == yrange
            and timedelta(minutes=-5) <= age < CACHE_WINDOW
        ):
            return Board(
                _items(symbols, labels, cached.quotes), cached.fetched_at, frame_online=online
            )

    def from_cache(detail: str) -> Board:
        if cached is None or not cached.quotes:
            return Board(state="unreachable", frame_online=online, detail=detail)
        return Board(
            _items(symbols, labels, cached.quotes),
            cached.fetched_at,
            stale=True,
            frame_online=online,
            detail=detail,
        )

    if not online:
        return from_cache("offline")

    results = fetch_all(
        symbols, labels, yrange, fetched_at=now_utc, budget=BUDGET, http_timeout=http_timeout
    )
    quotes: dict[str, Quote] = {}
    detail = ""
    for s in symbols:
        got = results.get(s)
        if isinstance(got, Quote):
            quotes[s] = got
        else:
            detail = f"{s}: {got}" if got is not None else f"{s}: no answer"
            log.warning("stocks: %s failed: %s", s, got)
    if not quotes:
        return from_cache(detail)
    write_cache(cache_dir, now_utc, symbols, yrange, list(quotes.values()))
    return Board(_items(symbols, labels, quotes), now_utc, frame_online=online, detail=detail)


# --- formatting -------------------------------------------------------------------------


def fmt_price(v: float | None) -> str:
    """Two decimals under 10 000 (``349.71``, ``7,481.56``); thousands separators and
    no decimals from there (``51,995``).  A true minus sign; ``None`` is a dash."""
    if v is None:
        return DASH
    sign = MINUS if v < 0 else ""
    a = abs(v)
    return sign + (f"{a:,.2f}" if a < 10_000 else f"{a:,.0f}")


def fmt_change(q: Quote | None, percent: bool = True) -> str:
    """``+1.23 %`` / ``−0.45 %`` (U+2212), or the absolute change ``+7.34``; a flat
    quote prints without a sign; no previous close prints a dash."""
    if q is None or q.change is None:
        return DASH
    v = q.change_pct if percent else q.change
    if v is None:
        return DASH
    sign = "+" if v > 0 else MINUS if v < 0 else ""
    return f"{sign}{abs(v):.2f} %" if percent else f"{sign}{fmt_price(abs(v))}"


def fmt_clock(t: datetime) -> str:
    """``3:42 PM``: the 12-hour clock, always."""
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def local(t: datetime, now: datetime) -> datetime:
    """``t`` in the frame's zone (the zone of ``now``; UTC when ``now`` is naive)."""
    return t.astimezone(now.tzinfo or timezone.utc)


def status_line(board: Board, now: datetime) -> str:
    """From the first quote that has data, in the frame's local time:
    ``LIVE · 3:42 PM`` while the session is open (the data is fresh and, when
    the provider said when the session ends, the bell has not rung yet: a
    card drawn from the cache at 4:05 PM says ``AS OF``, not ``LIVE``),
    ``FRI CLOSE · 4:00 PM`` when the last price is from an earlier local day,
    ``AS OF 4:00 PM`` when it is today's but the session is closed."""
    quotes = board.quotes
    if not quotes:
        return ""
    q = quotes[0]
    t = local(q.as_of, now)
    today = local(now, now).date()
    clock = fmt_clock(t)
    if t.date() < today:
        return f"{WEEKDAYS[t.weekday()]} CLOSE {DOT} {clock}"
    still_open = q.session_end is None or now < q.session_end
    if q.session == "open" and not board.stale and still_open:
        return f"LIVE {DOT} {clock}"
    return f"AS OF {clock}"


def stamp_line(board: Board, now: datetime) -> tuple[str, int]:
    """``("UPDATED 9:42 AM", BLACK)`` for fresh data; a stale cache shown because
    the refresh failed is ``("COULDN'T UPDATE, DATA FROM 9:42 AM", RED)``, in
    offline mode ``("OFFLINE, DATA FROM 9:42 AM", RED)``: Weather's wording.
    A day name is added when the data is not from today."""
    from inkframe.display.palette import BLACK, RED  # local: this module stays drawing-free

    if board.fetched_at is None:
        return "", BLACK
    t = local(board.fetched_at, now)
    stamp = fmt_clock(t)
    if t.date() != local(now, now).date():
        stamp = f"{WEEKDAYS[t.weekday()]} {stamp}"
    if board.stale and board.frame_online:
        return f"COULDN'T UPDATE, DATA FROM {stamp}", RED
    if board.stale:
        return f"OFFLINE, DATA FROM {stamp}", RED
    return f"UPDATED {stamp}", BLACK
