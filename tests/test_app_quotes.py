"""The Quotes app: bundled library, selection, caches, ZenQuotes, layouts."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest

from inkframe.apps import load_app
from inkframe.apps import quotes as Q
from inkframe.apps import quotes_data as QD
from inkframe.apps import quotes_type as T
from inkframe.apps.base import App, FontLibrary, RenderContext, render_to_frame, validate_settings
from inkframe.display.palette import BLACK, WHITE

FIXTURES = Path(__file__).parent / "fixtures" / "quotes"
NOW = datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc)
FONTS = FontLibrary()


def _ctx(tmp_path: Path, **kw) -> RenderContext:
    settings = validate_settings(Q.SETTINGS_SCHEMA, kw.pop("settings", {}))
    kw.setdefault("online", False)
    return RenderContext(
        settings=settings, now=kw.pop("now", NOW), cache_dir=tmp_path, fonts=FONTS, **kw
    )


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def library() -> tuple[QD.Quote, ...]:
    return QD.load_library()


def _only(monkeypatch: pytest.MonkeyPatch, quote: QD.Quote) -> None:
    """Make the app's library a single quote, so a render shows exactly it."""
    monkeypatch.setattr(Q, "load_library", lambda: (quote,))


def _no_network(timeout: float, **_kw) -> dict[str, str]:
    raise AssertionError("the network must not be touched here")


# --- the bundled library ----------------------------------------------------------------


def test_library_is_complete_and_clean(library: tuple[QD.Quote, ...]) -> None:
    raw = json.loads(QD.DATA_FILE.read_text(encoding="utf-8"))["quotes"]
    assert len(library) == len(raw) >= 250  # nothing was skipped while parsing
    texts = [" ".join(q.text.casefold().split()) for q in library]
    assert len(set(texts)) == len(texts), "duplicate quote texts"
    assert len({q.id for q in library}) == len(library)
    spellings: dict[str, str] = {}
    for q, entry in zip(library, raw, strict=True):
        assert q.text and q.author and q.source, q
        assert entry["author"] == QD.normalise_author(entry["author"]), entry["author"]
        assert entry["text"] == QD.clean_text(entry["text"]), entry["text"]
        # one spelling per author ("Seneca" and "Lucius Annaeus Seneca" would be caught
        # by review, "Marcus  Aurelius" or "marcus aurelius" by this)
        assert spellings.setdefault(QD.author_key(q.author), q.author) == q.author
        assert q.tags and set(entry["tags"]) <= set(QD.TAGS), entry
        assert len(q.text) <= 300
        # Playfair's numerals are old-style figures: no digits in the serif text
        assert not any(ch.isdigit() for ch in q.text), q.text
        # typographic punctuation only (curly quotes, real dashes)
        assert '"' not in q.text and "'" not in q.text and "--" not in q.text, q.text
    tags = {t for q in library for t in q.tags}
    assert tags == set(QD.TAGS)


def test_parse_library_normalises_and_skips_bad_entries() -> None:
    quotes = QD.parse_library(_fixture("library_small.json"))
    assert len(quotes) == 5  # empty text and the duplicate are skipped
    hunger = quotes[-1]
    assert hunger.text == "Hunger is the best pickle."
    assert hunger.author == "Benjamin Franklin"
    assert hunger.tags == ("humour",)  # alias folded into it, unknown tag dropped
    with pytest.raises(QD.QuoteDataError):
        QD.parse_library({"quotes": "nope"})


def test_load_library_errors(tmp_path: Path) -> None:
    with pytest.raises(QD.QuoteDataError, match="missing"):
        QD.load_library(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(QD.QuoteDataError, match="unreadable"):
        QD.load_library(bad)
    empty = tmp_path / "empty.json"
    empty.write_text('{"quotes": []}', encoding="utf-8")
    with pytest.raises(QD.QuoteDataError, match="no usable"):
        QD.load_library(empty)


def test_text_helpers() -> None:
    assert QD.normalise_author("  marcus   aurelius. ") == "marcus aurelius"
    assert QD.normalise_author("Jerome K. Jerome") == "Jerome K. Jerome"
    assert QD.author_key("Marcus  Aurelius") == QD.author_key("marcus aurelius")
    assert QD.smarten("\"Don't,\" she said... 'no'") == "“Don’t,” she said… ‘no’"
    assert QD.clean_text("  a  b \n\n  c ") == "a b\nc"


# --- settings and the app object ------------------------------------------------------------


def test_settings_schema_defaults_validate() -> None:
    defaults = validate_settings(Q.SETTINGS_SCHEMA, {})
    assert defaults == {"source": "local", "tags": [], "length": "any", "show_source": True}
    custom = validate_settings(
        Q.SETTINGS_SCHEMA,
        {"source": "zenquotes", "tags": "stoic\n\nnature ", "length": "short", "show_source": "0"},
    )
    assert custom == {
        "source": "zenquotes",
        "tags": ["stoic", "nature"],
        "length": "short",
        "show_source": False,
    }
    with pytest.raises(ValueError, match="source"):
        validate_settings(Q.SETTINGS_SCHEMA, {"source": "twitter"})


def test_app_satisfies_protocol() -> None:
    assert isinstance(Q.APP, App)
    assert Q.APP.id == "quotes" == Q.__name__.rsplit(".", 1)[-1]
    assert load_app("quotes") is Q.APP
    assert Q.APP.needs_dither is False and Q.APP.variants == 3


def test_variant_choice(tmp_path: Path) -> None:
    assert Q.choose_variant(_ctx(tmp_path, variant=4)) == 1
    assert Q.choose_variant(_ctx(tmp_path, seed=5)) == 2
    assert Q.choose_variant(_ctx(tmp_path)) == NOW.hour % 3
    assert Q.choose_variant(_ctx(tmp_path, now=NOW + timedelta(hours=1))) == (NOW.hour + 1) % 3


# --- selection ---------------------------------------------------------------------------------


def test_seeded_selection_is_deterministic(tmp_path: Path) -> None:
    settings = validate_settings(Q.SETTINGS_SCHEMA, {})
    picks = []
    for run in range(2):
        d = tmp_path / f"run{run}"
        picks.append([Q.select(_ctx(d, seed=s), settings).quote.id for s in range(6)])
    assert picks[0] == picks[1]
    assert len(set(picks[0])) > 1


def test_recent_quotes_are_not_repeated(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, seed=7)  # the same seed every time: only the memory varies the pick
    seen = []
    for _ in range(QD.RECENT_SIZE):
        Q.APP.render(ctx)
        seen.append(QD.load_recent(tmp_path)[-1])
    assert len(set(seen)) == QD.RECENT_SIZE
    Q.APP.render(ctx)
    recent = QD.load_recent(tmp_path)
    assert len(recent) == QD.RECENT_SIZE and recent[-1] not in seen[1:]


def test_small_pool_only_avoids_the_last_quote() -> None:
    pool = list(QD.parse_library(_fixture("library_small.json"))[:3])
    recent = [q.id for q in pool]
    for seed in range(20):
        assert QD.choose_quote(pool, recent, random.Random(seed)).id != recent[-1]
    with pytest.raises(ValueError):
        QD.choose_quote([], [], random.Random(0))


def test_tag_and_length_filters(tmp_path: Path, library) -> None:
    for tags, wanted in ((["nature"], "nature"), (["Humor"], "humour"), (["stoicism"], "stoic")):
        settings = validate_settings(Q.SETTINGS_SCHEMA, {"tags": tags})
        for s in range(8):
            sel = Q.select(_ctx(tmp_path / str(s), seed=s), settings)
            assert wanted in sel.quote.tags and not sel.notes
    pool, notes = QD.filter_pool(library, ["gardening"], "any")
    assert len(pool) == len(library) and "gardening" in notes[0]
    short, _ = QD.filter_pool(library, [], "short")
    long_, _ = QD.filter_pool(library, ["love"], "long")
    assert short and all(len(q.text) <= QD.SHORT_MAX for q in short)
    assert long_ and all(len(q.text) >= QD.LONG_MIN and "love" in q.tags for q in long_)
    tiny = QD.parse_library(_fixture("library_small.json"))
    pool, notes = QD.filter_pool(tiny, ["courage"], "long")
    assert [q.author for q in pool] == ["Virgil"] and "any length" in notes[0]


# --- caches ------------------------------------------------------------------------------------


def test_recent_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    assert QD.load_recent(tmp_path) == []
    ids: list[str] = []
    for n in range(QD.RECENT_SIZE + 5):
        ids = QD.remember(tmp_path, ids, f"q-{n}", NOW)
    assert QD.load_recent(tmp_path) == ids and len(ids) == QD.RECENT_SIZE
    assert QD.remember(tmp_path, ids, ids[0], NOW)[-1] == ids[0]  # moved to the end
    (tmp_path / QD.RECENT_FILE).write_text("{oops", encoding="utf-8")
    assert QD.load_recent(tmp_path) == []


def test_zen_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    raw = QD.parse_zenquotes(_fixture("zenquotes_random.json"))
    QD.save_zen_cache(tmp_path, raw, NOW)
    data = json.loads((tmp_path / QD.ZEN_CACHE_FILE).read_text(encoding="utf-8"))
    assert data["fetched_at"].startswith("2026-10-19T09:30")
    cache = QD.load_zen_cache(tmp_path)
    assert cache is not None and cache.fetched_at == NOW
    assert cache.quote.text == "If you tell the truth you don’t have to remember anything."
    assert cache.quote.origin == "zenquotes" and cache.quote.id.startswith("zq-")
    (tmp_path / QD.ZEN_CACHE_FILE).write_text('{"fetched_at": "never"}', encoding="utf-8")
    assert QD.load_zen_cache(tmp_path) is None


# --- ZenQuotes ---------------------------------------------------------------------------------


def test_parse_zenquotes_rejects_rate_limit_and_junk() -> None:
    with pytest.raises(ValueError, match="rate limit"):
        QD.parse_zenquotes(_fixture("zenquotes_rate_limit.json"))
    for junk in ({}, [], [{"q": ""}], [{"q": 3, "a": "x"}], "text"):
        with pytest.raises(ValueError):
            QD.parse_zenquotes(junk)


def test_fetch_sends_user_agent_and_parses() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=_fixture("zenquotes_random.json"))

    raw = Q.fetch_zenquote(3, transport=httpx.MockTransport(handler))
    assert raw["author"] == "Mark Twain"
    assert seen == {"url": Q.ZENQUOTES_URL, "ua": Q.USER_AGENT}
    failing = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPError):
        Q.fetch_zenquote(3, transport=failing)


def test_zenquotes_card_carries_the_attribution(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_fetch(timeout: float, **_kw):
        calls.append(timeout)
        return QD.parse_zenquotes(_fixture("zenquotes_random.json"))

    monkeypatch.setattr(Q, "fetch_zenquote", fake_fetch)
    settings = {"source": "zenquotes"}
    for v in range(3):
        d = tmp_path / f"v{v}"
        ctx = _ctx(d, settings=settings, online=True, variant=v, http_timeout=4.0)
        frame = render_to_frame(Q.APP, ctx)
        sel_quote = QD.load_zen_cache(d).quote
        bare = Q.compose(FONTS, sel_quote, v).to_frame()
        attributed = Q.compose(FONTS, sel_quote, v, attribution=Q.ZEN_ATTRIBUTION).to_frame()
        assert np.array_equal(frame, attributed)
        diff = np.argwhere(frame != bare)
        assert len(diff) > 100, "the attribution is drawn"
        # it is the footer that differs (layouts make room for it, so the
        # rest may move, but the footer band itself must carry ink)
        footer = frame[Q.FOOTER_BASELINE - 14 : Q.FOOTER_BASELINE + 4, 300:568]
        background = BLACK if v == Q.BLOCK else WHITE
        assert (footer != background).sum() > 100
    assert calls == [4.0, 4.0, 4.0]


def test_zenquotes_reuses_a_fresh_quote(tmp_path, monkeypatch) -> None:
    raw = QD.parse_zenquotes(_fixture("zenquotes_random.json"))
    QD.save_zen_cache(tmp_path, raw, NOW - timedelta(seconds=30))
    monkeypatch.setattr(Q, "fetch_zenquote", _no_network)
    ctx = _ctx(tmp_path, settings={"source": "zenquotes"}, online=True)
    sel = Q.select(ctx, ctx.settings)
    assert sel.quote.origin == "zenquotes" and sel.attribution == Q.ZEN_ATTRIBUTION
    assert not sel.notes


def test_zenquotes_offline_falls_back_to_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Q, "fetch_zenquote", _no_network)
    ctx = _ctx(tmp_path, settings={"source": "zenquotes"}, online=False, seed=3)
    sel = Q.select(ctx, ctx.settings)
    assert sel.quote.origin == "local" and sel.attribution is None
    assert sel.notes == ["Offline · showing a local quote"]
    frame = render_to_frame(Q.APP, ctx)
    plain = Q.compose(FONTS, sel.quote, Q.choose_variant(ctx)).to_frame()
    assert not np.array_equal(frame, plain)  # the footer note is drawn


def test_zenquotes_failure_uses_stale_cache_then_local(tmp_path, monkeypatch) -> None:
    def broken(timeout: float, **_kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(Q, "fetch_zenquote", broken)
    raw = QD.parse_zenquotes(_fixture("zenquotes_random.json"))
    QD.save_zen_cache(tmp_path, raw, NOW - timedelta(days=2))
    ctx = _ctx(tmp_path, settings={"source": "zenquotes"}, online=True, variant=1)
    sel = Q.select(ctx, ctx.settings)
    assert sel.quote.origin == "zenquotes" and sel.attribution == Q.ZEN_ATTRIBUTION
    assert sel.notes == ["ZenQuotes unreachable · cached 17 Oct"]
    render_to_frame(Q.APP, ctx)  # shows it, and remembers it as recent
    sel = Q.select(ctx, ctx.settings)
    assert sel.quote.origin == "local"
    assert sel.notes == ["ZenQuotes unreachable · showing a local quote"]
    render_to_frame(Q.APP, ctx)


def test_footer_notes_fit_beside_the_attribution() -> None:
    """The longest status line still fits next to the required attribution."""
    mono = FONTS.font("mono", 14)
    budget = (Q.W - 32 - T.tracked_width(mono, Q.ZEN_ATTRIBUTION, 0) - 16) - 48
    for note in ("ZenQuotes unreachable · cached 17 Oct", "Offline · cached 14:05"):
        assert T.tracked_width(mono, note, 0) <= budget, note


def test_missing_library_renders_a_tidy_card(tmp_path, monkeypatch) -> None:
    def missing():
        raise QD.QuoteDataError("quotes.json is missing")

    monkeypatch.setattr(Q, "load_library", missing)
    ctx = _ctx(tmp_path, variant=2)
    sel = Q.select(ctx, ctx.settings)
    assert sel.placeholder and sel.quote is Q.LIBRARY_MISSING
    frame = render_to_frame(Q.APP, ctx)
    assert frame[5, 5] == WHITE  # always the classic card, whatever the variant
    assert QD.load_recent(tmp_path) == []  # the placeholder is not remembered
    ctx = _ctx(tmp_path, settings={"source": "zenquotes"}, online=False)
    assert Q.select(ctx, ctx.settings).notes == ["Offline · no quotes available"]


# --- layouts -------------------------------------------------------------------------------------


def _ink_bounds(frame: np.ndarray, background: int) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(frame != background)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _extremes(library):
    by_len = sorted(library, key=lambda q: len(q.text))
    verse = max((q for q in library if "\n" in q.text), key=lambda q: len(q.text))
    long_name = max(library, key=lambda q: len(q.author))
    long_source = max(library, key=lambda q: len(q.source))
    return [by_len[0], by_len[-1], verse, long_name, long_source]


@pytest.mark.parametrize("variant", range(3))
def test_every_variant_renders_the_extremes(tmp_path, monkeypatch, library, variant) -> None:
    for n, quote in enumerate(_extremes(library)):
        _only(monkeypatch, quote)
        for note in (False, True):
            settings = {"source": "zenquotes"} if note else {}
            ctx = _ctx(tmp_path / f"{n}{note}", variant=variant, settings=settings)
            frame = render_to_frame(Q.APP, ctx)  # exact palette or it raises
            background = BLACK if variant == Q.BLOCK else WHITE
            assert frame[0, 0] == background
            x0, y0, x1, y1 = _ink_bounds(frame, background)
            assert x0 >= 24 and y0 >= 20 and x1 <= 576 and y1 <= 392, (quote.text, variant)


def test_long_quotes_keep_a_legible_size(library) -> None:
    longest = max(library, key=lambda q: len(q.text))
    fit = T.fit_quote(longest.text, Q._serif(FONTS, "regular"), 520, 260, max_size=76)
    assert fit.size >= 24 and not fit.lines[-1].endswith("…")
    shortest = min(library, key=lambda q: len(q.text))
    fit = T.fit_quote(shortest.text, Q._serif(FONTS, "regular"), 520, 260, max_size=76)
    assert fit.size >= 64


def test_fit_keeps_verse_lines_and_avoids_widows() -> None:
    serif = Q._serif(FONTS, "regular")
    verse = "To see a World in a Grain of Sand\nAnd a Heaven in a Wild Flower"
    assert T.fit_quote(verse, serif, 520, 260, max_size=76).lines == verse.split("\n")
    fit = T.fit_quote("Without music, life would be a mistake.", serif, 520, 260, max_size=76)
    assert len(fit.lines[-1].split()) >= 2
    huge = "word " * 400
    fit = T.fit_quote(huge, serif, 520, 120, max_size=76)
    assert fit.size == T.MIN_QUOTE_SIZE and fit.lines[-1].endswith("…")
    assert fit.height <= 120


def test_colour_discipline(tmp_path, monkeypatch, library) -> None:
    """Paper, ink and at most two accents per card (no anti-aliasing fringes:
    an anti-aliased edge would leave in-between colours that map elsewhere)."""
    _only(monkeypatch, library[0])
    for v in range(3):
        frame = render_to_frame(Q.APP, _ctx(tmp_path / str(v), variant=v))
        assert len(np.unique(frame)) <= 4


def test_render_is_fast(tmp_path, monkeypatch, library) -> None:
    _only(monkeypatch, max(library, key=lambda q: len(q.text)))
    for v in range(3):
        t = time.perf_counter()
        render_to_frame(Q.APP, _ctx(tmp_path / str(v), variant=v))
        assert time.perf_counter() - t < 3.0
