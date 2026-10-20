"""``/api/photos``: upload, list, render, previews, recipe, flags, delete.

The photo object every route returns is ``Photo.to_dict()`` without
``master_path`` (an implementation detail), plus:

``recipe_effective``  the complete recipe the photo renders with (its
                      override, else the default recipe)
``render_key``        the cache key of that render
``render_url``        ``/api/photos/<id>/render?recipe=<key>``
``render_ready``      whether that render is already on disk

Ids are 32 lowercase hex characters; anything else is a 404.  Uploads,
renders, previews, recipe changes and deletes run on the render executor
(one thread, shared with the scheduler), so they are serialised with each
other and the event loop never decodes or dithers.  Deletes go through the
executor too, so a render in flight for a photo cannot recreate its files
after they were removed.  A render already on disk is served from the
threadpool without queueing behind the executor.

At most ``UPLOAD_SLOTS`` uploads hold their bytes at once (parsing, waiting
for the executor, ingesting); later ones wait before their body is read, so
memory stays bounded however many a client sends.  A body that stops
arriving (``UPLOAD_IDLE_SECONDS``) or takes too long in all
(``UPLOAD_READ_SECONDS``) is answered 408, which frees its slot: two stalled
clients cannot block every other upload (uvicorn has no body-read timeout).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from inkframe.api import DbDep, PathsDep, RenderExecutorDep, SettingsDep, run_on_render_executor
from inkframe.config import Settings, SettingsStore
from inkframe.db import Database, Photo
from inkframe.paths import DataPaths
from inkframe.render import masters, pipeline
from inkframe.render.cache import RenderCache, encode_png, valid_key, valid_photo_id
from inkframe.render.dither import METHODS, METHODS_BY_ID, DitherError, DitherMethod

log = logging.getLogger("inkframe.api.photos")

router = APIRouter(prefix="/api/photos", tags=["photos"])

#: largest accepted image file; read at request time (tests lower it)
MAX_UPLOAD_BYTES = masters.MAX_UPLOAD_BYTES
#: allowance for multipart boundaries, part headers and the "name" field
FORM_OVERHEAD_BYTES = 64 * 1024
#: render URLs carry their content key, so browsers may keep them forever
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
#: uploads that may hold their bytes at once: the UI sends two in parallel,
#: so the second one's transfer overlaps the first one's ingest
UPLOAD_SLOTS = 2
#: a body with no data for this long is a 408 (read at request time)
UPLOAD_IDLE_SECONDS = 30.0
#: a body still arriving after this long is a 408: 25 MB at 85 KB/s, far
#: slower than any LAN (read at request time)
UPLOAD_READ_SECONDS = 300.0


class _NotFound(Exception):
    """Raised inside executor jobs; becomes a 404."""


# --- the photo object ------------------------------------------------------------------


def render_url(photo_id: str, key: str) -> str:
    return f"/api/photos/{photo_id}/render?recipe={key}"


def photo_json(photo: Photo, settings: Settings, cache: RenderCache) -> dict[str, Any]:
    """The API's photo object (see the module docstring)."""
    d = photo.to_dict()
    d.pop("master_path", None)
    recipe = pipeline.effective_recipe(photo, settings)
    key = pipeline.key_for(recipe, settings)
    d["recipe_effective"] = recipe
    d["render_key"] = key
    d["render_url"] = render_url(photo.id, key)
    d["render_ready"] = cache.has(photo.id, key)
    return d


def _require_id(photo_id: str) -> None:
    if not valid_photo_id(photo_id):
        raise HTTPException(status_code=404, detail=f"no photo {photo_id!r}")


async def _run(executor: Any, fn: Any, *args: Any) -> Any:
    """``run_on_render_executor`` with the pipeline's exceptions mapped to HTTP."""
    try:
        return await run_on_render_executor(executor, fn, *args)
    except _NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except pipeline.RecipeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except masters.IngestError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except pipeline.MasterMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _get_photo(db: Database, photo_id: str) -> Photo:
    photo = db.get_photo(photo_id)
    if photo is None:
        raise _NotFound(f"no photo {photo_id!r}")
    return photo


def _render_quietly(cache: RenderCache, photo: Photo, settings: Settings) -> str | None:
    """Render a photo's effective recipe; any failure is logged, not raised:
    the photo (or its recipe) is stored either way and renders on demand
    later, and an error answer would make a client retry the upload and
    store the photo twice.  Returns the key, or None if it did not render."""
    try:
        return cache.render_or_get(photo, None, settings).key
    except (DitherError, pipeline.MasterMissing) as exc:
        log.warning("could not render photo %s now (%s); it will render on demand", photo.id, exc)
    except Exception:  # OSError writing the render, MemoryError, a bug...
        log.exception("could not render photo %s now; it will render on demand", photo.id)
    return None


# --- list / read -----------------------------------------------------------------------


@router.get("")
def list_photos(db: DbDep, store: SettingsDep, paths: PathsDep) -> dict[str, Any]:
    """Every photo, newest first, with its render URL and readiness."""
    settings = store.get()
    cache = RenderCache(paths)
    photos = db.list_photos(include_excluded=True)[::-1]
    return {"photos": [photo_json(p, settings, cache) for p in photos], "count": len(photos)}


@router.get("/{photo_id}")
def read_photo(photo_id: str, db: DbDep, store: SettingsDep, paths: PathsDep) -> dict[str, Any]:
    """One photo object."""
    _require_id(photo_id)
    photo = db.get_photo(photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail=f"no photo {photo_id!r}")
    return photo_json(photo, store.get(), RenderCache(paths))


# --- upload ------------------------------------------------------------------------------


def _too_large(size: float, limit: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"the upload is {size / 1e6:.1f} MB; the limit is {limit / (1024 * 1024):g} MB",
    )


def _upload_slots(request: Request) -> asyncio.Semaphore:
    """The app's upload semaphore, made on first use on its event loop."""
    state = request.app.state
    slots = getattr(state, "upload_slots", None)
    if slots is None:
        slots = state.upload_slots = asyncio.Semaphore(UPLOAD_SLOTS)
    return slots


def _stalled(detail: str) -> HTTPException:
    return HTTPException(status_code=408, detail=f"the upload {detail}; try again")


async def _limited(
    stream: AsyncIterator[bytes], limit: int, max_bytes: int, *, idle: float, total: float
) -> AsyncIterator[bytes]:
    """Pass the request body through, refusing it once it exceeds ``limit``
    (413), or once no data has arrived for ``idle`` seconds or it has been
    arriving for ``total`` seconds (408)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total
    chunks = stream.__aiter__()
    received = 0
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise _stalled(f"took longer than {total:g} s")
        try:
            chunk = await asyncio.wait_for(chunks.__anext__(), min(idle, left))
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if left < idle:  # this wait was cut short by the deadline
                raise _stalled(f"took longer than {total:g} s") from None
            raise _stalled(f"stalled: no data for {idle:g} s") from None
        received += len(chunk)
        if received > limit:
            raise _too_large(received, max_bytes)
        yield chunk


def _ingest_job(
    data: bytes, name: str | None, db: Database, store: SettingsStore, paths: DataPaths
) -> dict[str, Any]:
    master = masters.ingest(data, name, paths=paths, max_bytes=MAX_UPLOAD_BYTES)
    try:
        photo = db.add_photo(
            photo_id=master.id,
            original_name=master.original_name,
            width=master.width,
            height=master.height,
            portrait=master.portrait,
            master_path=master.relative_path,
            master_bytes=master.bytes,
        )
    except BaseException:
        masters.delete_master(paths, master.id)
        raise
    settings = store.get()
    cache = RenderCache(paths)
    _render_quietly(cache, photo, settings)
    return photo_json(photo, settings, cache)


@router.post("", status_code=201)
async def upload_photo(
    request: Request,
    db: DbDep,
    store: SettingsDep,
    paths: PathsDep,
    executor: RenderExecutorDep,
) -> dict[str, Any]:
    """Multipart upload: one image in field ``file``, optional display name in
    ``name``.  Returns the new photo object once its master is stored and the
    default-recipe render is cached (so the grid can show it straight away).

    413 too large (by ``Content-Length`` before reading, and by the real
    size), 415 not an accepted image type, 422 undecodable or malformed,
    408 a body that stalls or takes too long.  The body is only read once
    one of ``UPLOAD_SLOTS`` is free.
    """
    max_bytes = MAX_UPLOAD_BYTES
    limit = max_bytes + FORM_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None
        if declared_size > limit:
            raise _too_large(declared_size, max_bytes)
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status_code=415, detail="upload as multipart/form-data with the image in field 'file'"
        )
    async with _upload_slots(request):
        data, name = await _read_upload(request, limit, max_bytes)
        return await _run(executor, _ingest_job, data, name, db, store, paths)


async def _read_upload(request: Request, limit: int, max_bytes: int) -> tuple[bytes, str | None]:
    """Parse the multipart body: the bytes of field ``file`` and the display
    name (field ``name``, else the file name).  The spooled copy is closed
    before this returns, whatever happens: on success by ``form.close()``,
    and when parsing fails, including the 413 or 408 ``_limited`` raises
    mid-stream, by ``MultiPartParser.parse`` itself, which closes every
    file it opened on any exception (Starlette 1.7; a test pins it)."""
    parser = MultiPartParser(
        request.headers,
        _limited(
            request.stream(),
            limit,
            max_bytes,
            idle=UPLOAD_IDLE_SECONDS,
            total=UPLOAD_READ_SECONDS,
        ),
        max_files=1,
        max_fields=4,
        max_part_size=4096,
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(
            status_code=422, detail=f"bad multipart upload: {exc.message}"
        ) from None
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail="the multipart field 'file' is missing")
        if upload.size is not None and upload.size > max_bytes:
            raise _too_large(upload.size, max_bytes)
        data = await upload.read()
        if len(data) > max_bytes:
            raise _too_large(len(data), max_bytes)
        name_field = form.get("name")
        name = name_field if isinstance(name_field, str) and name_field.strip() else upload.filename
    finally:
        await form.close()
    return data, name


# --- renders ---------------------------------------------------------------------------------


def _render_job(
    photo_id: str, key: str | None, db: Database, store: SettingsStore, paths: DataPaths
) -> bytes:
    photo = _get_photo(db, photo_id)
    settings = store.get()
    cache = RenderCache(paths)
    recipe = pipeline.effective_recipe(photo, settings)
    if key is not None and key != pipeline.key_for(recipe, settings):
        data = cache.read_png(photo_id, key)  # a preview job may have written it meanwhile
        if data is None:
            raise _NotFound(f"photo {photo_id} has no render {key!r}")
        return data
    rendered = cache.render_or_get(photo, recipe, settings)
    data = cache.read_png(photo_id, rendered.key)
    return data if data is not None else encode_png(rendered.frame, settings.measured_palette())


def _current_from_disk(
    photo_id: str, db: Database, store: SettingsStore, paths: DataPaths
) -> bytes | None:
    """The photo's current render if it is already on disk (no rendering)."""
    photo = db.get_photo(photo_id)
    if photo is None:
        return None  # the executor job answers the 404
    settings = store.get()
    key = pipeline.key_for(pipeline.effective_recipe(photo, settings), settings)
    return RenderCache(paths).read_png(photo_id, key)


@router.get("/{photo_id}/render")
async def get_render(
    photo_id: str,
    db: DbDep,
    store: SettingsDep,
    paths: PathsDep,
    executor: RenderExecutorDep,
    recipe: str | None = None,
) -> Response:
    """The render PNG for ``recipe`` (a render key).  A cached key is served
    straight from disk with an immutable cache header; the photo's current
    key is rendered on demand; any other unknown key is a 404.  Without
    ``recipe`` the current render is served with ``no-cache``.  Only a miss
    goes to the render executor."""
    _require_id(photo_id)
    if recipe is not None and not valid_key(recipe):
        raise HTTPException(status_code=404, detail=f"photo {photo_id} has no render {recipe!r}")
    if recipe is not None:
        data = await run_in_threadpool(RenderCache(paths).read_png, photo_id, recipe)
    else:
        data = await run_in_threadpool(_current_from_disk, photo_id, db, store, paths)
    if data is None:
        data = await _run(executor, _render_job, photo_id, recipe, db, store, paths)
    headers = {"Cache-Control": IMMUTABLE if recipe is not None else NO_CACHE}
    return Response(content=data, media_type="image/png", headers=headers)


def _previews_job(
    photo_id: str,
    overrides: dict[str, Any],
    db: Database,
    store: SettingsStore,
    paths: DataPaths,
    methods: tuple[DitherMethod, ...] = METHODS,
) -> list[dict[str, Any]]:
    photo = _get_photo(db, photo_id)
    settings = store.get()
    effective = pipeline.effective_recipe(photo, settings)
    base = pipeline.complete_recipe(overrides, base=effective)
    cache = RenderCache(paths)
    renders = cache.render_methods(photo, base, settings, methods=methods)
    keep = {r.key for r in renders} | {pipeline.key_for(effective, settings)}
    cache.prune(photo_id, keep_keys=keep)
    return [
        {
            "method": r.method.id,
            "name": r.method.name,
            "description": r.method.description,
            "kind": r.method.kind,
            "key": r.key,
            "url": render_url(photo_id, r.key),
        }
        for r in renders
    ]


def _methods_filter(value: Any) -> tuple[DitherMethod, ...]:
    """The ``methods`` of a previews body: None for all of them, else a
    non-empty list of known method ids (duplicates are fine)."""
    if value is None:
        return METHODS
    if not isinstance(value, list) or not value:
        raise HTTPException(
            status_code=422, detail="methods must be a non-empty list of method ids"
        )
    unknown = [m for m in value if not isinstance(m, str) or m not in METHODS_BY_ID]
    if unknown:
        raise HTTPException(status_code=422, detail=f"methods: unknown method(s) {unknown!r}")
    wanted = set(value)
    return tuple(m for m in METHODS if m.id in wanted)


@router.post("/{photo_id}/previews")
async def make_previews(
    photo_id: str,
    db: DbDep,
    store: SettingsDep,
    paths: PathsDep,
    executor: RenderExecutorDep,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Render the photo with every dither method (registry order) and the
    given knobs (``brightness, contrast, saturation, gamma, sharpen,
    serpentine, strength, distance, palette``; any subset, the rest from the
    effective recipe; ``method`` is ignored).  Cached renders are reused, so
    repeating a call is cheap.

    ``"methods": [id, ...]`` limits the answer to those methods (still in
    registry order): the editor's live preview asks for the selected method
    alone, so a knob change costs one dither instead of twelve on the Pi."""
    _require_id(photo_id)
    overrides = dict(body or {})
    overrides.pop("method", None)
    methods = _methods_filter(overrides.pop("methods", None))
    t0 = time.perf_counter()
    previews = await _run(executor, _previews_job, photo_id, overrides, db, store, paths, methods)
    return {"previews": previews, "took_ms": round((time.perf_counter() - t0) * 1000)}


# --- recipe / flags / delete ---------------------------------------------------------------


def _recipe_job(
    photo_id: str,
    patch: dict[str, Any] | None,
    db: Database,
    store: SettingsStore,
    paths: DataPaths,
) -> dict[str, Any]:
    photo = _get_photo(db, photo_id)
    settings = store.get()
    if patch is None:
        db.set_photo_recipe(photo_id, None)
    elif patch:
        recipe = pipeline.complete_recipe(patch, base=pipeline.effective_recipe(photo, settings))
        db.set_photo_recipe(photo_id, recipe)
    # no fields at all ({} or {"reset": false}): the override stays as it is,
    # so a photo that follows the default is not pinned to today's default
    photo = _get_photo(db, photo_id)
    cache = RenderCache(paths)
    key = _render_quietly(cache, photo, settings)
    if key is not None:
        cache.prune(photo_id, keep_keys={key})
    return photo_json(photo, settings, cache)


@router.put("/{photo_id}/recipe")
async def put_recipe(
    photo_id: str,
    body: Annotated[dict[str, Any], Body()],
    db: DbDep,
    store: SettingsDep,
    paths: PathsDep,
    executor: RenderExecutorDep,
) -> dict[str, Any]:
    """Set the photo's recipe override: a full or partial recipe merged over
    its effective recipe (422 naming the field on error), then render it.
    ``{"reset": true}`` drops the override so the photo follows the default;
    a body without recipe fields (``{}``, ``{"reset": false}``) changes
    nothing."""
    _require_id(photo_id)
    patch: dict[str, Any] | None = dict(body)
    if "reset" in patch:
        reset = patch.pop("reset")
        if not isinstance(reset, bool):
            raise HTTPException(
                status_code=422, detail=f"reset must be true or false, got {reset!r}"
            )
        if reset:
            if patch:
                raise HTTPException(
                    status_code=422, detail="reset cannot be combined with recipe fields"
                )
            patch = None
    return await _run(executor, _recipe_job, photo_id, patch, db, store, paths)


@router.put("/{photo_id}/flags")
def put_flags(
    photo_id: str,
    body: Annotated[dict[str, Any], Body()],
    db: DbDep,
    store: SettingsDep,
    paths: PathsDep,
) -> dict[str, Any]:
    """``{"excluded": bool}``: exclude the photo from (or include it in) rotation."""
    _require_id(photo_id)
    unknown = sorted(k for k in body if k != "excluded")
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"flags has unknown key(s): {', '.join(unknown)}"
        )
    if "excluded" not in body:
        raise HTTPException(status_code=422, detail="flags.excluded is required")
    excluded = body["excluded"]
    if not isinstance(excluded, bool):
        raise HTTPException(
            status_code=422, detail=f"flags.excluded must be true or false, got {excluded!r}"
        )
    if not db.set_photo_excluded(photo_id, excluded):
        raise HTTPException(status_code=404, detail=f"no photo {photo_id!r}")
    photo = db.get_photo(photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail=f"no photo {photo_id!r}")
    return photo_json(photo, store.get(), RenderCache(paths))


def _delete_job(photo_id: str, db: Database, paths: DataPaths) -> None:
    photo = _get_photo(db, photo_id)
    try:
        pipeline.master_file(photo, paths).unlink(missing_ok=True)
    except pipeline.MasterMissing:  # a stored path outside masters/ is never touched
        log.warning("photo %s: not deleting its out-of-place master path", photo_id)
    masters.delete_master(paths, photo_id)
    RenderCache(paths).invalidate(photo_id)
    pipeline.forget_fit(photo_id)
    db.delete_photo(photo_id)  # the row goes last: a crash leaves a photo that fails to render
    log.info("deleted photo %s (%s)", photo_id, photo.original_name)


@router.delete("/{photo_id}", status_code=204)
async def delete_photo(
    photo_id: str, db: DbDep, paths: PathsDep, executor: RenderExecutorDep
) -> Response:
    """Delete the master, every render, then the database row."""
    _require_id(photo_id)
    await _run(executor, _delete_job, photo_id, db, paths)
    return Response(status_code=204)
