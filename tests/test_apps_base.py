"""The app contract: settings fields, the Canvas helper, render_to_frame."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

from inkframe.apps import APP_IDS, load_app
from inkframe.apps.base import (
    MAX_LIST_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_TEXT_CHARS,
    MAX_TEXTAREA_CHARS,
    App,
    Field,
    FontLibrary,
    RenderContext,
    coerce_field,
    render_to_frame,
    schema_defaults,
    validate_settings,
)
from inkframe.apps.draw import Canvas
from inkframe.display.palette import BLACK, BLUE, GREEN, RED, WHITE, YELLOW


def _font(size: int = 24) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _ctx(tmp_path: Path, **kw) -> RenderContext:
    return RenderContext(
        settings={},
        now=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        online=False,
        **kw,
    )


# --- fields ---------------------------------------------------------------------


def test_field_coercion_and_validation() -> None:
    n = Field("count", "Count", "number", 3, min=1, max=10, step=1)
    assert coerce_field(n, "7") == 7 and isinstance(coerce_field(n, 7.0), int)
    with pytest.raises(ValueError, match="count: must be at most"):
        coerce_field(n, 11)
    with pytest.raises(ValueError, match="whole number"):
        coerce_field(n, 2.5)
    b = Field("on", "On", "bool", True)
    assert coerce_field(b, "false") is False and coerce_field(b, True) is True
    s = Field("mode", "Mode", "select", "a", options=(("a", "A"), ("b", "B")))
    assert coerce_field(s, "b") == "b"
    with pytest.raises(ValueError, match="mode: must be one of a, b"):
        coerce_field(s, "c")
    lst = Field("feeds", "Feeds", "list", ["x"], required=True)
    assert coerce_field(lst, "a\n\n b \n") == ["a", "b"]
    with pytest.raises(ValueError, match="at least one"):
        coerce_field(lst, [])
    t = Field("name", "Name", "text", "", required=True)
    with pytest.raises(ValueError, match="name: required"):
        coerce_field(t, "   ")


def test_field_schema_bugs_fail_at_definition() -> None:
    with pytest.raises(ValueError):
        Field("x", "X", "select", "a")  # select without options
    with pytest.raises(ValueError):
        Field("n", "N", "number", 50, min=0, max=10)  # default out of range
    with pytest.raises(ValueError):
        Field("bad key", "K", "text", "")


def test_validate_settings_merges_defaults_and_rejects_unknown() -> None:
    schema = (
        Field("city", "City", "text", "Berlin"),
        Field("days", "Days", "number", 3, min=1, max=7, step=1),
    )
    assert schema_defaults(schema) == {"city": "Berlin", "days": 3}
    assert validate_settings(schema, {"days": "5"}) == {"city": "Berlin", "days": 5}
    with pytest.raises(ValueError, match="unknown setting"):
        validate_settings(schema, {"colour": "red"})
    d = schema[1].to_dict()
    assert d["min"] == 1 and d["type"] == "number"


def test_field_caps() -> None:
    assert (MAX_TEXT_CHARS, MAX_TEXTAREA_CHARS) == (500, 20_000)
    assert (MAX_LIST_ITEMS, MAX_LIST_ITEM_CHARS) == (100, 2000)
    t = Field("location", "Location", "text", "")
    assert coerce_field(t, "x" * 500) == "x" * 500
    assert coerce_field(t, "  " + "x" * 500 + "  ") == "x" * 500  # counted after stripping
    with pytest.raises(ValueError, match="location: at most 500 characters, got 501"):
        coerce_field(t, "x" * 501)
    with pytest.raises(ValueError, match="location: at most 500 characters"):
        coerce_field(t, "x" * 5_000_000)
    ta = Field("note", "Note", "textarea", "")
    assert len(coerce_field(ta, "x\n" * 10_000)) == 20_000
    with pytest.raises(ValueError, match="note: at most 20000 characters, got 20001"):
        coerce_field(ta, "x" * 20_001)
    lst = Field("feeds", "Feeds", "list", [])
    urls = [f"https://example.org/{i}.xml" for i in range(100)]
    assert coerce_field(lst, urls) == urls
    # blank entries do not count, as a pasted list ends with a newline or two
    assert coerce_field(lst, "\n\n".join(urls) + "\n\n") == urls
    with pytest.raises(ValueError, match="^feeds: at most 100 entries$"):
        coerce_field(lst, [*urls, "https://example.org/101.xml"])
    with pytest.raises(ValueError, match="^feeds: at most 100 entries$"):
        coerce_field(lst, "\n".join(f"https://example.org/{i}" for i in range(20_000)))
    assert coerce_field(lst, ["a", "x" * 2000]) == ["a", "x" * 2000]
    with pytest.raises(ValueError, match="feeds: entry 2 is 2001 characters long; at most 2000"):
        coerce_field(lst, ["a", "", "x" * 2001])
    # the caps also hold through validate_settings, and the UI is told them
    with pytest.raises(ValueError, match="feeds: at most 100 entries"):
        validate_settings((lst,), {"feeds": urls * 2})
    assert t.to_dict()["maxlength"] == 500 and ta.to_dict()["maxlength"] == 20_000
    d = lst.to_dict()
    assert d["max_items"] == 100 and d["maxlength"] == 2000
    assert "maxlength" not in Field("on", "On", "bool", True).to_dict()


def test_apps_share_one_user_agent() -> None:
    from inkframe import USER_AGENT, __version__
    from inkframe.apps import headlines_data, quotes, weather

    assert USER_AGENT == f"Inkframe/{__version__} (e-ink photo frame)"
    assert "github" not in USER_AGENT.lower()
    assert weather.USER_AGENT is headlines_data.USER_AGENT is quotes.USER_AGENT is USER_AGENT


# --- canvas -----------------------------------------------------------------------


def test_canvas_shapes_and_text_stay_in_palette() -> None:
    c = Canvas(background=WHITE)
    c.rect((10, 10, 200, 100), fill=YELLOW, outline=BLACK, width=3, radius=12)
    c.ellipse((300, 50, 400, 150), fill=RED)
    c.line([(0, 250), (599, 290)], fill=BLUE, width=5)
    c.polygon([(500, 300), (590, 390), (420, 390)], fill=GREEN)
    c.stripes((0, 300, 200, 400), BLACK, WHITE, period=8)
    c.dots((200, 300, 400, 400), RED, background=WHITE)
    c.checker((400, 300, 600, 400), BLUE, YELLOW)
    box = c.text((20, 150), "Inkframe Hg 0123", _font(40), BLACK)
    assert box[2] > box[0] and box[3] > box[1]
    frame = c.to_frame()
    assert frame.shape == (400, 600) and frame.dtype == np.uint8
    assert set(np.unique(frame).tolist()) <= set(range(6))
    # the text is crisp: only background and fill around it
    region = frame[box[1] : box[3], box[0] : box[2]]
    assert set(np.unique(region).tolist()) <= {WHITE, BLACK}
    assert (region == BLACK).sum() > 100


def test_canvas_text_anchor_and_empty() -> None:
    c = Canvas()
    assert c.text((50, 50), "", _font(), BLACK) == (50, 50, 50, 50)
    box = c.text((300, 200), "centred", _font(30), RED, anchor="mm")
    assert abs((box[0] + box[2]) / 2 - 300) < 3


def test_canvas_wrap_and_paragraph() -> None:
    c = Canvas()
    f = _font(20)
    words = "the quick brown fox jumps over the lazy dog " * 6
    lines = c.wrap(words.strip(), f, 300)
    assert len(lines) > 3 and all(c.text_width(line, f) <= 300 for line in lines)
    assert c.wrap("supercalifragilisticexpialidocious" * 3, f, 120)[0]  # split long word
    assert c.wrap("a\n\nb", f, 300) == ["a", "", "b"]
    y = c.paragraph((20, 20), words, f, BLACK, 300, max_lines=3)
    assert y > 20
    lh = c.line_height(f)
    assert y == 20 + 3 * lh


def test_canvas_fit_text_picks_largest_size() -> None:
    c = Canvas()
    font, lines, size = c.fit_text("Short", 500, 300, lambda s: _font(s), max_size=120, min_size=14)
    assert size > 60 and lines == ["Short"]
    font2, lines2, size2 = c.fit_text(
        "a much longer sentence that has to wrap several times to fit the box " * 3,
        300,
        120,
        lambda s: _font(s),
    )
    assert size2 < size and len(lines2) >= 2


# --- render_to_frame ----------------------------------------------------------------


class _FlatApp:
    id = "flat"
    name = "Flat"
    description = "test"
    needs_dither = False
    settings_schema: tuple[Field, ...] = ()
    variants = 1

    def render(self, ctx: RenderContext) -> Image.Image:
        c = Canvas(background=BLUE)
        c.rect((0, 0, 300, 400), fill=YELLOW)
        return c.to_image()


class _FrameApp(_FlatApp):
    id = "frame"

    def render(self, ctx: RenderContext) -> np.ndarray:
        return np.full((400, 600), GREEN, dtype=np.uint8)


class _BadColourApp(_FlatApp):
    id = "bad"

    def render(self, ctx: RenderContext) -> Image.Image:
        return Image.new("RGB", (600, 400), (128, 128, 128))


class _WrongSizeApp(_FlatApp):
    id = "size"

    def render(self, ctx: RenderContext) -> Image.Image:
        return Image.new("RGB", (300, 200), (0, 0, 0))


def test_render_to_frame_flat_and_frame(tmp_path: Path) -> None:
    assert isinstance(_FlatApp(), App)
    frame = render_to_frame(_FlatApp(), _ctx(tmp_path))
    assert frame[0, 0] == YELLOW and frame[0, 599] == BLUE
    assert render_to_frame(_FrameApp(), _ctx(tmp_path))[200, 300] == GREEN
    with pytest.raises(ValueError, match="needs_dither=False"):
        render_to_frame(_BadColourApp(), _ctx(tmp_path))
    with pytest.raises(ValueError, match="300x200"):
        render_to_frame(_WrongSizeApp(), _ctx(tmp_path))


def test_registry_rejects_unknown_ids() -> None:
    assert "photos" in APP_IDS and "weather" in APP_IDS
    with pytest.raises(KeyError):
        load_app("nope")


def test_font_library_errors(tmp_path: Path) -> None:
    lib = FontLibrary(tmp_path / "missing.json")
    with pytest.raises(Exception, match="manifest"):
        lib.font("serif", 20)


def test_bundled_fonts_load_and_render() -> None:
    lib = FontLibrary()
    assert set(lib.roles()) >= {"serif", "sans", "mono"}
    for role in ("serif", "sans", "mono"):
        assert {"regular", "bold"} <= set(lib.weights(role))
        for weight in lib.weights(role):
            f = lib.font(role, 32, weight)
            assert f.getname()[0]
    assert lib.font("sans", 20) is lib.font("sans", 20)  # cached
    assert lib.font("mono", 20, "italic").getname()[1] == "Regular"  # falls back
    c = Canvas()
    box = c.text((20, 20), "Weather 23° 12:45", lib.font("sans", 36, "bold"), BLACK)
    frame = c.to_frame()
    assert (frame[box[1] : box[3], box[0] : box[2]] == BLACK).sum() > 200
