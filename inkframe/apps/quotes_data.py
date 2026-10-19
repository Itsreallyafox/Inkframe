"""Data side of the Quotes app: the bundled library, selection, and the caches.

Kept apart from ``inkframe.apps.quotes`` (layout and the ``App``) so each file
stays readable.  Nothing here draws.

* The library is ``inkframe/apps/data/quotes.json``: public-domain quotations
  (authors who died before 1928, or classical sources in old translations),
  each with ``text`` (``\\n`` marks a verse line break), ``author``,
  ``source`` and ``tags``.
* ``recent.json`` in the app's cache dir remembers the last ``RECENT_SIZE``
  quote ids so a refresh never repeats a recent quote.
* ``zenquotes.json`` holds the last quote fetched from ZenQuotes, with its
  ``fetched_at`` time, so an offline frame can still show it.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import random
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("inkframe.apps.quotes")

DATA_FILE = Path(__file__).resolve().parent / "data" / "quotes.json"

#: the themes a bundled quote can carry (the ``tags`` setting filters by them)
TAGS: tuple[str, ...] = (
    "wisdom",
    "nature",
    "courage",
    "humour",
    "love",
    "work",
    "time",
    "art",
    "science",
    "stoic",
)
#: spellings people type that mean one of ``TAGS``
TAG_ALIASES = {
    "humor": "humour",
    "funny": "humour",
    "wit": "humour",
    "stoicism": "stoic",
    "stoics": "stoic",
    "philosophy": "wisdom",
    "brave": "courage",
    "bravery": "courage",
    "arts": "art",
    "poetry": "art",
}

#: ``length`` setting thresholds, in characters
SHORT_MAX = 90
LONG_MIN = 120

RECENT_FILE = "recent.json"
RECENT_SIZE = 20
ZEN_CACHE_FILE = "zenquotes.json"


class QuoteDataError(RuntimeError):
    """The bundled library is missing or unreadable."""


@dataclass(frozen=True)
class Quote:
    id: str
    text: str
    author: str
    source: str = ""
    tags: tuple[str, ...] = ()
    #: ``"local"`` (bundled) or ``"zenquotes"``
    origin: str = "local"


# --- text normalisation -----------------------------------------------------------

_SPACES = re.compile(r"[ \t ]+")


def clean_text(text: str) -> str:
    """Collapse runs of spaces, trim each line, drop blank lines; keep ``\\n``
    verse breaks."""
    lines = (_SPACES.sub(" ", line).strip() for line in text.replace("\r", "").split("\n"))
    return "\n".join(line for line in lines if line)


def normalise_author(name: str) -> str:
    """``"  marcus   aurelius. "`` -> ``"marcus aurelius"``: whitespace collapsed,
    stray trailing separators removed.  Case is left alone (``"van Gogh"``)."""
    name = _SPACES.sub(" ", name.replace("\n", " ")).strip()
    name = name.strip(" ,;:-—–")
    if name.endswith(".") and not re.search(r"\b[A-Z]\.$", name):  # keep "Jr." / initials
        name = name[:-1].rstrip()
    return name


def author_key(name: str) -> str:
    """Comparison key: two spellings of one author map to the same key."""
    return re.sub(r"[^a-z]", "", normalise_author(name).casefold())


def smarten(text: str) -> str:
    """Typographic punctuation for text from outside the bundled set."""
    text = text.replace("...", "…").replace("--", "—")
    out: list[str] = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else " "
        opening = prev.isspace() or prev in "([{—–“‘"
        if ch == '"':
            out.append("“" if opening else "”")
        elif ch == "'":
            nxt = text[i + 1] if i + 1 < len(text) else " "
            out.append("‘" if opening and nxt.isalpha() else "’")
        else:
            out.append(ch)
    return "".join(out)


def quote_id(text: str, author: str, prefix: str = "q") -> str:
    """Stable id from the normalised text and author (survives reformatting)."""
    key = " ".join(text.casefold().split()) + "|" + author_key(author)
    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"


# --- the bundled library --------------------------------------------------------------

_LIBRARY_CACHE: dict[tuple[str, int, int], tuple[Quote, ...]] = {}


def parse_library(data: Any) -> tuple[Quote, ...]:
    """Quotes from the JSON document (a list, or ``{"quotes": [...]}``).
    Entries without text or author are skipped; unknown tags are dropped."""
    items = data.get("quotes") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise QuoteDataError("quotes.json: expected a list of quotes")
    quotes: list[Quote] = []
    seen: set[str] = set()
    for n, item in enumerate(items):
        if not isinstance(item, dict):
            log.warning("quotes.json entry %d is not an object; skipped", n)
            continue
        text = clean_text(str(item.get("text") or ""))
        author = normalise_author(str(item.get("author") or ""))
        if not text or not author:
            log.warning("quotes.json entry %d has no text or author; skipped", n)
            continue
        qid = quote_id(text, author)
        if qid in seen:
            log.warning("quotes.json entry %d duplicates an earlier quote; skipped", n)
            continue
        seen.add(qid)
        raw_tags = item.get("tags") or []
        tags = tuple(dict.fromkeys(t for t in (normalise_tag(str(t)) for t in raw_tags) if t))
        source = clean_text(str(item.get("source") or "")).replace("\n", " ")
        quotes.append(Quote(qid, text, author, source, tags))
    return tuple(quotes)


def load_library(path: Path | None = None) -> tuple[Quote, ...]:
    """The bundled quotes (cached until the file changes).  Raises
    ``QuoteDataError`` when the file is missing, unreadable or empty."""
    path = Path(path or DATA_FILE)
    try:
        st = path.stat()
    except OSError as e:
        raise QuoteDataError(f"{path.name} is missing") from e
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _LIBRARY_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise QuoteDataError(f"{path.name} is unreadable ({e.__class__.__name__})") from e
    quotes = parse_library(data)
    if not quotes:
        raise QuoteDataError(f"{path.name} has no usable quotes")
    _LIBRARY_CACHE.clear()
    _LIBRARY_CACHE[key] = quotes
    return quotes


# --- selection ------------------------------------------------------------------------


def normalise_tag(tag: str) -> str | None:
    t = tag.strip().casefold()
    t = TAG_ALIASES.get(t, t)
    return t if t in TAGS else None


def length_ok(quote: Quote, length: str) -> bool:
    n = len(quote.text)
    if length == "short":
        return n <= SHORT_MAX
    if length == "long":
        return n >= LONG_MIN
    return True


def filter_pool(
    quotes: Sequence[Quote], tags: Iterable[str], length: str = "any"
) -> tuple[list[Quote], list[str]]:
    """The quotes matching the ``tags`` and ``length`` settings, plus a short
    note for the card when a filter matched nothing and was relaxed."""
    notes: list[str] = []
    wanted_raw = [t.strip() for t in tags if t and t.strip()]
    wanted = {t for t in (normalise_tag(t) for t in wanted_raw) if t}
    pool = list(quotes)
    if wanted_raw:
        tagged = [q for q in pool if wanted.intersection(q.tags)]
        if tagged:
            pool = tagged
        else:
            shown = ", ".join(wanted_raw[:2]) + ("…" if len(wanted_raw) > 2 else "")
            notes.append(f"No quotes tagged “{shown}” · showing all themes")
    if length != "any":
        sized = [q for q in pool if length_ok(q, length)]
        if sized:
            pool = sized
        else:
            notes.append(f"No {length} quotes here · showing any length")
    return pool, notes


def choose_quote(pool: Sequence[Quote], recent: Sequence[str], rng: random.Random) -> Quote:
    """A seeded pick from ``pool`` that avoids the ``recent`` ids (most recent
    last); with a pool smaller than the memory it only avoids the last one."""
    if not pool:
        raise ValueError("empty quote pool")
    recent_set = set(recent)
    candidates = [q for q in pool if q.id not in recent_set]
    if not candidates:
        last = recent[-1] if recent else None
        candidates = [q for q in pool if q.id != last] or list(pool)
    return rng.choice(candidates)


# --- small JSON caches -----------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        log.warning("ignoring unreadable cache %s: %s", path, e)
        return None


def _write_json(path: Path, data: Any) -> None:
    """Atomic write (temp file + replace); a failure is logged, not raised:
    the card still renders, it just may repeat a quote."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


def load_recent(cache_dir: Path) -> list[str]:
    data = _read_json(Path(cache_dir) / RECENT_FILE)
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)][-RECENT_SIZE:]


def remember(cache_dir: Path, recent: Sequence[str], qid: str, now: datetime) -> list[str]:
    """Append ``qid`` (moving it to the end if present), keep the last
    ``RECENT_SIZE``, persist and return the new list."""
    ids = [i for i in recent if i != qid] + [qid]
    ids = ids[-RECENT_SIZE:]
    _write_json(
        Path(cache_dir) / RECENT_FILE,
        {"ids": ids, "updated_at": now.astimezone(timezone.utc).isoformat()},
    )
    return ids


# --- ZenQuotes -------------------------------------------------------------------------


def parse_zenquotes(payload: Any) -> dict[str, str]:
    """``[{"q": text, "a": author, "h": html}]`` -> ``{"text", "author"}``.
    Raises ``ValueError`` for anything else, including the rate-limit notice
    ZenQuotes serves in place of a quote."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("unexpected ZenQuotes response")
    q, a = payload[0].get("q"), payload[0].get("a")
    if not isinstance(q, str) or not q.strip() or not isinstance(a, str):
        raise ValueError("ZenQuotes response has no quote")
    text, author = html.unescape(q).strip(), html.unescape(a).strip()
    if author.casefold() == "zenquotes.io" or text.casefold().startswith("too many requests"):
        raise ValueError("ZenQuotes rate limit reached")
    return {"text": text, "author": author or "Unknown"}


def zen_to_quote(raw: dict[str, str]) -> Quote:
    text = smarten(clean_text(raw["text"]))
    author = normalise_author(raw.get("author") or "") or "Unknown"
    return Quote(quote_id(text, author, prefix="zq"), text, author, "", (), "zenquotes")


@dataclass(frozen=True)
class ZenCache:
    quote: Quote
    fetched_at: datetime


def load_zen_cache(cache_dir: Path) -> ZenCache | None:
    data = _read_json(Path(cache_dir) / ZEN_CACHE_FILE)
    if not isinstance(data, dict):
        return None
    try:
        fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        quote = zen_to_quote(parse_zenquotes([data["quote"]]))
    except (KeyError, TypeError, ValueError) as e:
        log.warning("ignoring malformed ZenQuotes cache: %s", e)
        return None
    return ZenCache(quote, fetched_at)


def save_zen_cache(cache_dir: Path, raw: dict[str, str], now: datetime) -> None:
    _write_json(
        Path(cache_dir) / ZEN_CACHE_FILE,
        {
            "fetched_at": now.astimezone(timezone.utc).isoformat(),
            "quote": {"q": raw["text"], "a": raw["author"]},
        },
    )
