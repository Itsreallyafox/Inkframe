"""Headlines app: feeds, normalisation, cache, selection, every layout and state.

Offline and deterministic: ``headlines.fetch_feed`` is replaced by a function
serving the fixture feeds in ``tests/fixtures/headlines/`` (saved from the live
BBC, NYT and Guardian feeds, trimmed to 15 items, summaries dropped) or by one
that fails; nothing here touches the network.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest

from inkframe.apps import headlines, load_app
from inkframe.apps import headlines_data as data
from inkframe.apps.base import App, RenderContext, render_to_frame, validate_settings
from inkframe.apps.draw import Canvas
from inkframe.apps.headlines_layouts import Story, meta, status_line
from inkframe.apps.headlines_text import clean_title, source_name
from inkframe.apps.headlines_type import Face, set_headline, short_forms
from inkframe.display.palette import BLACK, BLUE, RED, WHITE, YELLOW

FIX = Path(__file__).parent / "fixtures" / "headlines"
BBC, NYT, GUARDIAN = headlines.DEFAULT_FEEDS
ATOM = "https://wire.example.org/atom.xml"
BODIES = {BBC: "bbc.xml", NYT: "nyt.xml", GUARDIAN: "guardian.xml", ATOM: "atom.xml"}
#: 19:05 UTC, ten minutes after the newest fixture story; a non-UTC local zone
NOW = datetime(2026, 10, 19, 20, 5, tzinfo=timezone(timedelta(hours=1)))
#: the clock atom.xml was written against
ATOM_NOW = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
DEFAULTS = validate_settings(headlines.SETTINGS_SCHEMA, {})


class Feeds:
    """A stand-in for ``fetch_feed``: serves fixtures, counts calls, can fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.down: set[str] = set()

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        if url in self.down or "*" in self.down:
            raise httpx.ConnectError("network is down")
        return (FIX / BODIES[url]).read_bytes()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything reaches the real fetch function."""

    def refuse(url: str, timeout: float) -> bytes:
        raise AssertionError(f"test tried to fetch {url}")

    monkeypatch.setattr(headlines, "fetch_feed", refuse)


@pytest.fixture
def feeds(monkeypatch: pytest.MonkeyPatch) -> Feeds:
    f = Feeds()
    monkeypatch.setattr(headlines, "fetch_feed", f)
    return f


def make_ctx(cache: Path, **kw) -> RenderContext:
    return RenderContext(
        settings=kw.pop("settings", DEFAULTS),
        now=kw.pop("now", NOW),
        cache_dir=cache,
        online=kw.pop("online", False),
        **kw,
    )


def frame_of(cache: Path, **kw) -> np.ndarray:
    frame = render_to_frame(headlines.APP, make_ctx(cache, **kw))
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert set(np.unique(frame).tolist()) <= set(range(6))
    return frame


def state_of(cache: Path, settings=DEFAULTS, **kw) -> data.FeedState:
    return data.load(
        data.parse_feed_lines(settings["feeds"]),
        now=kw.get("now", NOW),
        cache_dir=cache,
        online=kw.get("online", True),
        timeout=5.0,
        fetch=headlines.fetch_feed,
    )


# --- contract ----------------------------------------------------------------------------


def test_app_satisfies_the_contract() -> None:
    assert isinstance(headlines.APP, App)
    assert headlines.APP.id == "headlines" == headlines.__name__.rsplit(".", 1)[1]
    assert load_app("headlines") is headlines.APP
    assert headlines.APP.needs_dither is False
    assert headlines.APP.variants == 3 == len(headlines.VARIANT_NAMES)


def test_settings_schema_defaults_validate() -> None:
    schema = headlines.SETTINGS_SCHEMA
    assert DEFAULTS == {
        "feeds": list(headlines.DEFAULT_FEEDS),
        "count": 5,
        "max_age_hours": 36,
        "mix": "interleave",
    }
    assert validate_settings(schema, DEFAULTS) == DEFAULTS
    assert validate_settings(schema, {"count": "8", "mix": "by_feed"})["count"] == 8
    for bad in ({"count": 2}, {"count": 9}, {"max_age_hours": 0}, {"max_age_hours": 73}):
        with pytest.raises(ValueError):
            validate_settings(schema, bad)
    with pytest.raises(ValueError, match="mix"):
        validate_settings(schema, {"mix": "random"})
    with pytest.raises(ValueError, match="at least one"):
        validate_settings(schema, {"feeds": []})
    assert {f.key: f.to_dict()["type"] for f in schema} == {
        "feeds": "list",
        "count": "number",
        "max_age_hours": "number",
        "mix": "select",
    }


def test_variant_choice() -> None:
    ctx = make_ctx(Path("."))
    assert headlines.pick_variant(ctx) == NOW.hour % 3
    ctx.seed = 7
    assert headlines.pick_variant(ctx) == 1
    ctx.variant = 2
    assert headlines.pick_variant(ctx) == 2
    ctx.variant = 5
    assert headlines.pick_variant(ctx) == 2


# --- feed list, names, normalisation ----------------------------------------------------


def test_feed_lines_labels_and_junk() -> None:
    feeds = data.parse_feed_lines(
        [f"Beeb {BBC}", BBC, "not a url", "ftp://x.example/feed", f"  {NYT}  ", ""]
    )
    assert [(f.url, f.label) for f in feeds] == [(BBC, "Beeb"), (NYT, None)]
    assert [f.url for f in data.parse_feed_lines(f"{BBC}\n\n{NYT}\n")] == [BBC, NYT]
    many = data.parse_feed_lines([f"https://f{i}.example/rss" for i in range(12)])
    assert len(many) == data.MAX_FEEDS


def test_source_names() -> None:
    assert source_name(BBC) == "BBC"
    assert source_name(NYT, "NYT > Top Stories") == "NYT"
    assert source_name(GUARDIAN) == "Guardian"
    assert source_name("https://feeds.npr.org/1001/rss.xml") == "NPR"
    assert source_name("https://x.example/a", "World news | The Daily Planet") == "Daily Planet"
    assert source_name("https://x.example/a", "Example Wire - Top Stories") == "Example Wire"
    assert source_name("https://feeds.example.org/rss") == "Example"


def test_clean_title() -> None:
    assert clean_title("Markets rally - Reuters") == "Markets rally"
    assert clean_title("Floods hit coast | BBC News") == "Floods hit coast"
    assert clean_title("Curfews announced as PM blames left – Europe live") == (
        "Curfews announced as PM blames left"
    )
    assert clean_title("Budget day for Reeves – live updates") == "Budget day for Reeves"
    assert clean_title("How to live") == "How to live"
    # a dash that is part of the headline stays
    assert clean_title("Spurs 2 - 1 Arsenal") == "Spurs 2 - 1 Arsenal"
    assert clean_title("Q&amp;amp;A: it&#8217;s &quot;on&quot;") == "Q&A: it’s “on”"
    assert clean_title("  Storm \n batters the <b>coast</b>​ ") == "Storm batters the coast"
    assert clean_title("Woman says she 'closed eyes'") == "Woman says she ‘closed eyes’"
    long = clean_title("word " * 60)
    assert len(long) <= 150 and long.endswith("…") and not long.endswith(" …")


def test_parse_fixture_feeds() -> None:
    for url, name in ((BBC, "BBC"), (NYT, "NYT"), (GUARDIAN, "Guardian")):
        entries = data.parse_feed((FIX / BODIES[url]).read_bytes(), data.Feed(url))
        assert 10 <= len(entries) <= 15
        assert {e.source for e in entries} == {name}
        assert all(e.title and e.link.startswith("https://") for e in entries)
        assert all(e.published is not None and e.published.tzinfo for e in entries)
        assert all("  " not in e.title and "&amp;" not in e.title for e in entries)
        # BBC's "Watch:" video items are not headlines
        assert not any(e.title.lower().startswith("watch:") for e in entries)


def test_parse_atom_fixture_normalises_titles() -> None:
    entries = data.parse_feed((FIX / "atom.xml").read_bytes(), data.Feed(ATOM))
    titles = [e.title for e in entries]
    assert titles[0] == "Markets rally as central bank holds rates steady"
    assert "Q&A: what’s next for the “harbour deal”" in titles
    assert "Storm batters the coast as thousands lose power" in titles
    assert not any("Volcano" in t for t in titles)  # "Watch:" item dropped
    assert any(t.endswith("…") and len(t) <= 150 for t in titles)
    assert {e.source for e in entries} == {"Example Wire"}
    undated = [e for e in entries if "library" in e.title]
    assert undated and undated[0].published is None


def test_parse_rejects_non_feeds() -> None:
    with pytest.raises(ValueError, match="not a feed"):
        data.parse_feed(b"<html><body>Service unavailable", data.Feed(BBC))


def test_relative_time() -> None:
    rt = data.relative_time
    assert rt(None, NOW) == ""
    assert rt(NOW + timedelta(minutes=3), NOW) == "just now"  # clock skew
    assert rt(NOW - timedelta(seconds=30), NOW) == "just now"
    assert rt(NOW - timedelta(minutes=35), NOW) == "35 min ago"
    assert rt(NOW - timedelta(hours=5, minutes=59), NOW) == "5 h ago"
    assert rt(NOW - timedelta(hours=50), NOW) == "2 d ago"


# --- selection ------------------------------------------------------------------------------


def _entries(*urls: str) -> list[data.Entry]:
    out: list[data.Entry] = []
    for url in urls:
        out += data.parse_feed((FIX / BODIES[url]).read_bytes(), data.Feed(url))
    return out


def test_interleave_round_robin_newest_first() -> None:
    entries = _entries(BBC, NYT, GUARDIAN)
    picked = data.select(
        entries, [BBC, NYT, GUARDIAN], count=6, max_age_hours=36, mix="interleave", now=NOW
    )
    assert [e.source for e in picked] == ["BBC", "NYT", "Guardian"] * 2
    for src in ("BBC", "NYT", "Guardian"):
        times = [e.published for e in picked if e.source == src]
        assert times == sorted(times, reverse=True)
        newest = max(e.published for e in entries if e.source == src)
        assert times[0] == newest


def test_by_feed_and_count() -> None:
    entries = _entries(BBC, NYT)
    picked = data.select(entries, [NYT, BBC], count=4, max_age_hours=72, mix="by_feed", now=NOW)
    assert len(picked) == 4 and {e.source for e in picked} == {"NYT"}


def test_max_age_dedupe_and_undated() -> None:
    entries = _entries(ATOM)
    picked = data.select(entries, [ATOM], count=8, max_age_hours=36, mix="interleave", now=ATOM_NOW)
    titles = [e.title for e in picked]
    assert not any("Old news" in t for t in titles)  # four days old
    assert sum("Markets rally" in t for t in titles) == 1  # near-duplicate dropped
    assert titles[-1].startswith("Undated")  # kept, after the dated ones
    # nothing recent: a fresh set gives nothing, a stale set keeps its old stories
    later = ATOM_NOW + timedelta(days=10)
    kw = dict(count=5, max_age_hours=1, mix="interleave", now=later)
    no_undated = [e for e in entries if e.published]
    assert data.select(no_undated, [ATOM], **kw) == []
    assert len(data.select(no_undated, [ATOM], stale=True, **kw)) == 5


def test_near_duplicate() -> None:
    sig = data._signature
    assert data.near_duplicate(
        sig("Markets rally as rates hold"), sig("Markets rally as rates hold!")
    )
    assert data.near_duplicate(
        sig("France officials acknowledge failure around riots"),
        sig("French officials acknowledge ‘failure’ around riots"),
    )
    assert not data.near_duplicate(
        sig("Why are there protests in France?"), sig("France officials acknowledge failure")
    )


# --- cache ------------------------------------------------------------------------------------


def test_fetch_writes_cache_and_reuses_it_for_15_minutes(tmp_path: Path, feeds: Feeds) -> None:
    state = state_of(tmp_path)
    assert sorted(feeds.calls) == sorted(headlines.DEFAULT_FEEDS)
    assert not state.stale and state.sources == ["BBC", "NYT", "Guardian"]
    cached = json.loads((tmp_path / "headlines.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(cached["fetched_at"]) == NOW
    assert cached["feeds"] == list(headlines.DEFAULT_FEEDS)
    assert len(cached["entries"]) == len(state.entries) > 30
    assert set(cached["entries"][0]) == {"title", "source", "link", "published", "feed"}

    feeds.calls.clear()
    again = state_of(tmp_path, now=NOW + timedelta(minutes=14))
    assert feeds.calls == [] and again.entries == state.entries and not again.stale
    state_of(tmp_path, now=NOW + timedelta(minutes=16))
    assert len(feeds.calls) == 3


def test_changed_feed_list_refetches(tmp_path: Path, feeds: Feeds) -> None:
    state_of(tmp_path)
    feeds.calls.clear()
    state_of(tmp_path, settings={**DEFAULTS, "feeds": [BBC, ATOM]})
    assert sorted(feeds.calls) == sorted([BBC, ATOM])


def test_stale_cache_when_fetch_fails(tmp_path: Path, feeds: Feeds) -> None:
    fresh = state_of(tmp_path)
    feeds.down.add("*")
    later = NOW + timedelta(hours=3)
    stale = state_of(tmp_path, now=later)
    assert stale.stale and stale.entries == fresh.entries and stale.fetched_at == NOW
    assert status_line(stale, later) == "OFFLINE · CACHED 20:05"
    assert status_line(stale, later + timedelta(days=1)) == "OFFLINE · CACHED 19 OCT 20:05"
    # offline (network not allowed) behaves the same, any age of cache
    offline = state_of(tmp_path, now=NOW + timedelta(days=9), online=False)
    assert offline.stale and offline.entries == fresh.entries
    assert status_line(fresh, NOW) == ""


def test_partial_failure_keeps_cached_stories_of_the_failed_feed(
    tmp_path: Path, feeds: Feeds
) -> None:
    state_of(tmp_path)
    feeds.down.add(NYT)
    state = state_of(tmp_path, now=NOW + timedelta(minutes=20))
    assert not state.stale and state.failed == [NYT]
    assert {e.source for e in state.entries} == {"BBC", "NYT", "Guardian"}


def test_corrupt_cache_is_ignored(tmp_path: Path, feeds: Feeds) -> None:
    (tmp_path / "headlines.json").write_text("{not json", encoding="utf-8")
    assert data.read_cache(tmp_path) is None
    assert not state_of(tmp_path).stale
    assert data.read_cache(tmp_path) is not None  # rewritten


# --- network helper ----------------------------------------------------------------------------


def _mock_httpx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real = httpx.Client

    def client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(data.httpx, "Client", client)


def test_fetch_feed_sends_user_agent_and_checks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        if request.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(200, content=b"<rss/>")

    _mock_httpx(monkeypatch, handler)
    assert data.fetch_feed("https://example.org/rss", 5) == b"<rss/>"
    assert seen["ua"] == "Inkframe/0.1 (+https://github.com/inkframe)"
    with pytest.raises(httpx.HTTPStatusError):
        data.fetch_feed("https://example.org/missing", 5)
    monkeypatch.setattr(data, "MAX_BYTES", 3)
    with pytest.raises(ValueError, match="larger"):
        data.fetch_feed("https://example.org/rss", 5)


def test_fetch_all_has_an_overall_deadline() -> None:
    def slow(url: str, timeout: float) -> bytes:
        time.sleep(1.5)
        return b""

    t = time.monotonic()
    results = data.fetch_all(["https://a.example", "https://b.example"], slow, 0.2)
    assert time.monotonic() - t < 1.2
    assert all(isinstance(v, TimeoutError) for v in results.values())


# --- rendering ----------------------------------------------------------------------------------

ACCENT = {0: RED, 1: YELLOW, 2: BLUE}


@pytest.mark.parametrize("variant", [0, 1, 2])
def test_every_variant_renders_from_fixtures(tmp_path: Path, feeds: Feeds, variant: int) -> None:
    frame = frame_of(tmp_path, variant=variant, online=True)
    counts = np.bincount(frame.ravel(), minlength=6)
    assert counts[ACCENT[variant]] > 300  # the variant's accent is there
    for other in {RED, YELLOW, BLUE} - {ACCENT[variant]}:
        assert counts[other] == 0  # and only that one (no offline note when fresh)
    assert counts[BLACK] > 5000  # plenty of type
    assert (tmp_path / "headlines.json").is_file()


@pytest.mark.parametrize("variant", [0, 1, 2])
@pytest.mark.parametrize("count", [3, 6, 8])
def test_layouts_stay_inside_the_margins(
    tmp_path: Path, feeds: Feeds, variant: int, count: int
) -> None:
    settings = {**DEFAULTS, "count": count, "feeds": [*headlines.DEFAULT_FEEDS, ATOM]}
    frame = frame_of(tmp_path, variant=variant, online=True, settings=settings)
    body = frame[90:] if variant == 2 else frame  # v2's masthead band is full bleed
    assert (body[:, :20] == WHITE).all() and (body[:, -20:] == WHITE).all()
    assert (frame[-16:] == WHITE).all()


def test_offline_with_cache_shows_the_red_note(tmp_path: Path, feeds: Feeds) -> None:
    fresh = {v: frame_of(tmp_path, variant=v, online=True) for v in range(3)}
    later = NOW + timedelta(hours=2)
    stale = {v: frame_of(tmp_path, variant=v, now=later) for v in range(3)}
    assert feeds.calls and len(feeds.calls) == 3  # one fetch, then cache only
    for v in range(3):
        # every layout puts "OFFLINE · CACHED 20:05" on the right half, in red
        assert (fresh[v][:, 300:] == RED).sum() == 0
        assert (stale[v][:, 300:] == RED).sum() > 150
    # a full six-story grid gives up a story so the colophon can carry the note
    six = {**DEFAULTS, "count": 6}
    yellow_fresh = (frame_of(tmp_path, variant=1, settings=six) == YELLOW).sum()
    yellow_stale = (frame_of(tmp_path, variant=1, settings=six, now=later) == YELLOW).sum()
    assert yellow_stale < 0.9 * yellow_fresh


def test_offline_without_cache_renders_the_designed_card(tmp_path: Path) -> None:
    frame = frame_of(tmp_path)  # online=False, empty cache: nothing at all
    assert (frame == RED).sum() > 200 and (frame == BLACK).sum() > 5000
    title, reason = headlines.empty_message(state_of(tmp_path, online=False), 36)
    assert (title, reason) == ("Nothing on the wire", "Offline - no cached headlines yet")


def test_unreachable_feeds_render_the_card(tmp_path: Path, feeds: Feeds) -> None:
    feeds.down.add("*")
    frame_of(tmp_path, online=True)
    state = state_of(tmp_path)
    assert state.reason == data.UNREACHABLE and len(state.failed) == 3
    assert headlines.empty_message(state, 36)[1] == "Feeds unreachable - will retry"


def test_no_valid_feed_renders_the_card(tmp_path: Path) -> None:
    settings = {**DEFAULTS, "feeds": ["not a url"]}
    frame_of(tmp_path, settings=settings)
    state = state_of(tmp_path, settings=settings)
    assert headlines.empty_message(state, 36) == ("No feeds yet", "Add a feed in Apps > Headlines")


def test_nothing_recent_renders_the_card(tmp_path: Path, feeds: Feeds) -> None:
    later = NOW + timedelta(days=5)
    settings = {**DEFAULTS, "max_age_hours": 1}
    frame_of(tmp_path, settings=settings, online=True, now=later)
    state = state_of(tmp_path, settings=settings, now=later)
    assert headlines.empty_message(state, 1) == ("A quiet news day", "No stories in the last 1 h")


def test_broken_loader_still_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(data, "load", boom)
    frame_of(tmp_path, online=True)


def test_partial_settings_and_naive_now(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, settings={"count": 3}, now=datetime(2026, 10, 19, 12, 0))
    render_to_frame(headlines.APP, ctx)


def test_render_time(tmp_path: Path, feeds: Feeds) -> None:
    frame_of(tmp_path, online=True, variant=0)  # warm the cache and the fonts
    for variant in range(3):
        t = time.perf_counter()
        frame_of(tmp_path, variant=variant)
        assert time.perf_counter() - t < 3.0


# --- typesetting ---------------------------------------------------------------------------------


def test_serif_face_sets_digits_in_lining_figures() -> None:
    look = headlines.Look(make_ctx(Path(".")))
    face = look.serif(44)
    runs = face.runs("Drone kills 80 in Sudan, 1.5m flee")
    assert [t for t, _ in runs] == ["Drone kills ", "80", " in Sudan, ", "1.5", "m flee"]
    assert runs[1][1] is face.digits and runs[0][1] is face.font
    assert Face(face.font).runs("190") == [("190", face.font)]


def test_short_forms_and_headline_setting() -> None:
    t = "Ukrainians dying because US restricting intelligence sharing, Zelensky adviser tells BBC"
    assert short_forms(t)[1] == "Ukrainians dying because US restricting intelligence sharing"
    assert short_forms("Spurs beat Arsenal, again") == ["Spurs beat Arsenal, again"]
    look = headlines.Look(make_ctx(Path(".")))
    face = look.face("sans", 20, "semibold")
    full = set_headline(t, face, 600, 2)
    assert " ".join(full) == t  # fits: untouched (balanced over two lines)
    # a measure where the short form fits two lines and the full title does not
    width = face.width(short_forms(t)[1]) // 2 + 40
    assert face.width(t) > 2 * width
    short = set_headline(t, face, width, 2)
    assert " ".join(short) == short_forms(t)[1]
    clamped = set_headline(t, face, 150, 2)
    assert len(clamped) == 2 and clamped[-1].endswith("…")
    assert all(face.width(line) <= 150 for line in clamped)


def test_meta_line_gives_way_within_its_width() -> None:
    look = headlines.Look(make_ctx(Path(".")))
    bold, regular = look.font("mono", 14, "bold"), look.font("mono", 14)
    story = Story("x", "Al Jazeera Engl", "59 min ago")
    c = Canvas()
    assert meta(c, 0, 50, story, bold, regular, RED) > 200
    assert meta(c, 0, 80, story, bold, regular, RED, max_width=200) <= 200
    assert meta(c, 0, 110, story, bold, regular, RED, max_width=120) <= 120
