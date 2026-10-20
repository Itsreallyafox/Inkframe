"""Data side of the Headlines app: fetch, parse, cache and pick stories.

Nothing here draws (``headlines_layouts`` does) and the text clean-up lives in
``headlines_text``.  The flow for one render is

    feeds setting -> parse_feed_lines -> [Feed]
    cache fresh (15 min, same feed list)?  -> entries from cache
    else fetch every feed in parallel (httpx, overall deadline) -> parse_feed
         all failed -> cached entries regardless of age (stale) -> or nothing
    select(): drop old stories, newest first per feed, interleave or feed
              by feed, drop near-duplicate titles, keep ``count``

``fetch`` is passed in by the app module (``inkframe.apps.headlines.fetch_feed``)
so tests can replace it without touching the network.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import feedparser
import httpx

from inkframe import USER_AGENT
from inkframe.apps.headlines_text import (
    clean_text,
    clean_title,
    is_media_item,
    short_source,
    source_name,
    strip_media_prefix,
)

log = logging.getLogger("inkframe.apps.headlines")

ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
)
CACHE_NAME = "headlines.json"
CACHE_VERSION = 1
#: reuse fetched entries for this long without touching the network
CACHE_WINDOW = timedelta(minutes=15)
#: feeds beyond this many are ignored (one render must stay quick)
MAX_FEEDS = 8
#: refuse feed bodies larger than this (a real RSS file is 10-300 KB)
MAX_BYTES = 3 * 1024 * 1024
#: entries kept per feed after parsing
MAX_PER_FEED = 30

FetchFn = Callable[[str, float], bytes]

#: the one-line reasons a card can give (the empty card shows them in red)
NO_FEEDS = "Add a feed in Apps > Headlines"
OFFLINE_EMPTY = "Offline - no cached headlines yet"
UNREACHABLE = "Feeds unreachable - will retry"


# --- model ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    """One configured feed: its URL and an optional label the user typed before it."""

    url: str
    label: str | None = None


@dataclass(frozen=True)
class Entry:
    """A normalised story."""

    title: str
    source: str
    link: str
    published: datetime | None
    feed: str

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "link": self.link,
            "published": self.published.isoformat() if self.published else None,
            "feed": self.feed,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Entry:
        published = _parse_iso(d.get("published"))
        return cls(
            title=str(d["title"]),
            source=str(d.get("source") or ""),
            link=str(d.get("link") or ""),
            published=published,
            feed=str(d.get("feed") or ""),
        )


@dataclass
class FeedState:
    """What the renderer gets: entries plus how fresh and complete they are."""

    entries: list[Entry] = field(default_factory=list)
    #: when ``entries`` were fetched (``None`` if never)
    fetched_at: datetime | None = None
    #: ``True`` when ``entries`` come from an old cache because fetching failed or
    #: was not allowed; the card says so
    stale: bool = False
    #: one short line explaining an empty or stale state
    reason: str = ""
    #: short source labels of the configured feeds, in order
    sources: list[str] = field(default_factory=list)
    #: feed URLs that failed on the last attempt
    failed: list[str] = field(default_factory=list)


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- feed list ------------------------------------------------------------------------


def parse_feed_lines(lines: Iterable[str]) -> list[Feed]:
    """``"https://..."`` or ``"Label https://..."`` per line -> feeds (deduplicated,
    non-http entries skipped, at most ``MAX_FEEDS``)."""
    if isinstance(lines, str):  # a textarea value that skipped validation
        lines = lines.splitlines()
    feeds: list[Feed] = []
    seen: set[str] = set()
    for line in lines:
        parts = str(line).split()
        if not parts:
            continue
        url = parts[-1]
        if not re.match(r"^https?://[^/\s]+", url, re.IGNORECASE):
            log.warning("headlines: ignoring feed %r (not an http(s) URL)", line)
            continue
        label = " ".join(parts[:-1]).strip(" |:=-–—") or None
        if url in seen:
            continue
        seen.add(url)
        feeds.append(Feed(url, label))
    if len(feeds) > MAX_FEEDS:
        log.warning("headlines: %d feeds configured; using the first %d", len(feeds), MAX_FEEDS)
    return feeds[:MAX_FEEDS]


# --- parsing --------------------------------------------------------------------------


def entry_time(e: Any) -> datetime | None:
    """Publication (else update) time of a feedparser entry, UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = e.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def parse_feed(data: bytes, feed: Feed) -> list[Entry]:
    """Parse an RSS/Atom body into normalised entries (feed order).

    Raises ``ValueError`` when the body is not a feed at all."""
    d = feedparser.parse(data)
    if not d.entries:
        if d.get("bozo"):
            raise ValueError(f"not a feed: {d.get('bozo_exception')!s:.120}")
        return []
    feed_title = clean_text(d.feed.get("title", ""))
    source = feed.label or source_name(feed.url, feed_title)
    aliases = {source, feed_title, source_name(feed.url), *feed_title.split(" | ")}
    stories: list[Entry] = []
    media: list[Entry] = []
    for e in d.entries[: MAX_PER_FEED * 2]:
        # aggregators (Google News, Feedly...) name the real publisher per entry
        entry_source = "" if feed.label else clean_text((e.get("source") or {}).get("title", ""))
        title = clean_title(e.get("title", ""), aliases | {entry_source})
        if not title:
            continue
        item = Entry(
            title=strip_media_prefix(title),
            source=short_source(entry_source) if entry_source else source,
            link=str(e.get("link") or ""),
            published=entry_time(e),
            feed=feed.url,
        )
        (media if is_media_item(title) else stories).append(item)
    # a feed of nothing but videos still beats an empty card
    return (stories or media)[:MAX_PER_FEED]


# --- network --------------------------------------------------------------------------


def fetch_feed(url: str, timeout: float) -> bytes:
    """GET one feed body with httpx: our User-Agent, redirects followed, a size
    cap and a hard wall-clock deadline of ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f"feed larger than {MAX_BYTES // 1024} KB")
                if time.monotonic() > deadline:
                    raise TimeoutError("feed download exceeded its deadline")
                chunks.append(chunk)
    return b"".join(chunks)


def fetch_all(urls: Sequence[str], fetch: FetchFn, timeout: float) -> dict[str, Any]:
    """Fetch ``urls`` in parallel.  Returns ``{url: bytes | Exception}``; never waits
    much longer than ``timeout`` in total."""
    results: dict[str, Any] = {}
    if not urls:
        return results
    pool = ThreadPoolExecutor(max_workers=min(len(urls), MAX_FEEDS), thread_name_prefix="feeds")
    try:
        futures = {pool.submit(fetch, url, timeout): url for url in urls}
        done, pending = wait(futures, timeout=timeout + 0.5)
        for fut in done:
            try:
                results[futures[fut]] = fut.result()
            except Exception as e:  # noqa: BLE001 - any failure is "this feed failed"
                results[futures[fut]] = e
        for fut in pending:
            results[futures[fut]] = TimeoutError(f"no answer within {timeout:g} s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# --- cache ----------------------------------------------------------------------------


@dataclass
class Cached:
    fetched_at: datetime
    feeds: list[str]
    entries: list[Entry]


def cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / CACHE_NAME


def read_cache(cache_dir: Path) -> Cached | None:
    """The cached entries, or ``None`` if missing/corrupt."""
    path = cache_path(cache_dir)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = _parse_iso(d["fetched_at"])
        if fetched_at is None:
            raise ValueError("bad fetched_at")
        entries = [Entry.from_json(e) for e in d.get("entries", []) if e.get("title")]
        return Cached(fetched_at, [str(u) for u in d.get("feeds", [])], entries)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("headlines: ignoring unreadable cache %s: %s", path, e)
        return None


def write_cache(
    cache_dir: Path, fetched_at: datetime, feeds: Sequence[str], entries: Sequence[Entry]
) -> None:
    """Atomically write the cache file (best effort: a full disk only logs)."""
    path = cache_path(cache_dir)
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "feeds": list(feeds),
        "entries": [e.to_json() for e in entries],
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("headlines: could not write cache %s: %s", path, e)


def _for_feeds(entries: list[Entry], urls: Sequence[str]) -> list[Entry]:
    """Entries of the configured feeds; everything if none match (better than nothing)."""
    wanted = set(urls)
    mine = [e for e in entries if e.feed in wanted]
    return mine or entries


# --- loading --------------------------------------------------------------------------


def feed_names(feeds: Sequence[Feed], entries: Sequence[Entry]) -> list[str]:
    """Short names of ``feeds`` in order: the user's label, else the name most of
    the feed's entries carry, else one derived from the URL."""
    names = []
    for f in feeds:
        counts = Counter(e.source for e in entries if e.feed == f.url and e.source)
        names.append(f.label or (counts.most_common(1)[0][0] if counts else source_name(f.url)))
    return names


def load(
    feeds: Sequence[Feed],
    *,
    now: datetime,
    cache_dir: Path,
    online: bool,
    timeout: float,
    fetch: FetchFn,
) -> FeedState:
    """Entries for ``feeds`` from the cache window, the network, or a stale cache.

    Never raises for network trouble: the result says what it is (``stale``,
    ``reason``) and the card shows that."""
    if not feeds:
        return FeedState(reason=NO_FEEDS)
    urls = [f.url for f in feeds]
    cached = read_cache(cache_dir)
    if cached is not None:
        age = now - cached.fetched_at
        if cached.feeds == urls and timedelta(0) <= age < CACHE_WINDOW:
            return FeedState(
                cached.entries, cached.fetched_at, sources=feed_names(feeds, cached.entries)
            )

    def from_cache(failed: list[str]) -> FeedState:
        if cached is None:
            reason = UNREACHABLE if online else OFFLINE_EMPTY
            return FeedState(reason=reason, sources=feed_names(feeds, []), failed=failed)
        entries = _for_feeds(cached.entries, urls)
        return FeedState(
            entries,
            cached.fetched_at,
            stale=True,
            reason="offline",
            sources=feed_names(feeds, entries),
            failed=failed,
        )

    if not online:
        return from_cache([])

    results = fetch_all(urls, fetch, timeout)
    entries: list[Entry] = []
    failed: list[str] = []
    for feed in feeds:
        body = results.get(feed.url)
        if not isinstance(body, (bytes, bytearray)):
            log.warning("headlines: %s failed: %s", feed.url, body)
            failed.append(feed.url)
            continue
        try:
            entries.extend(parse_feed(bytes(body), feed))
        except Exception as e:  # noqa: BLE001 - a broken feed must not sink the others
            log.warning("headlines: %s did not parse: %s", feed.url, e)
            failed.append(feed.url)

    if len(failed) == len(feeds):
        return from_cache(failed)
    if failed and cached is not None:
        # keep the last good stories of a feed that is down right now
        entries.extend(e for e in cached.entries if e.feed in failed)
    write_cache(cache_dir, now, urls, entries)
    return FeedState(entries, now, sources=feed_names(feeds, entries), failed=failed)


# --- selection ------------------------------------------------------------------------

_STOP = frozenset(
    "a an the and or of to in on at for by with from as is are was were be been it its "
    "this that after over into says say said new".split()
)


def _signature(title: str) -> list[str]:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in _STOP]


def near_duplicate(a: Sequence[str], b: Sequence[str]) -> bool:
    """Two headline signatures that tell the same story in (nearly) the same words."""
    if not a or not b:
        return list(a) == list(b)
    sa, sb = set(a), set(b)
    common = len(sa & sb)
    if common / len(sa | sb) >= 0.6:
        return True
    if min(len(sa), len(sb)) >= 4 and common / min(len(sa), len(sb)) >= 0.85:
        return True
    return SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= 0.85


def _newest_first(entries: list[Entry]) -> list[Entry]:
    dated = [e for e in entries if e.published is not None]
    undated = [e for e in entries if e.published is None]
    dated.sort(key=lambda e: e.published, reverse=True)  # type: ignore[arg-type,return-value]
    return dated + undated


def select(
    entries: Sequence[Entry],
    feed_order: Sequence[str],
    *,
    count: int,
    max_age_hours: float,
    mix: str,
    now: datetime,
    stale: bool = False,
) -> list[Entry]:
    """Pick up to ``count`` stories.

    Stories older than ``max_age_hours`` are dropped (undated ones are kept);
    a stale set that would end up empty keeps its old stories instead, since
    showing what we last had beats a blank card.  ``mix="interleave"`` takes one
    story per feed in turn; ``"by_feed"`` exhausts the first feed before the
    next.  Near-identical titles are shown once.
    """
    limit = timedelta(hours=float(max_age_hours))
    fresh = [e for e in entries if e.published is None or now - e.published <= limit]
    if not fresh and stale:
        fresh = list(entries)
    order = list(dict.fromkeys(list(feed_order) + [e.feed for e in fresh]))
    groups = {url: _newest_first([e for e in fresh if e.feed == url]) for url in order}
    if mix == "by_feed":
        ordered = [e for url in order for e in groups[url]]
    else:
        ordered = []
        depth = max((len(g) for g in groups.values()), default=0)
        for i in range(depth):
            ordered.extend(groups[url][i] for url in order if i < len(groups[url]))
    picked: list[Entry] = []
    sigs: list[list[str]] = []
    links: set[str] = set()
    for e in ordered:
        sig = _signature(e.title)
        if (e.link and e.link in links) or any(near_duplicate(sig, s) for s in sigs):
            continue
        picked.append(e)
        sigs.append(sig)
        if e.link:
            links.add(e.link)
        if len(picked) >= count:
            break
    return picked


def relative_time(published: datetime | None, now: datetime) -> str:
    """``"just now"``, ``"35 min ago"``, ``"5 h ago"``, ``"3 d ago"`` (``""`` if unknown)."""
    if published is None:
        return ""
    secs = (now - published).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 48 * 3600:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"
