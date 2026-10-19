"""Text side of the Headlines app: masthead names and headline normalisation.

Pure functions, no I/O: ``source_name`` turns a feed URL (and its title) into
a short label such as "BBC" or "Guardian"; ``clean_title`` turns whatever a
feed put in ``<title>`` into a headline fit to print (entities decoded, tags
and invisible characters dropped, whitespace collapsed, a trailing
"- Publisher" removed, typographic quotes, length capped).
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlparse

#: titles longer than this are cut at a word boundary
MAX_TITLE = 150
#: source labels longer than this are cut
MAX_SOURCE = 14

# --- source names ---------------------------------------------------------------------

#: host suffix -> short masthead name
KNOWN_SOURCES: tuple[tuple[str, str], ...] = (
    ("bbc.co.uk", "BBC"),
    ("bbci.co.uk", "BBC"),
    ("bbc.com", "BBC"),
    ("nytimes.com", "NYT"),
    ("theguardian.com", "Guardian"),
    ("guardian.co.uk", "Guardian"),
    ("npr.org", "NPR"),
    ("reuters.com", "Reuters"),
    ("apnews.com", "AP"),
    ("aljazeera.com", "Al Jazeera"),
    ("washingtonpost.com", "WaPo"),
    ("wsj.com", "WSJ"),
    ("dj.com", "WSJ"),
    ("cnn.com", "CNN"),
    ("ft.com", "FT"),
    ("economist.com", "Economist"),
    ("bloomberg.com", "Bloomberg"),
    ("dw.com", "DW"),
    ("france24.com", "France 24"),
    ("euronews.com", "Euronews"),
    ("abc.net.au", "ABC"),
    ("cbc.ca", "CBC"),
    ("latimes.com", "LA Times"),
    ("theatlantic.com", "Atlantic"),
    ("politico.com", "Politico"),
    ("politico.eu", "Politico"),
    ("axios.com", "Axios"),
    ("theverge.com", "Verge"),
    ("arstechnica.com", "Ars"),
    ("ycombinator.com", "HN"),
    ("hnrss.org", "HN"),
    ("spiegel.de", "Spiegel"),
    ("lemonde.fr", "Le Monde"),
    ("nos.nl", "NOS"),
    ("independent.co.uk", "Independent"),
    ("telegraph.co.uk", "Telegraph"),
    ("sky.com", "Sky"),
    ("cbsnews.com", "CBS"),
    ("nbcnews.com", "NBC"),
    ("abcnews.go.com", "ABC"),
    ("pbs.org", "PBS"),
    ("propublica.org", "ProPublica"),
)

#: publisher names a title may end with ("Headline - Reuters")
KNOWN_PUBLISHERS = frozenset(
    "associated press|ap|ap news|bbc|bbc news|the new york times|new york times|nyt|"
    "the guardian|guardian|npr|reuters|al jazeera|al jazeera english|the washington post|"
    "washington post|cnn|financial times|bloomberg|the economist|politico|axios|the verge|"
    "dw|france 24|euronews|sky news|cbs news|nbc news|abc news|the independent|"
    "the telegraph|wsj|the wall street journal".split("|")
)

#: words that describe a section rather than a publisher ("World news | The Guardian")
_GENERIC_WORDS = frozenset(
    "news world top stories story home homepage page latest headlines rss feed feeds "
    "topics breaking international edition front online all the".split()
)


def short_source(name: str) -> str:
    """Drop a leading "The" and cap the length."""
    name = re.sub(r"^the\s+", "", name.strip(), flags=re.IGNORECASE)
    if len(name) > MAX_SOURCE:
        name = name[:MAX_SOURCE].rstrip()
    return name


def source_name(url: str, feed_title: str = "") -> str:
    """A short masthead name for a feed: known host, else the brand part of the
    feed title, else the host name."""
    host = (urlparse(url).hostname or "").lower()
    for suffix, name in KNOWN_SOURCES:
        if host == suffix or host.endswith("." + suffix):
            return name
    if feed_title:
        for part in re.split(r"\s+[|>\-–—:]\s+|:\s+|\s*\|\s*", feed_title):
            words = [w for w in part.split() if w.lower().strip(".,") not in _GENERIC_WORDS]
            if words:
                return short_source(" ".join(words))
    parts = [p for p in host.split(".") if p not in ("www", "feeds", "rss", "feed", "news")]
    if len(parts) >= 2:
        return short_source(parts[-2].capitalize())
    return short_source(parts[0].capitalize()) if parts else "News"


# --- headline text --------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_SPACES = re.compile(r"[\s\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_SUFFIX = re.compile(r"\s+(?:-|–|—|\|)\s+([^|–—]{1,48}?)\s*$")
#: a live blog's tail: "... - live", "... – Europe live", "... – US politics live"
_LIVE_SUFFIX = re.compile(r"\s+[-–—]\s+(?:[\w.’'-]+\s+){0,4}live(?:\s+updates)?$", re.IGNORECASE)
_MEDIA_PREFIX = re.compile(
    r"^(?:watch(?: live)?|listen|video|audio|podcast|quiz|in pictures|in pics|"
    r"pictured|gallery)\s*[:|]\s*",
    re.IGNORECASE,
)
_MEDIA_SUFFIX = re.compile(
    r"\s+(?:-|–|—)\s+(?:video|podcast|audio|in pictures|quiz|photo essay|cartoon)$",
    re.IGNORECASE,
)


def clean_text(raw: str) -> str:
    """Decode entities (also double-encoded ones), drop tags and invisible
    characters, collapse whitespace."""
    s = str(raw or "")
    for _ in range(3):
        un = html.unescape(s)
        if un == s:
            break
        s = un
    s = _TAG.sub(" ", s)
    s = s.translate(_INVISIBLE)
    s = unicodedata.normalize("NFC", s)
    return _SPACES.sub(" ", s).strip()


def smart_quotes(s: str) -> str:
    """Straight quotes -> typographic ones (the fonts have them; they read better)."""
    s = re.sub(r"(^|[\s(\[{\u2014\u2013-])'", "\\1\u2018", s)
    s = s.replace("'", "\u2019")
    s = re.sub(r'(^|[\s(\[{\u2014\u2013-])"', "\\1\u201c", s)
    return s.replace('"', "\u201d")


def cap_length(s: str, limit: int = MAX_TITLE) -> str:
    """``s`` cut at a word boundary (plus an ellipsis) when longer than ``limit``."""
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-–—") + "…"


def clean_title(raw: str, publisher_names: Iterable[str] = ()) -> str:
    """Normalise a headline: entities, tags, whitespace, a trailing " - Source"
    suffix naming the publisher, straight quotes, length cap."""
    s = clean_text(raw)
    names = {n.lower() for n in publisher_names if n} | KNOWN_PUBLISHERS
    m = _SUFFIX.search(s)
    if m and m.group(1).strip().lower().strip(".") in names and len(s[: m.start()]) >= 12:
        s = s[: m.start()].rstrip()
    live = _LIVE_SUFFIX.search(s)
    if live and live.start() >= 12:
        s = s[: live.start()].rstrip()
    return cap_length(smart_quotes(s))


def is_media_item(title: str) -> bool:
    """Video, audio, galleries and quizzes: not headlines a frame can show."""
    return bool(_MEDIA_PREFIX.match(title) or _MEDIA_SUFFIX.search(title))


def strip_media_prefix(title: str) -> str:
    """``"Watch: Volcano erupts"`` -> ``"Volcano erupts"``."""
    return _MEDIA_PREFIX.sub("", title) or title
