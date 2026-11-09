"""The Trivia app: bundled facts, selection, the day cache, Wikimedia, layouts."""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from inkframe.apps import trivia as T
from inkframe.apps import trivia_data as TD
from inkframe.apps import trivia_layouts as L
from inkframe.apps.base import App, FontLibrary, RenderContext, render_to_frame, validate_settings
from inkframe.display.palette import BLACK, NOMINAL, WHITE, rgb_to_indices_exact

FIXTURES = Path(__file__).parent / "fixtures" / "trivia"
#: 09:30 on 9 November: an odd hour, so ``mix`` wants Wikipedia
NOW = datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)
EVEN = NOW.replace(hour=10)
FONTS = FontLibrary()


def _ctx(tmp_path: Path, **kw) -> RenderContext:
    settings = validate_settings(T.SETTINGS_SCHEMA, kw.pop("settings", {}))
    kw.setdefault("online", False)
    return RenderContext(
        settings=settings, now=kw.pop("now", NOW), cache_dir=tmp_path, fonts=FONTS, **kw
    )


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def library() -> tuple[TD.Fact, ...]:
    return TD.load_library()


@pytest.fixture
def payload() -> dict:
    return _fixture("onthisday-11-09.json")


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replaces the pause before a retry: nothing sleeps, the delays are recorded."""
    delays: list[float] = []
    monkeypatch.setattr(TD, "_sleep", delays.append)
    return delays


class FakeFeed:
    """Stands in for ``fetch_onthisday``: serves ``payload``, or raises the
    queued errors first (one per call), and counts every call."""

    def __init__(self, payload: Any, errors: tuple[Exception, ...] = ()) -> None:
        self.payload = payload
        self.errors = list(errors)
        self.calls: list[tuple[int, int, float]] = []

    def __call__(self, month: int, day: int, timeout: float, **_kw: Any) -> Any:
        self.calls.append((month, day, timeout))
        if self.errors:
            raise self.errors.pop(0)
        return self.payload


def _feed(monkeypatch: pytest.MonkeyPatch, payload: Any, *errors: Exception) -> FakeFeed:
    feed = FakeFeed(payload, errors)
    monkeypatch.setattr(TD, "fetch_onthisday", feed)
    return feed


def _no_network(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("the network must not be touched here")


def _only(monkeypatch: pytest.MonkeyPatch, fact: TD.Fact) -> None:
    monkeypatch.setattr(T, "load_library", lambda: (fact,))


def _drawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every string the layouts set through ``draw_tracked``."""
    texts: list[str] = []
    real = L.draw_tracked

    def spy(c, x, baseline, text, font, fill, **kw):
        texts.append(text)
        return real(c, x, baseline, text, font, fill, **kw)

    monkeypatch.setattr(L, "draw_tracked", spy)
    return texts


# --- the bundled facts -------------------------------------------------------------------


def test_library_is_complete_and_clean(library: tuple[TD.Fact, ...]) -> None:
    doc = json.loads(TD.DATA_FILE.read_text(encoding="utf-8"))
    raw = doc["facts"]
    assert doc["categories"] == list(TD.CATEGORIES)
    assert len(library) == len(raw) >= 300  # nothing was skipped while parsing
    texts = [" ".join(f.text.casefold().split()) for f in library]
    assert len(set(texts)) == len(texts), "duplicate fact texts"
    assert len({f.id for f in library}) == len(library), "duplicate ids"
    for fact, entry in zip(library, raw, strict=True):
        expected = {
            "id": fact.id,
            "text": fact.text,
            "category": fact.category,
            "source": fact.source,
        }
        assert entry == expected, entry  # the file is already normalised
        assert re.fullmatch(r"[a-z]{3}-\d{3}", fact.id), fact.id
        assert fact.category in TD.CATEGORIES, entry
        assert TD.MIN_FACT_CHARS <= len(fact.text) <= TD.MAX_FACT_CHARS, entry
        assert fact.text == " ".join(fact.text.split()), entry  # no stray spaces
        assert fact.text[-1] in ".!?”", entry  # a sentence
        # typographic punctuation only (curly quotes, real dashes, one ellipsis glyph)
        assert '"' not in fact.text and "'" not in fact.text, fact.text
        assert "--" not in fact.text and "..." not in fact.text, fact.text
        assert fact.source and len(fact.source.split()) <= 4, entry
    counts = {c: sum(f.category == c for f in library) for c in TD.CATEGORIES}
    assert all(n >= 25 for n in counts.values()), counts


def test_retired_facts_stay_retired(library: tuple[TD.Fact, ...]) -> None:
    """Facts the review found wrong or no longer true were replaced; the old
    wording must not creep back (Iceland had its first mosquitoes in 2025;
    "Z is the least common letter" and "a heart the size of a small car" were
    doubtful)."""
    texts = " ".join(f.text.casefold() for f in library)
    assert "no mosquitoes" not in texts
    assert "least common is z" not in texts
    assert "size of a small car" not in texts


def test_parse_library_normalises_and_skips_bad_entries(caplog) -> None:
    data = {
        "facts": [
            {"id": "a-1", "text": "  Two   spaces  here. ", "category": " Science", "source": "X"},
            {"id": "a-2", "text": "", "category": "science"},
            {"id": "a-3", "text": "Unknown category.", "category": "sports"},
            {"id": "a-1", "text": "Same id again.", "category": "food"},
            {"id": "a-4", "text": "two spaces here.", "category": "food"},
            {"text": "No id at all, which is fine.", "category": "food"},
            "not an object",
        ]
    }
    facts = TD.parse_library(data)
    assert [f.id[:4] for f in facts] == ["a-1", "fact"]
    assert facts[0].text == "Two spaces here." and facts[0].category == "science"
    assert facts[1].id.startswith("fact-") and facts[1].origin == "bundled"
    with pytest.raises(TD.TriviaDataError):
        TD.parse_library({"facts": "nope"})


def test_load_library_errors(tmp_path: Path) -> None:
    with pytest.raises(TD.TriviaDataError, match="missing"):
        TD.load_library(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TD.TriviaDataError, match="unreadable"):
        TD.load_library(bad)
    empty = tmp_path / "empty.json"
    empty.write_text('{"facts": []}', encoding="utf-8")
    with pytest.raises(TD.TriviaDataError, match="no usable"):
        TD.load_library(empty)


# --- settings and the app object ----------------------------------------------------------


def test_settings_schema_defaults_validate() -> None:
    defaults = validate_settings(T.SETTINGS_SCHEMA, {})
    assert defaults == {"source": "mix", "categories": [], "show_source": True, "layout": "rotate"}
    custom = validate_settings(
        T.SETTINGS_SCHEMA,
        {
            "source": "bundled",
            "categories": "space\n\n Food ",
            "show_source": "0",
            "layout": "index",
        },
    )
    assert custom == {
        "source": "bundled",
        "categories": ["space", "Food"],
        "show_source": False,
        "layout": "index",
    }


def test_every_validation_message_names_the_field() -> None:
    cases = [
        ({"source": "twitter"}, "source: must be one of mix, bundled, onthisday"),
        ({"layout": "banner"}, "layout: must be one of rotate, poster, onthisday, index"),
        ({"show_source": "maybe"}, "show_source: expected true/false"),
        ({"categories": 3}, "categories: expected a list of strings"),
        ({"categories": [1]}, "categories: expected strings"),
        ({"category": ["x"]}, "unknown setting"),
    ]
    for data, message in cases:
        with pytest.raises(ValueError, match=re.escape(message)):
            validate_settings(T.SETTINGS_SCHEMA, data)


def test_validate_refuses_unknown_categories_naming_the_field() -> None:
    # the settings API runs T.APP.validate after the schema, so this is a 422 there
    T.APP.validate(validate_settings(T.SETTINGS_SCHEMA, {}))
    T.APP.validate({"categories": [" Space ", "FOOD", "", "  "]})
    with pytest.raises(ValueError) as e:
        T.APP.validate({"categories": ["space", "sports"]})
    assert str(e.value) == "categories: unknown category 'sports'; one of " + ", ".join(
        TD.CATEGORIES
    )


def test_app_satisfies_protocol() -> None:
    assert isinstance(T.APP, App)
    assert T.APP.id == "trivia" == T.__name__.rsplit(".", 1)[-1]
    assert T.APP.needs_dither is False and T.APP.variants == 3 == len(L.VARIANT_NAMES)
    assert T.APP.description == "A fun fact or a moment from history for today's date"


def test_variant_choice(tmp_path: Path) -> None:
    assert T.pick_variant(_ctx(tmp_path, variant=4), {}) == 1
    assert T.pick_variant(_ctx(tmp_path, seed=5), {"layout": "rotate"}) == 2
    assert T.pick_variant(_ctx(tmp_path, seed=5), {"layout": "index"}) == 2
    assert T.pick_variant(_ctx(tmp_path, seed=5), {"layout": "poster"}) == 0
    assert T.pick_variant(_ctx(tmp_path, variant=1), {"layout": "poster"}) == 1  # ctx wins
    assert T.pick_variant(_ctx(tmp_path), {"layout": "rotate"}) == NOW.hour % 3
    later = _ctx(tmp_path, now=NOW + timedelta(hours=1))
    assert T.pick_variant(later, {"layout": "rotate"}) == (NOW.hour + 1) % 3


# --- selection -----------------------------------------------------------------------------


def test_seeded_selection_is_deterministic(tmp_path: Path) -> None:
    settings = validate_settings(T.SETTINGS_SCHEMA, {"source": "bundled"})
    picks = []
    for run in range(2):
        d = tmp_path / f"run{run}"
        picks.append([T.select(_ctx(d, seed=s), settings).fact.id for s in range(8)])
    assert picks[0] == picks[1]
    assert len(set(picks[0])) > 1
    # without a seed the date and hour decide: a preview and the refresh that
    # follows show the same fact, the next hour shows another
    a = T.select(_ctx(tmp_path, now=NOW), settings).fact.id
    b = T.select(_ctx(tmp_path, now=NOW + timedelta(minutes=20)), settings).fact.id
    assert a == b
    hours = {
        T.select(_ctx(tmp_path, now=NOW + timedelta(hours=h)), settings).fact.id for h in range(6)
    }
    assert len(hours) > 1


def test_recent_facts_are_not_repeated(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path, seed=7, settings={"source": "bundled"}
    )  # same seed: only the memory varies
    seen = []
    for _ in range(TD.RECENT_SIZE):
        T.APP.render(ctx)
        seen.append(TD.load_recent(tmp_path)[-1])
    assert len(set(seen)) == TD.RECENT_SIZE == 40
    T.APP.render(ctx)
    recent = TD.load_recent(tmp_path)
    assert len(recent) == TD.RECENT_SIZE and recent[-1] not in seen[1:]


def test_small_pool_only_avoids_the_last_fact(library) -> None:
    pool = list(library[:3])
    recent = [f.id for f in pool]
    for seed in range(20):
        assert TD.choose_fact(pool, recent, random.Random(seed)).id != recent[-1]
    with pytest.raises(ValueError):
        TD.choose_fact([], [], random.Random(0))


def test_categories_filter(tmp_path: Path, library) -> None:
    assert TD.filter_pool(library, []) == list(library)
    assert TD.filter_pool(library, ["", "  "]) == list(library)
    space = TD.filter_pool(library, [" Space "])
    assert space and all(f.category == "space" for f in space)
    two = TD.filter_pool(library, ["food", "numbers"])
    assert {f.category for f in two} == {"food", "numbers"}
    assert TD.filter_pool(library, ["sports"]) == []
    settings = validate_settings(
        T.SETTINGS_SCHEMA, {"source": "bundled", "categories": ["animals"]}
    )
    for s in range(6):
        sel = T.select(_ctx(tmp_path, seed=s), settings)
        assert sel.kind == "bundled" and sel.fact.category == "animals"


def test_empty_pool_renders_a_card_and_is_not_remembered(tmp_path: Path) -> None:
    settings = {"source": "bundled", "categories": ["sports", "music", "films", "cars"]}
    ctx = _ctx(tmp_path, settings=settings, variant=2)
    sel = T.select(ctx, ctx.settings)
    assert (
        sel.placeholder and sel.fact.text == "No facts in those categories: sports, music, films…"
    )
    frame = render_to_frame(T.APP, ctx)
    assert frame[5, 5] == WHITE and frame[300, 300] == WHITE  # the poster, not the index card
    assert (frame == BLACK).sum() > 500
    assert TD.load_recent(tmp_path) == []


def test_missing_library_renders_a_tidy_card(tmp_path: Path, monkeypatch) -> None:
    def missing():
        raise TD.TriviaDataError("trivia.json is missing")

    monkeypatch.setattr(T, "load_library", missing)
    ctx = _ctx(tmp_path, settings={"source": "bundled"})
    sel = T.select(ctx, ctx.settings)
    assert sel.placeholder and "missing" in sel.fact.text
    render_to_frame(T.APP, ctx)
    assert TD.load_recent(tmp_path) == []


# --- caches ----------------------------------------------------------------------------------


def test_recent_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    assert TD.load_recent(tmp_path) == []
    ids: list[str] = []
    for n in range(TD.RECENT_SIZE + 5):
        ids = TD.remember(tmp_path, ids, f"f-{n}", NOW)
    assert TD.load_recent(tmp_path) == ids and len(ids) == TD.RECENT_SIZE
    assert TD.remember(tmp_path, ids, ids[0], NOW)[-1] == ids[0]  # moved to the end
    (tmp_path / TD.RECENT_FILE).write_text("{oops", encoding="utf-8")
    assert TD.load_recent(tmp_path) == []


def test_day_cache_roundtrip_and_staleness(tmp_path: Path, payload) -> None:
    events = TD.parse_onthisday(payload)
    today = NOW.date()
    assert TD.load_day_cache(tmp_path, today) is None
    TD.save_day_cache(tmp_path, today, events, NOW)
    path = tmp_path / "onthisday-11-09.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["date"] == "2026-11-09" and data["fetched_at"].startswith("2026-11-09T09:30")
    assert all(set(e) == {"year", "text"} for e in data["events"])  # no page thumbnails
    assert path.stat().st_size < 6000
    assert TD.load_day_cache(tmp_path, today) == events
    # the same day next year is a different day
    assert TD.load_day_cache(tmp_path, today.replace(year=2027)) is None
    # saving another day removes the old file
    TD.save_day_cache(tmp_path, today + timedelta(days=1), events[:2], NOW)
    assert not path.exists() and (tmp_path / "onthisday-11-10.json").exists()
    (tmp_path / "onthisday-11-10.json").write_text('{"date": 3}', encoding="utf-8")
    assert TD.load_day_cache(tmp_path, today + timedelta(days=1)) is None


# --- Wikimedia -----------------------------------------------------------------------------------


def test_parse_onthisday_from_the_real_response(payload) -> None:
    assert len(payload["selected"]) == 16
    events = TD.parse_onthisday(payload)
    assert len(events) == 15  # the 231-character Kristallnacht entry is dropped
    assert all(len(e.text) <= TD.MAX_EVENT_CHARS for e in events)
    assert events[0].year == 2019 and events[-1].year == 1822  # feed order kept
    by_year = {e.year: e.text for e in events}  # (the two 2019 entries collapse to the second)
    assert by_year[2016] == "A tram derailment in Croydon, London, killed seven people."
    assert events[0].text.endswith("played in a “Game of the Century”.")  # smartened quotes
    assert by_year[1926].startswith("Paul Hindemith’s opera Cardillac premiered")  # (pictured) gone
    assert "\xa0" not in by_year[1939] and by_year[1939].startswith("World War II:")
    assert "Günter" in by_year[1989]
    # the lead event for the single-fact layouts
    fact = TD.event_fact(events[2])
    assert fact.year == 2016 and fact.origin == "onthisday" and fact.category == "on this day"
    assert fact.id.startswith("otd-2016-") and fact.id == TD.event_fact(events[2]).id


def test_parse_onthisday_falls_back_to_events_and_rejects_junk() -> None:
    fallback = {"selected": [], "events": [{"text": "Something happened.", "year": "1900"}]}
    assert TD.parse_onthisday(fallback) == [TD.Event(1900, "Something happened.")]
    messy = {
        "selected": [
            {"text": "x", "year": True},
            {"text": 3, "year": 1},
            "junk",
            {"text": "Twice.", "year": 1},
            {"text": "twice.", "year": 2},
        ]
    }
    assert TD.parse_onthisday(messy) == [TD.Event(1, "Twice.")]
    for junk in ([], "text", {"selected": "no"}, {"events": None}):
        with pytest.raises(ValueError):
            TD.parse_onthisday(junk)


def test_fetch_sends_user_agent_and_maps_failures(payload) -> None:
    seen: dict[str, Any] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        seen["accept"] = request.headers["accept"]
        return httpx.Response(200, json=payload)

    body = TD.fetch_onthisday(3, 7, 4.0, transport=httpx.MockTransport(ok))
    assert body == payload
    assert seen["url"] == "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/03/07"
    assert seen["ua"] == TD.USER_AGENT and seen["accept"] == "application/json"

    def status(code: int, text: str = "") -> httpx.MockTransport:
        return httpx.MockTransport(lambda r: httpx.Response(code, text=text))

    with pytest.raises(TD.FetchError, match="^HTTP 503$") as e:
        TD.fetch_onthisday(11, 9, 4.0, transport=status(503))
    assert e.value.transient is True
    with pytest.raises(TD.FetchError, match="^HTTP 404$") as e:
        TD.fetch_onthisday(11, 9, 4.0, transport=status(404))
    assert e.value.transient is False
    with pytest.raises(TD.FetchError, match="not JSON") as e:
        TD.fetch_onthisday(11, 9, 4.0, transport=status(200, "<html>"))
    assert e.value.transient is False

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TD.FetchError, match="ConnectError") as e:
        TD.fetch_onthisday(11, 9, 4.0, transport=httpx.MockTransport(down))
    assert e.value.transient is True


def test_one_fetch_per_day_then_the_cache(tmp_path: Path, monkeypatch, payload) -> None:
    feed = _feed(monkeypatch, payload)
    settings = {"source": "onthisday"}
    first = render_to_frame(T.APP, _ctx(tmp_path, settings=settings, online=True, http_timeout=4.0))
    assert feed.calls == [(11, 9, 4.0)]
    assert (tmp_path / "onthisday-11-09.json").exists()
    again = render_to_frame(
        T.APP, _ctx(tmp_path, settings=settings, online=True, now=NOW + timedelta(hours=3))
    )
    assert feed.calls == [(11, 9, 4.0)]  # served from the day cache
    assert not np.array_equal(first, again)  # a different hour, a different pick / layout
    # offline the same day still shows Wikipedia from the cache
    monkeypatch.setattr(TD, "fetch_onthisday", _no_network)
    sel = T.select(
        _ctx(tmp_path, settings=settings, online=False),
        validate_settings(T.SETTINGS_SCHEMA, settings),
    )
    assert sel.kind == "onthisday" and 2 <= len(sel.events) <= 3
    # a new day fetches again and replaces the file
    feed = _feed(monkeypatch, payload)
    tomorrow = NOW + timedelta(days=1)
    render_to_frame(T.APP, _ctx(tmp_path, settings=settings, online=True, now=tomorrow))
    assert feed.calls == [(11, 10, 10.0)]
    assert not (tmp_path / "onthisday-11-09.json").exists()
    assert (tmp_path / "onthisday-11-10.json").exists()


def test_retry_once_on_a_transient_failure_only(
    tmp_path: Path, monkeypatch, payload, no_sleep
) -> None:
    settings = {"source": "onthisday"}
    feed = _feed(monkeypatch, payload, TD.FetchError("HTTP 503", transient=True))
    sel = T.select(
        _ctx(tmp_path / "a", settings=settings, online=True),
        validate_settings(T.SETTINGS_SCHEMA, settings),
    )
    assert sel.kind == "onthisday" and len(feed.calls) == 2 and no_sleep == [TD.RETRY_DELAY]

    no_sleep.clear()
    feed = _feed(monkeypatch, payload, TD.FetchError("HTTP 404", transient=False))
    sel = T.select(
        _ctx(tmp_path / "b", settings=settings, online=True),
        validate_settings(T.SETTINGS_SCHEMA, settings),
    )
    assert sel.kind == "bundled" and len(feed.calls) == 1 and no_sleep == []
    assert not list((tmp_path / "b").glob("onthisday-*.json"))

    feed = _feed(monkeypatch, payload, TD.FetchError("ConnectTimeout"), TD.FetchError("HTTP 502"))
    sel = T.select(
        _ctx(tmp_path / "c", settings=settings, online=True),
        validate_settings(T.SETTINGS_SCHEMA, settings),
    )
    assert sel.kind == "bundled" and len(feed.calls) == 2 and no_sleep == [TD.RETRY_DELAY]

    # a body that is not the feed is not retried either
    feed = _feed(monkeypatch, {"unexpected": True})
    sel = T.select(
        _ctx(tmp_path / "d", settings=settings, online=True),
        validate_settings(T.SETTINGS_SCHEMA, settings),
    )
    assert sel.kind == "bundled" and len(feed.calls) == 1


def test_offline_falls_back_to_bundled_with_the_source_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(TD, "fetch_onthisday", _no_network)
    for source in ("onthisday", "mix"):
        d = tmp_path / source
        ctx = _ctx(d, settings={"source": source}, online=False, variant=0)
        sel = T.select(ctx, ctx.settings)
        assert sel.kind == "bundled" and sel.fact.origin == "bundled" and not sel.events
        texts = _drawn(monkeypatch)
        frame = render_to_frame(T.APP, ctx)
        plain = L.compose(FONTS, sel.fact, 0, NOW, show_source=True).to_frame()
        assert np.array_equal(frame, plain)  # no failure note anywhere on the card
        assert sel.fact.source in texts and TD.LICENCE not in texts
        monkeypatch.undo()
        monkeypatch.setattr(TD, "fetch_onthisday", _no_network)


def test_mix_alternates_by_the_hour(tmp_path: Path, monkeypatch, payload) -> None:
    TD.save_day_cache(tmp_path, NOW.date(), TD.parse_onthisday(payload), NOW)
    monkeypatch.setattr(TD, "fetch_onthisday", _no_network)
    settings = validate_settings(T.SETTINGS_SCHEMA, {})
    assert settings["source"] == "mix"
    assert T.select(_ctx(tmp_path, now=NOW), settings).kind == "onthisday"  # 09:xx
    assert T.select(_ctx(tmp_path, now=EVEN), settings).kind == "bundled"  # 10:xx
    assert T.select(_ctx(tmp_path, now=NOW.replace(hour=23)), settings).kind == "onthisday"
    # on an odd hour with nothing cached and no network: bundled, quietly
    assert T.select(_ctx(tmp_path / "empty", now=NOW), settings).kind == "bundled"
    # an even hour never even looks at the network
    feed = _feed(monkeypatch, payload)
    assert T.select(_ctx(tmp_path / "even", now=EVEN, online=True), settings).kind == "bundled"
    assert feed.calls == []
    # "bundled" ignores the cache; "onthisday" uses it at any hour
    bundled = validate_settings(T.SETTINGS_SCHEMA, {"source": "bundled"})
    assert T.select(_ctx(tmp_path, now=NOW), bundled).kind == "bundled"
    otd = validate_settings(T.SETTINGS_SCHEMA, {"source": "onthisday"})
    assert T.select(_ctx(tmp_path, now=EVEN), otd).kind == "onthisday"


def test_onthisday_selection_is_seeded_and_sorted(tmp_path: Path, payload) -> None:
    events = TD.parse_onthisday(payload)
    TD.save_day_cache(tmp_path, NOW.date(), events, NOW)
    settings = validate_settings(T.SETTINGS_SCHEMA, {"source": "onthisday"})
    a = T.select(_ctx(tmp_path, seed=3), settings)
    b = T.select(_ctx(tmp_path, seed=3), settings)
    assert a.events == b.events and a.fact == b.fact
    assert a.events == sorted(a.events, key=lambda e: e.year) and len(a.events) == 3
    assert a.fact.text in {e.text for e in a.events}
    picks = {
        tuple(e.text for e in T.select(_ctx(tmp_path, seed=s), settings).events) for s in range(5)
    }
    assert len(picks) > 1
    # the day's list is not remembered as "recent": that memory is for bundled facts
    render_to_frame(T.APP, _ctx(tmp_path, seed=3, settings={"source": "onthisday"}))
    assert TD.load_recent(tmp_path) == []


# --- layouts -----------------------------------------------------------------------------------


def _ink_bounds(frame: np.ndarray, background: int) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(frame != background)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _extremes(library) -> list[TD.Fact]:
    by_len = sorted(library, key=lambda f: len(f.text))
    digits = max(
        (f for f in library if any(c.isdigit() for c in f.text)), key=lambda f: len(f.text)
    )
    long_source = max(library, key=lambda f: len(f.source))
    return [by_len[0], by_len[-1], digits, long_source]


@pytest.mark.parametrize("variant", range(3))
def test_every_variant_renders_the_extremes(tmp_path, monkeypatch, library, variant) -> None:
    for n, fact in enumerate(_extremes(library)):
        _only(monkeypatch, fact)
        for show in (True, False):
            settings = {"source": "bundled", "show_source": show}
            ctx = _ctx(tmp_path / f"{n}{show}", variant=variant, settings=settings)
            frame = render_to_frame(T.APP, ctx)  # exact NOMINAL colours or it raises
            assert frame.shape == (400, 600) and frame[0, 0] == WHITE
            x0, y0, x1, y1 = _ink_bounds(frame, WHITE)
            assert x0 >= 20 and y0 >= 16 and x1 <= 580 and y1 <= 392, (fact.text, variant, show)
            assert (frame == BLACK).sum() > 1500  # the fact is there, legibly


@pytest.mark.parametrize("variant", range(3))
def test_every_variant_renders_wikipedia_events(tmp_path, variant, payload) -> None:
    events = TD.parse_onthisday(payload)
    TD.save_day_cache(tmp_path, NOW.date(), events, NOW)
    longest = max(events, key=lambda e: len(e.text))
    shortest = min(events, key=lambda e: len(e.text))
    for lead, shown in (
        (longest, [longest, shortest, events[0]]),
        (shortest, [shortest]),
        (events[3], [events[3], longest]),
    ):
        card = L.compose(FONTS, TD.event_fact(lead), variant, NOW, events=shown, show_source=False)
        frame = card.to_frame()
        x0, y0, x1, y1 = _ink_bounds(frame, WHITE)
        assert x0 >= 20 and y0 >= 16 and x1 <= 580 and y1 <= 392, (lead.text, variant)
    # and through the protocol, with the real selection
    ctx = _ctx(tmp_path, settings={"source": "onthisday"}, variant=variant, seed=1)
    frame = render_to_frame(T.APP, ctx)
    rgb = np.asarray(T.APP.render(ctx).convert("RGB"), dtype=np.uint8)
    assert np.array_equal(rgb_to_indices_exact(rgb, NOMINAL), frame)


def test_licence_line_whenever_wikipedia_text_is_shown(
    tmp_path, monkeypatch, payload, library
) -> None:
    TD.save_day_cache(tmp_path, NOW.date(), TD.parse_onthisday(payload), NOW)
    for variant in range(3):
        for show in (True, False):
            texts = _drawn(monkeypatch)
            ctx = _ctx(
                tmp_path, settings={"source": "onthisday", "show_source": show}, variant=variant
            )
            frame = render_to_frame(T.APP, ctx)
            assert TD.LICENCE in texts, (variant, show)
            assert (frame[345:392, 300:570] == BLACK).sum() > 150  # in the footer band
            monkeypatch.undo()
            texts = _drawn(monkeypatch)
            ctx = _ctx(
                tmp_path / "b", settings={"source": "bundled", "show_source": show}, variant=variant
            )
            render_to_frame(T.APP, ctx)
            assert TD.LICENCE not in texts and "Wikipedia" not in " ".join(texts)
            monkeypatch.undo()


def test_body_font_follows_the_digits_rule() -> None:
    serif, threshold = L.body_font(FONTS, "No digits here.")
    assert serif(20).path.endswith("PlayfairDisplay-Regular.ttf") and threshold == L.SERIF_THRESHOLD
    sans, threshold = L.body_font(FONTS, "Born in 1879.")
    assert sans(20).path.endswith("Inter-Regular.ttf") and threshold == 128
    assert L.month_day(NOW) == "November 9" and L.month_day(datetime(2026, 3, 1)) == "March 1"


def test_show_source_hides_only_the_bundled_note(tmp_path, monkeypatch, library) -> None:
    fact = library[0]
    for variant in range(3):
        with_ = L.compose(FONTS, fact, variant, NOW, show_source=True).to_frame()
        without = L.compose(FONTS, fact, variant, NOW, show_source=False).to_frame()
        assert not np.array_equal(with_, without)
        texts = _drawn(monkeypatch)
        L.compose(FONTS, fact, variant, NOW, show_source=False)
        assert not any(fact.source in t for t in texts)
        monkeypatch.undo()


def test_colour_discipline(tmp_path, monkeypatch, library) -> None:
    """Paper, ink and a few accents per card: no anti-aliasing fringes."""
    _only(monkeypatch, library[0])
    for v in range(3):
        frame = render_to_frame(
            T.APP, _ctx(tmp_path / str(v), variant=v, settings={"source": "bundled"})
        )
        assert 2 <= len(np.unique(frame)) <= 5


def test_render_is_deterministic_and_fast(tmp_path, monkeypatch, library) -> None:
    _only(monkeypatch, max(library, key=lambda f: len(f.text)))
    for v in range(3):
        t = time.perf_counter()
        a = render_to_frame(
            T.APP, _ctx(tmp_path / f"a{v}", variant=v, settings={"source": "bundled"})
        )
        assert time.perf_counter() - t < 3.0
        b = render_to_frame(
            T.APP, _ctx(tmp_path / f"b{v}", variant=v, settings={"source": "bundled"})
        )
        assert np.array_equal(a, b)
