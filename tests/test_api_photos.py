"""``/api/photos`` and ``/api/recipe/default`` end to end (TestClient, real engine)."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import starlette.formparsers
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import Request

from inkframe.api import photos as photos_api
from inkframe.config import DEFAULT_RECIPE, RECIPE_RANGES
from inkframe.display.palette import FRAME_BYTES, MEASURED_DEFAULT, WHITE, pack_frame
from inkframe.paths import DataPaths
from inkframe.render import masters, pipeline
from inkframe.render.cache import RenderCache, decode_png
from inkframe.render.dither import METHOD_IDS, METHODS
from tests.conftest import encode, gradient_image, heic_bytes, jpeg_bytes

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RECIPE = pipeline.complete_recipe(DEFAULT_RECIPE)


@pytest.fixture(autouse=True)
def _fresh_fit_cache() -> Iterator[None]:
    pipeline.clear_fit_cache()
    yield
    pipeline.clear_fit_cache()


@pytest.fixture(scope="module")
def gradient_jpeg() -> bytes:
    return jpeg_bytes()


def upload(client: TestClient, data: bytes, filename: str = "photo.jpg", **form: str) -> Any:
    return client.post(
        "/api/photos",
        files={"file": (filename, data, "application/octet-stream")},
        data=form or None,
    )


def _decode(data: bytes) -> np.ndarray:
    frame = decode_png(data)
    assert frame is not None
    return frame


def _direct_render(data_dir: Path, photo: dict[str, Any], recipe: dict[str, Any]) -> np.ndarray:
    """The pipeline run directly on the stored master, bypassing API and cache."""
    master = Image.open(data_dir / "masters" / f"{photo['id']}.jpg")
    fit = pipeline.fit_image(master)
    palette = MEASURED_DEFAULT if recipe["palette"] == "measured" else pipeline.NOMINAL
    return pipeline.render_frame(fit, recipe, palette)


# --- the happy path ------------------------------------------------------------------------


def test_end_to_end(client: TestClient, data_dir: Path, gradient_jpeg: bytes) -> None:
    # upload
    r = upload(client, gradient_jpeg, "beach.jpg")
    assert r.status_code == 201, r.text
    photo = r.json()
    pid = photo["id"]
    assert len(pid) == 32
    assert "master_path" not in photo
    assert photo["original_name"] == "beach.jpg"
    assert (photo["width"], photo["height"], photo["portrait"]) == (1800, 1200, False)
    assert photo["excluded"] is False and photo["recipe"] is None
    assert photo["recipe_effective"] == RECIPE
    assert photo["render_ready"] is True
    key = photo["render_key"]
    assert photo["render_url"] == f"/api/photos/{pid}/render?recipe={key}"
    assert (data_dir / "masters" / f"{pid}.jpg").is_file()
    assert photo["master_bytes"] == (data_dir / "masters" / f"{pid}.jpg").stat().st_size

    # list
    listed = client.get("/api/photos").json()
    assert listed["count"] == 1 and listed["photos"] == [photo]
    assert client.get(f"/api/photos/{pid}").json() == photo

    # the default render
    r = client.get(photo["render_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    frame = _decode(r.content)
    assert np.array_equal(frame, _direct_render(data_dir, photo, RECIPE))

    # previews: every method, in registry order
    r = client.post(f"/api/photos/{pid}/previews", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["took_ms"], int)
    previews = body["previews"]
    assert [p["method"] for p in previews] == list(METHOD_IDS) and len(previews) == 12
    for p, m in zip(previews, METHODS, strict=True):
        assert (p["name"], p["description"], p["kind"]) == (m.name, m.description, m.kind)
        assert p["url"] == f"/api/photos/{pid}/render?recipe={p['key']}"
    assert previews[METHOD_IDS.index("atkinson")]["key"] == key
    assert len({p["key"] for p in previews}) == 12
    for p in previews:
        img = client.get(p["url"])
        assert img.status_code == 200 and img.content.startswith(PNG_MAGIC)
        with Image.open(io.BytesIO(img.content)) as im:
            assert im.mode == "P" and im.getpalette()[:18] == MEASURED_DEFAULT.flat()
        expected = _direct_render(data_dir, photo, {**RECIPE, "method": p["method"]})
        assert np.array_equal(_decode(img.content), expected), p["method"]

    # a second identical call is all cache hits
    renders = data_dir / "renders" / pid
    mtimes = {f.name: f.stat().st_mtime_ns for f in renders.iterdir()}
    again = client.post(f"/api/photos/{pid}/previews", json={})
    assert again.json()["previews"] == previews
    assert again.json()["took_ms"] <= max(body["took_ms"], 50)
    assert {f.name: f.stat().st_mtime_ns for f in renders.iterdir()} == mtimes

    # set a recipe
    r = client.put(f"/api/photos/{pid}/recipe", json={"method": "floyd_steinberg", "gamma": 1.2})
    assert r.status_code == 200, r.text
    updated = r.json()
    want = {**RECIPE, "method": "floyd_steinberg", "gamma": 1.2}
    assert updated["recipe"] == want and updated["recipe_effective"] == want
    assert updated["render_ready"] is True and updated["render_key"] != key
    r = client.get(updated["render_url"])
    assert r.status_code == 200
    frame = _decode(r.content)
    packed = pack_frame(frame)
    assert len(packed) == FRAME_BYTES == 120000
    direct = _direct_render(data_dir, photo, want)
    assert np.array_equal(frame, direct)
    assert packed == pack_frame(direct)

    # flags
    r = client.put(f"/api/photos/{pid}/flags", json={"excluded": True})
    assert r.status_code == 200 and r.json()["excluded"] is True
    assert client.get("/api/photos").json()["photos"][0]["excluded"] is True
    assert client.app.state.db.list_photos(include_excluded=False) == []
    assert (
        client.put(f"/api/photos/{pid}/flags", json={"excluded": False}).json()["excluded"] is False
    )

    # delete
    r = client.delete(f"/api/photos/{pid}")
    assert r.status_code == 204 and r.content == b""
    assert not (data_dir / "masters" / f"{pid}.jpg").exists()
    assert not renders.exists()
    assert client.app.state.db.get_photo(pid) is None
    assert client.get(f"/api/photos/{pid}").status_code == 404
    assert client.get(updated["render_url"]).status_code == 404
    assert client.delete(f"/api/photos/{pid}").status_code == 404
    assert client.get("/api/photos").json() == {"photos": [], "count": 0}


def test_list_is_newest_first(client: TestClient) -> None:
    small = jpeg_bytes(Image.new("RGB", (90, 60), (10, 20, 30)))
    ids = []
    for name in ("one.jpg", "two.jpg", "three.jpg"):
        ids.append(upload(client, small, name).json()["id"])
        time.sleep(0.002)  # distinct created_at milliseconds
    body = client.get("/api/photos").json()
    assert body["count"] == 3
    assert [p["id"] for p in body["photos"]] == ids[::-1]
    assert [p["original_name"] for p in body["photos"]] == ["three.jpg", "two.jpg", "one.jpg"]


def test_previews_with_overrides(client: TestClient, data_dir: Path) -> None:
    photo = upload(client, jpeg_bytes(gradient_image(900, 600))).json()
    pid = photo["id"]
    knobs = {
        "brightness": 1.1,
        "contrast": 1.2,
        "saturation": 0.9,
        "gamma": 1.1,
        "sharpen": 0.5,
        "serpentine": False,
        "strength": 0.7,
        "distance": "rgb",
        "palette": "nominal",
        "method": "ignored_because_every_method_is_rendered",
    }
    r = client.post(f"/api/photos/{pid}/previews", json=knobs)
    assert r.status_code == 200, r.text
    previews = r.json()["previews"]
    assert photo["render_key"] not in {p["key"] for p in previews}
    knobs.pop("method")
    blue_noise = previews[METHOD_IDS.index("blue_noise")]
    recipe = {**RECIPE, **knobs, "method": "blue_noise"}
    got = _decode(client.get(blue_noise["url"]).content)
    assert np.array_equal(got, _direct_render(data_dir, photo, recipe))
    # previews never change what the photo itself renders with
    assert client.get(f"/api/photos/{pid}").json()["recipe"] is None


def test_previews_errors(client: TestClient) -> None:
    pid = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()["id"]
    r = client.post(f"/api/photos/{pid}/previews", json={"strength": 3})
    assert r.status_code == 422 and "strength" in r.json()["error"]
    r = client.post(f"/api/photos/{pid}/previews", json={"brightnes": 1.0})
    assert r.status_code == 422 and "unknown key" in r.json()["error"]
    r = client.post(f"/api/photos/{pid}/previews", json=[1, 2])
    assert r.status_code == 422
    assert client.post(f"/api/photos/{'0' * 32}/previews", json={}).status_code == 404
    # no body at all: the effective recipe
    assert client.post(f"/api/photos/{pid}/previews").status_code == 200


def test_recipe_put_errors_and_reset(client: TestClient) -> None:
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60), (200, 100, 50)))).json()
    pid, default_key = photo["id"], photo["render_key"]
    for body, fragment in (
        ({"strength": 0.1}, "strength"),
        ({"method": "crosshatch"}, "method"),
        ({"distance": "cmyk"}, "distance"),
        ({"bogus": 1}, "unknown key"),
        ({"reset": "yes"}, "reset"),
        ({"reset": True, "gamma": 1.2}, "reset"),
    ):
        r = client.put(f"/api/photos/{pid}/recipe", json=body)
        assert r.status_code == 422, body
        assert fragment in r.json()["error"], (body, r.json())
    assert client.put(f"/api/photos/{pid}/recipe", json=[1]).status_code == 422
    assert client.get(f"/api/photos/{pid}").json()["recipe"] is None

    full = {**RECIPE, "method": "stucki", "palette": "nominal"}
    r = client.put(f"/api/photos/{pid}/recipe", json=full)
    assert r.status_code == 200 and r.json()["recipe"] == full
    # a partial update merges over the stored override, not the default
    r = client.put(f"/api/photos/{pid}/recipe", json={"sharpen": 1.0, "reset": False})
    assert r.json()["recipe"] == {**full, "sharpen": 1.0}
    r = client.put(f"/api/photos/{pid}/recipe", json={"reset": True})
    assert r.status_code == 200
    assert r.json()["recipe"] is None and r.json()["recipe_effective"] == RECIPE
    assert r.json()["render_key"] == default_key and r.json()["render_ready"] is True
    assert client.put(f"/api/photos/{'a' * 32}/recipe", json={}).status_code == 404


def test_recipe_put_without_fields_pins_nothing(client: TestClient) -> None:
    # a UI that sends reset:false by habit must not pin a photo to today's default
    pid = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()["id"]
    for body in ({}, {"reset": False}):
        r = client.put(f"/api/photos/{pid}/recipe", json=body)
        assert r.status_code == 200 and r.json()["recipe"] is None, body
    client.put("/api/recipe/default", json={"method": "burkes"})
    assert client.get(f"/api/photos/{pid}").json()["recipe_effective"]["method"] == "burkes"
    # and a stored override is left as it is
    own = client.put(f"/api/photos/{pid}/recipe", json={"gamma": 1.3}).json()["recipe"]
    r = client.put(f"/api/photos/{pid}/recipe", json={"reset": False})
    assert r.status_code == 200 and r.json()["recipe"] == own


def test_huge_integer_knob_is_422_not_500(client: TestClient) -> None:
    pid = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()["id"]
    huge = "1" + "0" * 400  # valid JSON; float() of it raises OverflowError
    for method, url, knob in (
        ("POST", f"/api/photos/{pid}/previews", "brightness"),
        ("PUT", f"/api/photos/{pid}/recipe", "gamma"),
        ("PUT", "/api/recipe/default", "strength"),
    ):
        body = '{"' + knob + '": ' + huge + "}"
        r = client.request(method, url, content=body, headers={"content-type": "application/json"})
        assert r.status_code == 422, (method, url, r.status_code, r.text[:120])
        assert knob in r.json()["error"]
    assert client.get(f"/api/photos/{pid}").json()["recipe"] is None
    assert client.get("/api/recipe/default").json()["strength"] == RECIPE["strength"]


def test_a_render_failure_after_the_insert_is_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the photo is stored: answering 500 would make the client upload it again
    def full_disk(*_a: Any, **_kw: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(RenderCache, "put", full_disk)
    r = upload(client, jpeg_bytes(Image.new("RGB", (90, 60))))
    assert r.status_code == 201, r.text
    photo = r.json()
    assert photo["render_ready"] is False
    assert [p.id for p in client.app.state.db.list_photos(include_excluded=True)] == [photo["id"]]
    monkeypatch.undo()
    assert client.get(photo["render_url"]).status_code == 200  # renders on demand later


def test_current_render_is_served_from_disk_without_the_executor(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60), (5, 90, 200)))).json()
    pid = photo["id"]
    jobs: list[str] = []
    real = photos_api._render_job

    def counting(photo_id: str, *args: Any) -> bytes:
        jobs.append(photo_id)
        return real(photo_id, *args)

    monkeypatch.setattr(photos_api, "_render_job", counting)
    for _ in range(3):  # polling the current render never queues behind previews
        r = client.get(f"/api/photos/{pid}/render")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert jobs == []
    (data_dir / "renders" / pid / f"{photo['render_key']}.png").write_bytes(PNG_MAGIC + b"junk")
    r = client.get(f"/api/photos/{pid}/render")  # not a valid frame: re-rendered
    assert r.status_code == 200 and _decode(r.content).shape == (400, 600)
    assert jobs == [pid]


def test_uploads_holding_bytes_are_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    lock = threading.Lock()
    reads: list[int] = []
    real_read, real_ingest = photos_api._read_upload, masters.ingest

    async def counting_read(*args: Any) -> Any:
        with lock:
            reads.append(len(reads) + 1)
        return await real_read(*args)

    def slow_ingest(*args: Any, **kw: Any) -> Any:
        release.wait(timeout=20)
        return real_ingest(*args, **kw)

    monkeypatch.setattr(photos_api, "_read_upload", counting_read)
    monkeypatch.setattr(masters, "ingest", slow_ingest)
    data = jpeg_bytes(Image.new("RGB", (90, 60)))
    results: list[int] = []

    def send() -> None:
        results.append(upload(client, data).status_code)

    senders = [threading.Thread(target=send) for _ in range(photos_api.UPLOAD_SLOTS + 2)]
    for t in senders:
        t.start()
    deadline = time.monotonic() + 10
    while len(reads) < photos_api.UPLOAD_SLOTS and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)  # time for an unbounded server to read the others too
    try:
        assert len(reads) == photos_api.UPLOAD_SLOTS  # the rest wait, unread
    finally:
        release.set()
        for t in senders:
            t.join(timeout=30)
    assert results == [201] * len(senders) and len(reads) == len(senders)


def test_flags_errors(client: TestClient) -> None:
    pid = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()["id"]
    for body in ({}, {"excluded": "yes"}, {"excluded": 1}, {"excluded": True, "pinned": True}):
        r = client.put(f"/api/photos/{pid}/flags", json=body)
        assert r.status_code == 422 and "error" in r.json(), body
    assert client.put(f"/api/photos/{'b' * 32}/flags", json={"excluded": True}).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/photos/nothex",
        "/api/photos/ABCDEF0123456789ABCDEF0123456789",
        "/api/photos/0123456789abcdef0123456789abcde",
        "/api/photos/0123456789abcdef0123456789abcdef",  # well formed, unknown
    ],
)
def test_unknown_or_malformed_ids_are_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
    assert client.get(path).json()["error"]
    assert client.get(path + "/render").status_code == 404
    assert client.post(path + "/previews", json={}).status_code == 404
    assert client.put(path + "/recipe", json={"gamma": 1.1}).status_code == 404
    assert client.put(path + "/flags", json={"excluded": True}).status_code == 404
    assert client.delete(path).status_code == 404


def test_render_keys(client: TestClient, data_dir: Path) -> None:
    photo = upload(client, jpeg_bytes(gradient_image(600, 400))).json()
    pid, key = photo["id"], photo["render_key"]
    base = f"/api/photos/{pid}/render"
    assert client.get(f"{base}?recipe=nothex").status_code == 404
    assert client.get(f"{base}?recipe={'0' * 16}").status_code == 404  # never rendered
    assert client.get(f"{base}?recipe=../../x").status_code == 404
    # no key: the current render, not cacheable
    r = client.get(base)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert r.content == client.get(photo["render_url"]).content
    # the current key renders on demand when its file is gone
    path = data_dir / "renders" / pid / f"{key}.png"
    path.unlink()
    r = client.get(photo["render_url"])
    assert r.status_code == 200 and path.is_file()
    assert np.array_equal(_decode(r.content), _direct_render(data_dir, photo, RECIPE))


def test_changing_the_default_rerenders_lazily(client: TestClient, data_dir: Path) -> None:
    photo = upload(client, jpeg_bytes(gradient_image(600, 400))).json()
    pid = photo["id"]
    r = client.put("/api/recipe/default", json={"method": "burkes", "contrast": 1.1})
    assert r.status_code == 200
    listed = client.get("/api/photos").json()["photos"][0]
    assert listed["recipe_effective"] == {**RECIPE, "method": "burkes", "contrast": 1.1}
    assert listed["render_key"] != photo["render_key"]
    assert listed["render_ready"] is False  # not rendered eagerly
    assert len(list((data_dir / "renders" / pid).iterdir())) == 1
    r = client.get(listed["render_url"])
    assert r.status_code == 200
    assert client.get("/api/photos").json()["photos"][0]["render_ready"] is True
    # a photo with its own recipe is unaffected by the default
    client.put(f"/api/photos/{pid}/recipe", json={"method": "sierra"})
    client.put("/api/recipe/default", json={"method": "jarvis"})
    assert client.get(f"/api/photos/{pid}").json()["recipe_effective"]["method"] == "sierra"


def test_measured_palette_change_changes_keys_and_png_colours(client: TestClient) -> None:
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60), (30, 50, 140)))).json()
    tuned = [10, 40, 150]
    client.put("/api/settings", json={"palette": {"measured": {"blue": tuned}}})
    now = client.get(f"/api/photos/{photo['id']}").json()
    assert now["render_key"] != photo["render_key"]
    data = client.get(now["render_url"]).content
    with Image.open(io.BytesIO(data)) as im:
        assert im.getpalette()[12:15] == tuned


def test_portrait_upload(client: TestClient) -> None:
    photo = upload(client, jpeg_bytes(gradient_image(800, 1200)), "tall.jpg").json()
    assert photo["portrait"] is True and (photo["width"], photo["height"]) == (800, 1200)
    frame = _decode(client.get(photo["render_url"]).content)
    assert np.all(frame[:, :166] == WHITE) and np.all(frame[:, 433:] == WHITE)


def test_upload_name_field_and_sanitising(client: TestClient) -> None:
    small = jpeg_bytes(Image.new("RGB", (90, 60)))
    assert upload(client, small, "x.jpg", name="Grand Canyon").json()["original_name"] == (
        "Grand Canyon"
    )
    assert upload(client, small, "C:\\fake\\path\\evil.jpg").json()["original_name"] == ("evil.jpg")
    # (httpx percent-encodes control characters in file names, so send them in "name")
    named = upload(client, small, "x.jpg", name="bell\x07 and\ttab\x1b[0m")
    assert named.json()["original_name"] == "bell and tab[0m"
    long_name = "n" * 500 + ".jpg"
    assert len(upload(client, small, long_name).json()["original_name"]) == 120


def test_png_and_webp_uploads(client: TestClient) -> None:
    src = gradient_image(1200, 800)
    rgba = src.convert("RGBA")
    rgba.putalpha(255)
    cases = [("a.png", encode(rgba, "PNG")), ("a.webp", encode(src, "WEBP", quality=85))]
    for name, data in cases:
        r = upload(client, data, name)
        assert r.status_code == 201, (name, r.text)
        assert (r.json()["width"], r.json()["height"]) == (1200, 800)
        assert r.json()["render_ready"] is True


def test_heic_upload(client: TestClient) -> None:
    cases = [("IMG_0001.HEIC", heic_bytes(gradient_image(1200, 800)))]
    for name, data in cases:
        r = upload(client, data, name)
        assert r.status_code == 201, (name, r.text)
        assert (r.json()["width"], r.json()["height"]) == (1200, 800)
        assert r.json()["render_ready"] is True


# --- refusals ----------------------------------------------------------------------------------


def _assert_no_files(data_dir: Path) -> None:
    assert list((data_dir / "masters").iterdir()) == []
    assert client_rows_are_empty(data_dir)


def client_rows_are_empty(data_dir: Path) -> bool:
    import sqlite3

    with sqlite3.connect(data_dir / "inkframe.db") as conn:
        return conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


def test_30_mb_upload_is_413(client: TestClient, data_dir: Path) -> None:
    r = upload(client, b"\xff\xd8\xff" + bytes(30 * 1024 * 1024), "huge.jpg")
    assert r.status_code == 413 and "25 MB" in r.json()["error"]
    _assert_no_files(data_dir)


def test_413_by_real_size(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = jpeg_bytes(Image.new("RGB", (300, 200), (9, 9, 9)))
    monkeypatch.setattr(photos_api, "MAX_UPLOAD_BYTES", len(data) - 1)
    r = upload(client, data)  # Content-Length passes the overhead allowance, the file does not
    assert r.status_code == 413
    monkeypatch.setattr(photos_api, "MAX_UPLOAD_BYTES", len(data))
    assert upload(client, data).status_code == 201


def test_413_for_a_chunked_body_without_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file part itself is small and well inside the limit; the excess is
    # an epilogue after the closing boundary, which the multipart parser
    # skips.  Only the streaming guard counting the raw body can refuse it
    # (without that guard this is a 415 for the tiny non-image part).
    monkeypatch.setattr(photos_api, "MAX_UPLOAD_BYTES", 1000)
    boundary = "xyz"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()

    def body() -> Iterator[bytes]:
        yield head + b"\xff" * 100 + f"\r\n--{boundary}--\r\n".encode()
        for _ in range(200):  # 200 KB > 1000 B + the 64 KB multipart allowance
            yield b"e" * 1024

    r = client.post(
        "/api/photos",
        content=body(),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and "limit" in r.json()["error"]
    # the same request without the epilogue gets as far as sniffing the bytes
    r = client.post(
        "/api/photos",
        content=head + b"\xff" * 100 + f"\r\n--{boundary}--\r\n".encode(),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 415


_BOUNDARY = "xyz"
_PART_HEAD = (
    f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="a.jpg"\r\n'
    "Content-Type: image/jpeg\r\n\r\n"
).encode()


class _Spool(starlette.formparsers.SpooledTemporaryFile):  # type: ignore[misc]
    """Records every spooled upload file the multipart parser opens."""

    made: list[_Spool] = []

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        _Spool.made.append(self)


def _read_streamed(chunks: Iterable[bytes], *, interval: float = 0.0, stall: bool = False) -> Any:
    """Run ``_read_upload`` on a body that arrives in ``chunks`` (``interval``
    seconds apart) and then ends, or then stops arriving (``stall``)."""
    it = iter(chunks)

    async def receive() -> dict[str, Any]:
        if interval:
            await asyncio.sleep(interval)
        for chunk in it:
            return {"type": "http.request", "body": chunk, "more_body": True}
        if stall:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    content_type = f"multipart/form-data; boundary={_BOUNDARY}".encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/photos",
        "headers": [(b"content-type", content_type)],
        "query_string": b"",
    }
    return asyncio.run(photos_api._read_upload(Request(scope, receive), 100_000, 50_000))


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch) -> list[_Spool]:
    _Spool.made = []
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", _Spool)
    return _Spool.made


def test_a_body_cut_off_mid_stream_closes_its_spool(spools: list[_Spool]) -> None:
    # the 413 is raised inside the body stream, after the file part was
    # opened: MultiPartParser.parse closes its files on any exception
    with pytest.raises(HTTPException) as exc:
        _read_streamed([_PART_HEAD] + [b"\xff" * 1024] * 200)
    assert exc.value.status_code == 413
    assert len(spools) == 1 and spools[0].closed


def test_a_stalled_upload_is_408_and_frees_its_slot(
    spools: list[_Spool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(photos_api, "UPLOAD_IDLE_SECONDS", 0.05)
    t0 = time.monotonic()
    with pytest.raises(HTTPException) as exc:
        _read_streamed([_PART_HEAD, b"\xff" * 100], stall=True)
    assert exc.value.status_code == 408 and "stalled" in exc.value.detail
    assert time.monotonic() - t0 < 5
    assert len(spools) == 1 and spools[0].closed
    # a trickle that never stalls for long is cut off by the total deadline
    monkeypatch.setattr(photos_api, "UPLOAD_IDLE_SECONDS", 1.0)
    monkeypatch.setattr(photos_api, "UPLOAD_READ_SECONDS", 0.2)
    with pytest.raises(HTTPException) as exc:
        _read_streamed([_PART_HEAD] + [b"\xff" * 10] * 1000, interval=0.01)
    assert exc.value.status_code == 408 and "longer than" in exc.value.detail
    assert all(s.closed for s in spools)
    # and a body that keeps coming is read as before
    monkeypatch.setattr(photos_api, "UPLOAD_READ_SECONDS", 30.0)
    tail = f"\r\n--{_BOUNDARY}--\r\n".encode()
    data, name = _read_streamed([_PART_HEAD, b"\xff" * 100, b"\xd8" * 100, tail], interval=0.01)
    assert data == b"\xff" * 100 + b"\xd8" * 100 and name == "a.jpg"


def test_408_reaches_the_client_in_the_error_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stalled(*_args: Any) -> Any:
        raise photos_api._stalled("stalled: no data for 30 s")

    monkeypatch.setattr(photos_api, "_read_upload", stalled)
    r = upload(client, jpeg_bytes(Image.new("RGB", (90, 60))))
    assert r.status_code == 408 and "stalled" in r.json()["error"]
    monkeypatch.undo()
    for _ in range(photos_api.UPLOAD_SLOTS + 1):  # the slots were given back
        assert upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).status_code == 201


def test_415_text_file_named_jpg(client: TestClient, data_dir: Path) -> None:
    r = upload(client, b"definitely not a photo\n" * 50, "holiday.jpg")
    assert r.status_code == 415 and "not a supported image" in r.json()["error"]
    _assert_no_files(data_dir)


def test_415_not_multipart(client: TestClient) -> None:
    r = client.post("/api/photos", content=jpeg_bytes(), headers={"content-type": "image/jpeg"})
    assert r.status_code == 415


def test_422_undecodable_and_malformed(client: TestClient, data_dir: Path) -> None:
    data = jpeg_bytes()
    r = upload(client, data[: len(data) // 3], "cut.jpg")
    assert r.status_code == 422 and "decode" in r.json()["error"]
    r = client.post("/api/photos", files={"other": ("a.jpg", data, "image/jpeg")})
    assert r.status_code == 422 and "'file'" in r.json()["error"]
    r = client.post(
        "/api/photos",
        files=[("file", ("a.jpg", data, "image/jpeg")), ("file", ("b.jpg", data, "image/jpeg"))],
    )
    assert r.status_code == 422
    r = client.post(
        "/api/photos", content=b"--x\r\n", headers={"content-type": "multipart/form-data"}
    )
    assert r.status_code == 422  # no boundary
    _assert_no_files(data_dir)


def test_failed_row_insert_removes_the_master(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kw: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(client.app.state.db, "add_photo", boom)
    r = upload(client, jpeg_bytes(Image.new("RGB", (90, 60))))
    assert r.status_code == 500 and r.json() == {"error": "disk full"}
    assert list((data_dir / "masters").iterdir()) == []


def test_missing_master(client: TestClient, data_dir: Path) -> None:
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()
    pid = photo["id"]
    (data_dir / "masters" / f"{pid}.jpg").unlink()
    RenderCache(DataPaths(data_dir)).invalidate(pid)
    pipeline.clear_fit_cache()
    r = client.get(photo["render_url"])
    assert r.status_code == 404 and "missing" in r.json()["error"]
    assert client.post(f"/api/photos/{pid}/previews", json={}).status_code == 404
    # the recipe is still stored; the render fails quietly
    r = client.put(f"/api/photos/{pid}/recipe", json={"gamma": 1.3})
    assert r.status_code == 200 and r.json()["render_ready"] is False
    assert client.delete(f"/api/photos/{pid}").status_code == 204


# --- threading ------------------------------------------------------------------------------------


def test_heavy_work_runs_on_the_render_executor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: dict[str, set[str]] = {"ingest": set(), "dither": set()}
    real_ingest, real_dither = masters.ingest, pipeline.dither_engine.dither

    def ingest(*a: Any, **kw: Any) -> Any:
        threads["ingest"].add(threading.current_thread().name)
        return real_ingest(*a, **kw)

    def dither(*a: Any, **kw: Any) -> Any:
        threads["dither"].add(threading.current_thread().name)
        return real_dither(*a, **kw)

    monkeypatch.setattr(masters, "ingest", ingest)
    monkeypatch.setattr(pipeline.dither_engine, "dither", dither)
    pid = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()["id"]
    client.post(f"/api/photos/{pid}/previews", json={"gamma": 1.4})
    client.put(f"/api/photos/{pid}/recipe", json={"gamma": 1.5})
    assert threads["ingest"] and threads["dither"]
    for names in threads.values():
        assert all(n.startswith("render") for n in names), names


# --- the default recipe ---------------------------------------------------------------------------


def test_default_recipe_get(client: TestClient) -> None:
    r = client.get("/api/recipe/default")
    assert r.status_code == 200
    body = r.json()
    assert {k: body[k] for k in DEFAULT_RECIPE} == RECIPE
    assert set(body) == set(DEFAULT_RECIPE) | {"methods", "limits"}
    assert body["methods"] == [
        {"id": m.id, "name": m.name, "description": m.description, "kind": m.kind} for m in METHODS
    ]
    for knob, (lo, hi) in RECIPE_RANGES.items():
        assert body["limits"][knob] == {"min": lo, "max": hi}
    assert body["limits"]["distance"] == {"choices": ["rgb", "lab"]}
    assert body["limits"]["palette"] == {"choices": ["nominal", "measured"]}


def test_default_recipe_put(client: TestClient, data_dir: Path) -> None:
    r = client.put("/api/recipe/default", json={"method": "bayer_8", "saturation": 1.3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "bayer_8" and body["saturation"] == 1.3
    assert body["strength"] == RECIPE["strength"] and len(body["methods"]) == 12
    on_disk = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["default_recipe"] == {k: body[k] for k in DEFAULT_RECIPE}
    assert client.get("/api/recipe/default").json() == body
    assert client.get("/api/settings").json()["default_recipe"]["method"] == "bayer_8"
    # a GET body can be edited and sent straight back
    body["gamma"] = 1.25
    r = client.put("/api/recipe/default", json=body)
    assert r.status_code == 200 and r.json()["gamma"] == 1.25 and r.json()["method"] == "bayer_8"
    # new uploads use it
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()
    assert photo["recipe"] is None and photo["recipe_effective"]["method"] == "bayer_8"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"method": "crosshatch"}, "method"),
        ({"strength": 1.5}, "strength"),
        ({"palette": "sepia"}, "palette"),
        ({"serpentine": "yes"}, "serpentine"),
        ({"surprise": 1}, "unknown key"),
    ],
)
def test_default_recipe_put_invalid(client: TestClient, body: dict, fragment: str) -> None:
    before = client.get("/api/recipe/default").json()
    r = client.put("/api/recipe/default", json=body)
    assert r.status_code == 422 and fragment in r.json()["error"]
    assert client.get("/api/recipe/default").json() == before


def test_unknown_default_method_from_settings_falls_back_alone(client: TestClient) -> None:
    # PUT /api/settings validates without the dither registry, so it can
    # store a method this build lacks; the default's other knobs still apply
    r = client.put("/api/settings", json={"default_recipe": {"method": "gone", "gamma": 1.3}})
    assert r.status_code == 200
    body = client.get("/api/recipe/default").json()
    assert body["method"] == RECIPE["method"] and body["gamma"] == 1.3
    photo = upload(client, jpeg_bytes(Image.new("RGB", (90, 60)))).json()
    assert photo["recipe_effective"] == {**RECIPE, "gamma": 1.3}
    # a PUT of the default stores a complete, usable recipe again
    r = client.put("/api/recipe/default", json={"strength": 0.7})
    assert r.status_code == 200 and r.json()["gamma"] == 1.3
    assert client.get("/api/settings").json()["default_recipe"]["method"] == RECIPE["method"]


def test_default_recipe_put_non_object(client: TestClient) -> None:
    assert client.put("/api/recipe/default", json=["atkinson"]).status_code == 422


# --- timings ------------------------------------------------------------------------------------


def test_desktop_timings(client: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Not a benchmark: prints where the time goes and guards gross regressions."""
    src = gradient_image()
    payloads = [("jpeg", jpeg_bytes(src)), ("png", encode(src, "PNG"))]
    pillow_heif = pytest.importorskip("pillow_heif")
    if pillow_heif.libheif_info().get("encoders") and masters.heif_available():
        payloads.append(("heic", encode(src, "HEIF", quality=85)))
    lines = []
    pid = ""
    for label, data in payloads:
        t0 = time.perf_counter()
        r = upload(client, data, f"timing.{label}")
        ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 201
        pid = pid or r.json()["id"]
        lines.append(
            f"upload 2400x1600 {label} ({len(data) / 1e6:.1f} MB) incl. render: {ms:.0f} ms"
        )
    photo = client.app.state.db.get_photo(pid)
    paths = client.app.state.paths
    pipeline.clear_fit_cache()
    t0 = time.perf_counter()
    fit = pipeline.load_fit(photo, paths)
    lines.append(f"fit (decode master + Lanczos): {(time.perf_counter() - t0) * 1000:.0f} ms")
    t0 = time.perf_counter()
    pipeline.render_frame(fit, {**RECIPE, "gamma": 1.3}, MEASURED_DEFAULT)
    lines.append(
        f"single render (adjust + atkinson/lab): {(time.perf_counter() - t0) * 1000:.0f} ms"
    )
    t0 = time.perf_counter()
    r = client.post(f"/api/photos/{pid}/previews", json={"brightness": 1.05})
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    client.post(f"/api/photos/{pid}/previews", json={"brightness": 1.05})
    warm = (time.perf_counter() - t0) * 1000
    lines.append(f"12-method preview set: cold {cold:.0f} ms, cached {warm:.0f} ms")
    with capsys.disabled():
        print("\n  desktop timings:\n    " + "\n    ".join(lines))
    assert cold < 10_000 and warm < 1_000
