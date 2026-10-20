"""The no-build web UI in inkframe/static: references resolve, the size
budget holds (plan section 7: under 300 KB of HTML+CSS+JS), and the ES
modules hang together (every relative import names a file that exists and
every imported name is exported there).  There is no JavaScript engine in
the test environment, so the module checks are static."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inkframe.paths import PACKAGE_DIR, STATIC_DIR

FONTS_DIR = PACKAGE_DIR / "fonts"
INDEX = STATIC_DIR / "index.html"
BUDGET_BYTES = 300 * 1024

_REF = re.compile(r"""\b(?:src|href)=["']([^"'#]+)["']""")
_IMPORT = re.compile(
    r"""^\s*import\s+(?:\{(?P<names>[^}]*)\}\s+from\s+)?["'](?P<spec>[^"']+)["'];""",
    re.MULTILINE,
)
_EXPORT = re.compile(r"""^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+(\w+)""", re.M)
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")


def _ui_files() -> list[Path]:
    return sorted(p for p in STATIC_DIR.rglob("*") if p.suffix in {".html", ".css", ".js"})


def _modules() -> list[Path]:
    return sorted(STATIC_DIR.rglob("*.js"))


def _local(url: str) -> Path | None:
    """The file behind a same-origin URL the page references, or None for a
    data: URL (the favicon) or an in-page anchor."""
    if url.startswith("data:"):
        return None
    if url.startswith("/static/"):
        return STATIC_DIR / url.removeprefix("/static/")
    if url.startswith("/fonts/"):
        return FONTS_DIR / url.removeprefix("/fonts/")
    raise AssertionError(f"unexpected reference {url!r}: the UI must not load anything external")


def test_index_references_exist() -> None:
    html = INDEX.read_text(encoding="utf-8")
    refs = [r for r in _REF.findall(html)]
    assert "/static/app.css" in refs and "/static/app.js" in refs
    for ref in refs:
        path = _local(ref)
        if path is not None:
            assert path.is_file(), f"index.html references {ref}, which does not exist"
    assert '<script type="module" src="/static/app.js">' in html
    assert "<title>Inkframe</title>" in html
    # the favicon is an inline SVG data URL (six tiny squares)
    assert 'rel="icon" type="image/svg+xml" href="data:image/svg+xml,' in html


def test_css_urls_exist() -> None:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for url in _CSS_URL.findall(css):
        path = _local(url)
        if path is not None:
            assert path.is_file(), f"app.css references {url}, which does not exist"
    # at most two font files are loaded (the plan's static budget)
    fonts = {u for u in _CSS_URL.findall(css) if u.startswith("/fonts/")}
    assert 1 <= len(fonts) <= 2


def test_static_budget() -> None:
    total = sum(p.stat().st_size for p in _ui_files())
    assert total < BUDGET_BYTES, f"HTML+CSS+JS is {total} bytes; the budget is {BUDGET_BYTES}"


@pytest.mark.parametrize("path", _ui_files(), ids=lambda p: p.relative_to(STATIC_DIR).as_posix())
def test_no_debug_logging_or_external_code(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "console.log(" not in text
    assert "debugger;" not in text
    # nothing from a CDN: the frame may be offline (the SVG namespace is not a fetch)
    for m in re.finditer(r"https?://[^\s\"'<>)]+", text):
        url = m.group(0)
        assert url.startswith("http://www.w3.org/"), f"{path.name} refers to {url}"


def test_app_js_is_an_es_module() -> None:
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert _IMPORT.search(src), "app.js should import its views as ES modules"
    assert "require(" not in src and "module.exports" not in src
    # balanced brackets (comments and strings in this file hold none)
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r'"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`', '""', code)
    for open_, close in ("()", "[]", "{}"):
        assert code.count(open_) == code.count(close), f"unbalanced {open_}{close} in app.js"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(STATIC_DIR).as_posix())
def test_module_imports_resolve(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    for m in _IMPORT.finditer(src):
        spec = m.group("spec")
        assert spec.startswith("./") and spec.endswith(".js"), f"{path.name}: import {spec!r}"
        target = (path.parent / spec).resolve()
        assert target.is_file(), f"{path.name} imports {spec}, which does not exist"
        exported = set(_EXPORT.findall(target.read_text(encoding="utf-8")))
        for name in filter(None, (n.strip() for n in (m.group("names") or "").split(","))):
            name = name.split(" as ")[0].strip()
            assert name in exported, f"{path.name} imports {name!r} from {spec}; not exported"


def test_static_files_are_served(client: TestClient) -> None:
    for path in _ui_files():
        rel = path.relative_to(STATIC_DIR).as_posix()
        url = "/" if rel == "index.html" else f"/static/{rel}"
        r = client.get(url)
        assert r.status_code == 200, url
    font = client.get("/fonts/playfair-display/PlayfairDisplay-Regular.ttf")
    assert font.status_code == 200 and "max-age=31536000" in font.headers["cache-control"]
