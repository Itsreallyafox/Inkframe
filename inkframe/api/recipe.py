"""``GET/PUT /api/recipe/default``: the recipe new uploads (and every photo
without an override) render with, plus what the editor needs to draw its
controls.

Response shape (both methods)::

    {
      "method": "atkinson", "serpentine": true, "strength": 0.85,
      "distance": "lab", "brightness": 1.0, "contrast": 1.0,
      "saturation": 1.1, "gamma": 1.0, "sharpen": 0.0, "palette": "measured",
      "methods": [{"id", "name", "description", "kind"}, ...],   # registry order
      "limits": {"strength": {"min": 0.5, "max": 1.0}, ...,
                 "distance": {"choices": ["rgb", "lab"]},
                 "palette": {"choices": ["nominal", "measured"]}}
    }

``PUT`` takes a full or partial recipe (``methods`` and ``limits`` are
ignored, so a ``GET`` body can be edited and sent back), merges it over the
current default, validates it (422 naming the field) and saves it in
``settings.json``.  Existing photos are not re-rendered eagerly: their render
keys change and the new renders happen lazily when the grid asks for them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from inkframe.api import SettingsDep
from inkframe.config import RECIPE_DISTANCES, RECIPE_PALETTES, RECIPE_RANGES, Settings
from inkframe.render import pipeline
from inkframe.render.dither import METHODS

router = APIRouter(prefix="/api/recipe", tags=["recipe"])

#: read-only keys of the response that ``PUT`` accepts and ignores
META_KEYS = ("methods", "limits")


def methods_json() -> list[dict[str, str]]:
    """The dither registry as the UI's method strip needs it."""
    return [
        {"id": m.id, "name": m.name, "description": m.description, "kind": m.kind} for m in METHODS
    ]


def limits_json() -> dict[str, Any]:
    """Slider ranges and choice lists for every recipe knob that has one."""
    out: dict[str, Any] = {k: {"min": lo, "max": hi} for k, (lo, hi) in RECIPE_RANGES.items()}
    out["distance"] = {"choices": list(RECIPE_DISTANCES)}
    out["palette"] = {"choices": list(RECIPE_PALETTES)}
    return out


def _payload(settings: Settings) -> dict[str, Any]:
    return {
        **pipeline.default_recipe(settings),
        "methods": methods_json(),
        "limits": limits_json(),
    }


@router.get("/default")
def read_default_recipe(store: SettingsDep) -> dict[str, Any]:
    """The complete default recipe, the method registry and the knob limits."""
    return _payload(store.get())


@router.put("/default")
def update_default_recipe(
    body: Annotated[dict[str, Any], Body()], store: SettingsDep
) -> dict[str, Any]:
    """Merge a full or partial recipe over the default and save it."""
    patch = {k: v for k, v in body.items() if k not in META_KEYS}

    def merge(current: Settings) -> Settings:
        recipe = pipeline.complete_recipe(
            patch, base=pipeline.default_recipe(current)
        )  # RecipeError (a ValueError) aborts
        return current.merged({"default_recipe": recipe})

    try:
        # ``update`` is ``patch`` with the merge done under the store's lock,
        # so two concurrent PUTs cannot lose each other's fields
        settings = store.update(merge)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _payload(settings)
