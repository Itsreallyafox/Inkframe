"""Settings: defaults, JSON round-trip, forward-compatible loading, validation, store."""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

import pytest

from inkframe.config import (
    DEFAULT_RECIPE,
    PanelSettings,
    RotationSlot,
    Settings,
    SettingsStore,
    deep_merge,
    load_settings,
    parse_hhmm,
    save_settings,
    validate_recipe,
)
from inkframe.display.palette import BLUE, MEASURED_DEFAULT, NOMINAL


def test_defaults_match_plan() -> None:
    s = Settings()
    assert s.display.rotation == 90 and s.display.clear_before_refresh is False
    assert s.panel.refresh_floor_seconds == 180
    assert s.panel.show_now_min_seconds == 30
    assert s.panel.keepalive_hours == 20
    q = s.quiet_hours
    assert (q.enabled, q.start, q.end, q.days) == (True, "23:00", "07:00", (0, 1, 2, 3, 4, 5, 6))
    assert s.palette.use_measured is True
    assert s.measured_palette().rgb == MEASURED_DEFAULT.rgb
    assert dict(s.default_recipe) == {
        "method": "atkinson",
        "serpentine": True,
        "strength": 0.85,
        "distance": "lab",
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.1,
        "gamma": 1.0,
        "sharpen": 0.0,
        "palette": "measured",
    }
    assert [(r.app_id, r.duration_minutes, r.enabled) for r in s.rotation] == [
        ("photos", 10, True),
        ("weather", 3, True),
        ("headlines", 3, True),
        ("quotes", 2, True),
        ("patterns", 2, True),
    ]
    assert dict(s.rotation[0].options) == {"dwell_minutes": 3, "order": "shuffle"}
    assert dict(s.rotation[1].options) == {}
    assert s.paused is False


def test_render_palette_follows_use_measured() -> None:
    s = Settings()
    assert s.render_palette() == s.measured_palette()
    off = s.merged({"palette": {"use_measured": False}})
    assert off.render_palette() == NOMINAL
    assert off.measured_palette().rgb == MEASURED_DEFAULT.rgb


def test_to_dict_is_json_and_round_trips(tmp_path: Path) -> None:
    s = Settings().merged(
        {
            "panel": {"refresh_floor_seconds": 600},
            "quiet_hours": {"days": [5, 6]},
            "palette": {"measured": {"blue": [30, 50, 140]}},
            "rotation": [
                {"app_id": "photos", "duration_minutes": 20, "options": {"dwell_minutes": 5}}
            ],
            "paused": True,
        }
    )
    text = json.dumps(s.to_dict())
    assert Settings.from_dict(json.loads(text)) == s
    path = tmp_path / "settings.json"
    save_settings(s, path)
    assert load_settings(path) == s
    assert json.loads(path.read_text(encoding="utf-8")) == s.to_dict()


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_partial_file_gets_defaults_merged(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "panel": {"refresh_floor_seconds": 300},
                "palette": {"measured": {"red": [200, 40, 30]}},
                "default_recipe": {"method": "floyd_steinberg", "added_later": 1},
                "from_the_future": {"x": 1},
            }
        ),
        encoding="utf-8",
    )
    s = load_settings(path)
    assert s.panel.refresh_floor_seconds == 300
    assert s.panel.keepalive_hours == 20
    assert s.quiet_hours == Settings().quiet_hours
    assert s.measured_palette().as_dict()["red"] == [200, 40, 30]
    assert s.measured_palette().as_dict()["blue"] == list(MEASURED_DEFAULT.rgb[BLUE])
    assert s.default_recipe["method"] == "floyd_steinberg"
    assert s.default_recipe["strength"] == 0.85
    assert "added_later" not in s.default_recipe
    assert len(s.rotation) == 5


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"panel": {"refresh_floor_seconds": 10}}, "panel.refresh_floor_seconds"),
        ({"panel": {"refresh_floor_seconds": 120.5}}, "whole number"),
        ({"panel": {"keepalive_hours": 24}}, "panel.keepalive_hours"),
        ({"panel": {"show_now_min_seconds": 600}}, "must not exceed"),
        (
            {"panel": {"refresh_floor_seconds": 7200, "keepalive_hours": 1}},
            r"refresh_floor_seconds \(7200\) must not exceed panel.keepalive_hours",
        ),
        ({"display": {"rotation": 180}}, "display.rotation"),
        ({"display": {"clear_before_refresh": "yes"}}, "true or false"),
        ({"quiet_hours": {"start": "25:00"}}, "quiet_hours.start"),
        ({"quiet_hours": {"end": "7:00"}}, "quiet_hours.end"),
        ({"quiet_hours": {"start": "07:00"}}, "must differ"),
        ({"quiet_hours": {"days": [7]}}, r"quiet_hours.days\[0\]"),
        ({"palette": {"measured": {"blue": [1, 2]}}}, "palette.measured.blue"),
        ({"palette": {"measured": {"blue": [1, 2, 300]}}}, r"palette.measured.blue\[2\]"),
        ({"palette": {"measured": {"orange": [1, 2, 3]}}}, "orange"),
        ({"palette": {"measured": "grey"}}, "palette.measured"),
        ({"default_recipe": {"strength": 1.5}}, "default_recipe.strength"),
        ({"default_recipe": {"gamma": 0.1}}, "default_recipe.gamma"),
        ({"default_recipe": {"distance": "cmyk"}}, "default_recipe.distance"),
        ({"default_recipe": {"method": " "}}, "default_recipe.method"),
        ({"default_recipe": {"dither": "x"}}, "unknown key"),
        ({"rotation": [{"app_id": "Photos!", "duration_minutes": 3}]}, r"rotation\[0\].app_id"),
        ({"rotation": [{"app_id": "photos", "duration_minutes": 0}]}, "duration_minutes"),
        ({"rotation": [{"app_id": "photos"}]}, "missing"),
        ({"rotation": {"app_id": "photos"}}, "list"),
        ({"paused": 1}, "paused"),
        ({"bogus": True}, "unknown key"),
        ({"panel": {"bogus": True}}, "panel has unknown key"),
    ],
)
def test_validation_errors(patch: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings().merged(patch)


def test_dataclass_replace_validates() -> None:
    with pytest.raises(ValueError, match="refresh_floor_seconds"):
        dataclasses.replace(Settings().panel, refresh_floor_seconds=10)
    with pytest.raises(ValueError):
        PanelSettings(keepalive_hours=0)
    assert dataclasses.replace(Settings(), paused=True).paused is True


def test_snapshot_is_immutable() -> None:
    s = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.paused = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        s.default_recipe["strength"] = 0.5  # type: ignore[index]
    with pytest.raises(TypeError):
        s.rotation[0].options["order"] = "sequential"  # type: ignore[index]
    d = s.to_dict()
    d["rotation"][0]["options"]["order"] = "sequential"
    assert s.rotation[0].options["order"] == "shuffle"


def test_merge_replaces_lists_and_merges_dicts() -> None:
    s = Settings().merged({"rotation": [{"app_id": "quotes", "duration_minutes": 5}]})
    assert [r.app_id for r in s.rotation] == ["quotes"]
    assert s.rotation[0] == RotationSlot("quotes", 5)
    s2 = s.merged({"quiet_hours": {"enabled": False}})
    assert s2.quiet_hours.enabled is False and s2.quiet_hours.start == "23:00"
    assert deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}}) == {"a": {"b": 1, "c": [2]}}


def test_quiet_hours_days_are_normalised() -> None:
    s = Settings().merged({"quiet_hours": {"days": [6, 0, 6, 2]}})
    assert s.quiet_hours.days == (0, 2, 6)
    assert s.quiet_hours.start_time().hour == 23
    assert parse_hhmm("07:05").minute == 5
    with pytest.raises(ValueError):
        parse_hhmm("07:00\n")


def test_validate_recipe() -> None:
    assert validate_recipe(dict(DEFAULT_RECIPE)) == dict(DEFAULT_RECIPE)
    r = validate_recipe({**DEFAULT_RECIPE, "method": "bayer_8", "strength": 1})
    assert r["strength"] == 1.0 and isinstance(r["strength"], float)
    with pytest.raises(ValueError, match="missing"):
        validate_recipe({"method": "none"})
    with pytest.raises(ValueError, match="recipe.serpentine"):
        validate_recipe({**DEFAULT_RECIPE, "serpentine": "on"})
    with pytest.raises(ValueError, match="recipe.sharpen"):
        validate_recipe({**DEFAULT_RECIPE, "sharpen": float("nan")})
    with pytest.raises(ValueError, match="recipe.palette"):
        validate_recipe({**DEFAULT_RECIPE, "palette": "custom"})
    # an integer too large for a float is a ValueError naming the field, not
    # an OverflowError (which PUT /api/settings would answer with a 500)
    with pytest.raises(ValueError, match="recipe.gamma"):
        validate_recipe({**DEFAULT_RECIPE, "gamma": 10**400})


def test_store_recovers_from_a_huge_number_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"default_recipe": {"strength": 1' + "0" * 400 + "}}", encoding="utf-8")
    store = SettingsStore(path)  # boots with the defaults instead of crashing
    assert store.get() == Settings()
    assert (tmp_path / "settings.json.invalid").exists()


def test_store_creates_file_with_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.get() == Settings()
    assert json.loads(path.read_text(encoding="utf-8")) == Settings().to_dict()


def test_store_update_persists_and_swaps(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    before = store.get()
    after = store.patch({"paused": True, "panel": {"refresh_floor_seconds": 240}})
    assert store.get() is after
    assert before.paused is False  # old snapshots never change
    assert SettingsStore(path).get() == after
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]  # no temp files left


def test_store_replace(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    new = dataclasses.replace(Settings(), paused=True)
    assert store.replace(new) == new
    assert load_settings(store.path).paused is True


def test_store_failed_update_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.patch({"paused": True})
    on_disk = path.read_bytes()
    snapshot = store.get()
    with pytest.raises(ValueError):
        store.patch({"panel": {"refresh_floor_seconds": 10}})
    with pytest.raises(TypeError):
        store.update(lambda s: s.to_dict())  # type: ignore[arg-type, return-value]
    assert store.get() is snapshot
    assert path.read_bytes() == on_disk


def test_store_concurrent_updates_are_not_lost(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    n_threads, per_thread = 8, 10
    barrier = threading.Barrier(n_threads)

    def bump(s: Settings) -> Settings:
        panel = dataclasses.replace(
            s.panel, refresh_floor_seconds=s.panel.refresh_floor_seconds + 1
        )
        return dataclasses.replace(s, panel=panel)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            store.update(bump)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = 180 + n_threads * per_thread
    assert store.get().panel.refresh_floor_seconds == expected
    assert load_settings(store.path).panel.refresh_floor_seconds == expected


def test_store_recovers_from_invalid_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"panel": {"refresh_floor_seconds": 5}}', encoding="utf-8")
    with caplog.at_level("ERROR", logger="inkframe.config"):
        store = SettingsStore(path)
    assert store.get() == Settings()
    backup = tmp_path / "settings.json.invalid"
    assert backup.read_text(encoding="utf-8").startswith('{"panel"')
    assert load_settings(path) == Settings()
    assert any("invalid settings file" in r.getMessage() for r in caplog.records)


def test_load_rejects_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_settings(path)
