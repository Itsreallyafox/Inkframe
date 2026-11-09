"""Re-render the app cards in ``docs/previews/``.

Run from the repo root with the native dither library built:

    .venv/Scripts/python scripts/render_previews.py                   # every app
    .venv/Scripts/python scripts/render_previews.py countdown trivia  # just these
    .venv/Scripts/python scripts/render_previews.py --out /tmp/cards patterns

For each app named (default: every id in ``APP_IDS``) the script renders
every layout variant with the app's default settings to
``<out>/<app>-v<N>.png``, then the named states it knows (``STATES`` below)
to ``<out>/<app>-<state>.png``: one card per Countdown event and Christmas
Day, and Trivia's "on this day" list primed from the test fixture.  Cards are
rendered exactly as the Apps page preview renders them (``render_to_frame``
with the default recipe) and saved in the mock measured palette
(``MEASURED_DEFAULT``), with a fixed clock (9 November 2026, 09:30 US
Eastern) and seed, so a run is reproducible.

Nothing touches the network or the real data directory: a throw-away data
dir holds the caches.  Weather therefore renders its "where are you?" card
and Headlines its offline card; their data cards in ``docs/previews/`` were
captured by hand and are only replaced when you name those apps.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inkframe.apps import APP_IDS, load_app  # noqa: E402
from inkframe.apps.base import (  # noqa: E402
    App,
    FontLibrary,
    RenderContext,
    render_to_frame,
    validate_settings,
)
from inkframe.config import Settings  # noqa: E402
from inkframe.db import Database  # noqa: E402
from inkframe.display.palette import indices_to_image  # noqa: E402
from inkframe.paths import DataPaths  # noqa: E402
from inkframe.scheduler import build_render_context  # noqa: E402

OUT_DIR = ROOT / "docs" / "previews"
FIXTURES = ROOT / "tests" / "fixtures"
#: 9 November 2026, 09:30 US Eastern (standard time)
NOW = datetime(2026, 11, 9, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
SEED = 7


@dataclass(frozen=True)
class State:
    """One named card, saved as ``<app>-<name>.png``."""

    name: str
    #: settings over the schema defaults
    settings: Mapping[str, Any] = field(default_factory=dict)
    now: datetime = NOW
    #: a layout to force; ``None`` lets the app pick
    variant: int | None = None
    #: runs with the render context before the render, to seed a cache
    prime: Callable[[RenderContext], None] | None = None


def _countdown_states() -> list[State]:
    """Every event pinned on the poster layout, plus Christmas Day itself."""
    from inkframe.apps import countdown

    extra = {
        "birthday": {"birthday": "11-22", "birthday_name": "Sam"},
        "custom": {"custom_date": "2027-04-03", "custom_label": "Our trip"},
    }
    states = [
        State(key.replace("_", "-"), {"event": key, **extra.get(key, {})}, variant=0)
        for key in countdown.EVENT_KEYS
    ]
    christmas = NOW.replace(month=12, day=25)
    states.append(State("christmas-day", {"event": "christmas"}, now=christmas, variant=0))
    return states


def _prime_onthisday(ctx: RenderContext) -> None:
    """Put the recorded Wikimedia response in the day cache, as a fetch would."""
    from inkframe.apps import trivia_data

    payload = json.loads((FIXTURES / "trivia" / "onthisday-11-09.json").read_text(encoding="utf-8"))
    events = trivia_data.parse_onthisday(payload)
    trivia_data.save_day_cache(ctx.cache_dir, ctx.now.date(), events, ctx.now)


def _trivia_states() -> list[State]:
    return [State("onthisday", {"source": "onthisday"}, variant=1, prime=_prime_onthisday)]


#: the named states per app; apps not listed get their variants only
STATES: dict[str, Callable[[], list[State]]] = {
    "countdown": _countdown_states,
    "trivia": _trivia_states,
}


def render_card(
    app: App,
    *,
    settings: Settings,
    db: Database,
    paths: DataPaths,
    fonts: FontLibrary,
    now: datetime,
    variant: int | None,
    overrides: Mapping[str, Any] | None = None,
    prime: Callable[[RenderContext], None] | None = None,
):
    """One card as the preview endpoint would make it, as a PIL image in the
    measured palette."""
    ctx = build_render_context(
        app,
        settings=settings,
        db=db,
        paths=paths,
        now=now,
        fonts=fonts,
        online=False,
        variant=variant,
        seed=SEED,
    )
    if overrides:
        ctx = replace(ctx, settings=validate_settings(app.settings_schema, dict(overrides)))
    if prime is not None:
        prime(ctx)
    frame = render_to_frame(
        app, ctx, recipe=settings.default_recipe, render_palette=settings.render_palette()
    )
    return indices_to_image(frame, settings.measured_palette())


def render_all(app_ids: Iterable[str], out: Path) -> list[Path]:
    """Render the variants and the named states of ``app_ids`` into ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    fonts = FontLibrary()
    settings = Settings()
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="inkframe-previews-") as tmp:
        paths = DataPaths(Path(tmp)).ensure_dirs()
        db = Database(paths.db_file)
        try:
            for app_id in app_ids:
                app = load_app(app_id)
                common = dict(settings=settings, db=db, paths=paths, fonts=fonts)
                for v in range(app.variants):
                    img = render_card(app, now=NOW, variant=v, **common)
                    written.append(_save(img, out / f"{app_id}-v{v}.png"))
                for state in STATES.get(app_id, list)():
                    img = render_card(
                        app,
                        now=state.now,
                        variant=state.variant,
                        overrides=state.settings,
                        prime=state.prime,
                        **common,
                    )
                    written.append(_save(img, out / f"{app_id}-{state.name}.png"))
        finally:
            db.close()
    return written


def _save(img, path: Path) -> Path:
    img.save(path, format="PNG", optimize=True)
    print(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "apps", nargs="*", metavar="APP", help=f"app ids (default: all: {', '.join(APP_IDS)})"
    )
    parser.add_argument(
        "--out", type=Path, default=OUT_DIR, help="output directory (default: docs/previews)"
    )
    args = parser.parse_args(argv)
    unknown = [a for a in args.apps if a not in APP_IDS]
    if unknown:
        parser.error(f"unknown app(s): {', '.join(unknown)}; known: {', '.join(APP_IDS)}")
    logging.basicConfig(level=logging.WARNING)
    written = render_all(args.apps or APP_IDS, args.out)
    print(f"{len(written)} cards written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
