"""Settings: defaults, JSON round-trip, forward-compatible loading, validation, store."""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

import pytest

from inkframe.config import (
    DEFAULT_RECIPE,
    MAX_PHOTOS_IN_A_ROW,
    AppTurn,
    PanelSettings,
    PhotoStream,
    RotationSettings,
    Settings,
    SettingsStore,
    deep_merge,
    load_settings,
    parse_hhmm,
    save_settings,
    validate_recipe,
)
from inkframe.display.palette import BLUE, MEASURED_DEFAULT, NOMINAL
from tests.conftest import legacy_slot


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
    assert s.rotation.photos == PhotoStream(
        enabled=True, in_a_row=6, minutes_each=3, order="shuffle"
    )
    assert [(t.app_id, t.minutes, t.enabled) for t in s.rotation.apps] == [
        ("weather", 3, True),
        ("headlines", 3, True),
        ("quotes", 2, True),
        ("patterns", 2, True),
    ]
    assert s.rotation.cycle_minutes() == 28  # 18 min of photos (64 %) + 10 min of cards
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
            "rotation": {
                "photos": {"in_a_row": 4, "minutes_each": 5, "order": "sequential"},
                "apps": [{"app_id": "quotes", "enabled": False, "minutes": 7}],
            },
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
    assert s.rotation == Settings().rotation


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
        (
            {"rotation": {"photos": {"in_a_row": 0}}},
            "rotation.photos.in_a_row must be between 1 and 200, got 0",
        ),
        ({"rotation": {"photos": {"in_a_row": 201}}}, "in_a_row must be between 1 and 200"),
        ({"rotation": {"photos": {"in_a_row": 2.5}}}, "rotation.photos.in_a_row must be a whole"),
        (
            {"rotation": {"photos": {"minutes_each": 0}}},
            "rotation.photos.minutes_each must be between 1 and 1440, got 0",
        ),
        ({"rotation": {"photos": {"minutes_each": 1441}}}, "minutes_each must be between 1 and"),
        (
            {"rotation": {"photos": {"order": "random"}}},
            "rotation.photos.order must be shuffle or sequential, got 'random'",
        ),
        ({"rotation": {"photos": {"enabled": "yes"}}}, "rotation.photos.enabled must be true"),
        ({"rotation": {"photos": {"dwell_minutes": 3}}}, "rotation.photos has unknown key"),
        ({"rotation": {"photos": []}}, "rotation.photos must be an object"),
        (
            {"rotation": {"apps": [{"app_id": "weather", "minutes": 0}]}},
            r"rotation.apps\[0\].minutes must be between 1 and 1440, got 0",
        ),
        (
            {"rotation": {"apps": [{"app_id": "weather"}, {"app_id": "quotes", "minutes": 1441}]}},
            r"rotation.apps\[1\].minutes must be between 1 and 1440, got 1441",
        ),
        (
            {"rotation": {"apps": [{"app_id": "weather", "enabled": 1}]}},
            r"rotation.apps\[0\].enabled must be true or false",
        ),
        (
            {"rotation": {"apps": [{"app_id": "Weather!"}]}},
            r"rotation.apps\[0\].app_id must be a lowercase identifier",
        ),
        (
            {"rotation": {"apps": [{"minutes": 3}]}},
            r"rotation.apps\[0\] is missing key\(s\): app_id",
        ),
        (
            {
                "rotation": {
                    "apps": [{"app_id": "weather"}, {"app_id": "quotes"}, {"app_id": "weather"}]
                }
            },
            r"rotation.apps\[2\].app_id: duplicate of rotation.apps\[0\]",
        ),
        (
            {"rotation": {"apps": [{"app_id": "photos"}]}},
            r"rotation.apps\[0\].app_id: photos belongs in rotation.photos",
        ),
        (
            {"rotation": {"apps": [{"app_id": "weather", "duration_minutes": 3}]}},
            r"rotation.apps\[0\] has unknown key\(s\): duration_minutes",
        ),
        ({"rotation": {"apps": ["weather"]}}, r"rotation.apps\[0\] must be an object"),
        ({"rotation": {"apps": {"app_id": "weather"}}}, "rotation.apps must be a list"),
        ({"rotation": {"apps": [{"app_id": f"app{i}"} for i in range(33)]}}, "at most 32"),
        ({"rotation": {"cards": []}}, "rotation has unknown key"),
        ({"rotation": "photos"}, "rotation must be an object"),
        # the 0.1 slot list is converted, and its fields are still checked
        ({"rotation": [{"app_id": "Photos!", "duration_minutes": 3}]}, r"rotation\[0\].app_id"),
        (
            {"rotation": [{"app_id": "photos", "duration_minutes": 0}]},
            r"rotation\[0\].duration_minutes",
        ),
        (
            {"rotation": [{"app_id": "weather", "duration_minutes": 1441}]},
            r"rotation\[0\].duration_minutes",
        ),
        (
            {"rotation": [{"app_id": "weather", "duration_minutes": 3, "enabled": "no"}]},
            r"rotation\[0\].enabled",
        ),
        ({"rotation": [{"app_id": "photos"}]}, "missing"),
        (
            {"rotation": [{"app_id": "weather", "duration_minutes": 3, "colour": 1}]},
            r"rotation\[0\] has unknown key",
        ),
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
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.rotation.photos.order = "sequential"  # type: ignore[misc]
    d = s.to_dict()
    d["rotation"]["apps"].clear()
    d["rotation"]["photos"]["order"] = "sequential"
    assert len(s.rotation.apps) == 4 and s.rotation.photos.order == "shuffle"


def test_merge_replaces_lists_and_merges_dicts() -> None:
    s = Settings().merged({"rotation": {"apps": [{"app_id": "quotes", "minutes": 5}]}})
    assert s.rotation.apps == (AppTurn("quotes", minutes=5),)  # the list is replaced whole
    assert s.rotation.photos == Settings().rotation.photos  # the photo stream is kept
    s2 = s.merged({"quiet_hours": {"enabled": False}})
    assert s2.quiet_hours.enabled is False and s2.quiet_hours.start == "23:00"
    assert deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}}) == {"a": {"b": 1, "c": [2]}}


# --- rotation: the 0.2 model and the 0.1 slot list ------------------------------------------

#: the deployed frame's settings.json before 0.2
LEGACY_FRAME = [
    {
        "app_id": "photos",
        "enabled": True,
        "duration_minutes": 10,
        "options": {"dwell_minutes": 3, "order": "shuffle"},
    },
    {"app_id": "weather", "enabled": True, "duration_minutes": 3, "options": {}},
    {"app_id": "headlines", "enabled": True, "duration_minutes": 3, "options": {}},
    {"app_id": "quotes", "enabled": True, "duration_minutes": 2, "options": {}},
    {"app_id": "patterns", "enabled": True, "duration_minutes": 2, "options": {}},
]


def test_rotation_partial_dict_patches_the_photo_stream_only() -> None:
    s = Settings().merged({"rotation": {"photos": {"in_a_row": 8}}})
    assert s.rotation.photos == PhotoStream(in_a_row=8, minutes_each=3, order="shuffle")
    assert s.rotation.apps == Settings().rotation.apps
    assert s.rotation.cycle_minutes() == 34


def test_rotation_apps_default_minutes_and_enabled() -> None:
    s = Settings().merged({"rotation": {"apps": [{"app_id": "weather"}]}})
    assert s.rotation.apps == (AppTurn("weather", enabled=True, minutes=3),)
    off = Settings().merged({"rotation": {"photos": {"enabled": False}}})
    assert off.rotation.cycle_minutes() == 10
    assert RotationSettings(apps=()).cycle_minutes() == 18


def test_legacy_list_converts_the_deployed_frame(tmp_path: Path) -> None:
    """Adam's frame: photos 10 min at 3 per photo -> 3 in a row; the four
    cards keep their minutes and order; the file is upgraded on save."""
    for strict in (True, False):
        s = Settings.from_dict({"rotation": LEGACY_FRAME}, strict=strict)
        assert s.rotation.photos == PhotoStream(enabled=True, in_a_row=3, minutes_each=3)
        assert [(t.app_id, t.minutes, t.enabled) for t in s.rotation.apps] == [
            ("weather", 3, True),
            ("headlines", 3, True),
            ("quotes", 2, True),
            ("patterns", 2, True),
        ]
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"rotation": LEGACY_FRAME}), encoding="utf-8")
    store = SettingsStore(path)  # boots on the old file ...
    assert store.get().rotation.photos.in_a_row == 3
    store.patch({"paused": True})  # ... and the first save writes the new shape
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["rotation"] == store.get().rotation.to_dict()
    assert set(on_disk["rotation"]) == {"photos", "apps"}
    # a legacy list through merged() (PUT /api/settings) replaces the whole section
    s = Settings().merged({"rotation": [{"app_id": "quotes", "duration_minutes": 5}]})
    assert s.rotation == RotationSettings(
        photos=PhotoStream(enabled=False), apps=(AppTurn("quotes", minutes=5),)
    )


@pytest.mark.parametrize(
    ("slots", "photos", "app_ids"),
    [
        # photos first, in the middle, absent, disabled
        (
            [{"app_id": "photos", "duration_minutes": 20, "options": {"dwell_minutes": 4}}],
            PhotoStream(enabled=True, in_a_row=5, minutes_each=4, order="shuffle"),
            [],
        ),
        (
            [
                {"app_id": "alpha", "duration_minutes": 1},
                {"app_id": "photos", "duration_minutes": 10, "options": {"dwell_minutes": 3}},
                {"app_id": "beta", "duration_minutes": 1},
            ],
            PhotoStream(enabled=True, in_a_row=3, minutes_each=3),
            ["alpha", "beta"],
        ),
        ([{"app_id": "alpha", "duration_minutes": 1}], PhotoStream(enabled=False), ["alpha"]),
        ([], PhotoStream(enabled=False), []),
        (
            [{"app_id": "photos", "enabled": False, "duration_minutes": 10}],
            PhotoStream(enabled=False, in_a_row=3, minutes_each=3),
            [],
        ),
        # a bad dwell or order falls back to 3 / shuffle (halves round up: 20 / 8 -> 3)
        (
            [{"app_id": "photos", "duration_minutes": 20, "options": {"dwell_minutes": 8}}],
            PhotoStream(in_a_row=3, minutes_each=8),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 1, "options": {"dwell_minutes": 2.5}}],
            PhotoStream(in_a_row=1, minutes_each=3),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 9, "options": {"dwell_minutes": 0}}],
            PhotoStream(in_a_row=3, minutes_each=3),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 9, "options": {"dwell_minutes": True}}],
            PhotoStream(in_a_row=3, minutes_each=3),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 9, "options": {"order": "random"}}],
            PhotoStream(in_a_row=3, minutes_each=3, order="shuffle"),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 9, "options": {"order": "sequential"}}],
            PhotoStream(in_a_row=3, minutes_each=3, order="sequential"),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 9, "options": "none"}],
            PhotoStream(in_a_row=3, minutes_each=3),
            [],
        ),
        # duplicate apps keep the first; extra photos slots are ignored
        (
            [
                {"app_id": "alpha", "duration_minutes": 1},
                {"app_id": "beta", "duration_minutes": 2},
                {"app_id": "alpha", "duration_minutes": 9},
                {"app_id": "photos", "duration_minutes": 6, "options": {"dwell_minutes": 2}},
                {"app_id": "photos", "duration_minutes": 60, "options": {"dwell_minutes": 1}},
            ],
            PhotoStream(in_a_row=3, minutes_each=2),
            ["alpha", "beta"],
        ),
        # a 0.1-valid slot whose duration / dwell exceeds the new cap is clamped
        (
            [{"app_id": "photos", "duration_minutes": 1440, "options": {"dwell_minutes": 1}}],
            PhotoStream(in_a_row=MAX_PHOTOS_IN_A_ROW, minutes_each=1),
            [],
        ),
        (
            [{"app_id": "photos", "duration_minutes": 700}],
            PhotoStream(in_a_row=MAX_PHOTOS_IN_A_ROW, minutes_each=3),
            [],
        ),
    ],
)
def test_legacy_list_conversion_cases(slots: list, photos: PhotoStream, app_ids: list) -> None:
    for strict in (True, False):
        r = RotationSettings.from_dict(slots, strict=strict)
        assert r.photos == photos
        assert [t.app_id for t in r.apps] == app_ids


def test_legacy_photos_slot_over_the_in_a_row_cap_is_clamped_not_rejected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 0.1 photos slot of 600 min at 1 per photo (valid then) converts to
    the 200 cap with a warning instead of failing validation on a key the
    file does not contain; the rest of the file (this frame's 270 rotation,
    the cards) is kept, nothing is moved to settings.json.invalid."""
    legacy = [legacy_slot("photos", 600, dwell_minutes=1), legacy_slot("weather", 3)]
    with caplog.at_level("WARNING", logger="inkframe.config"):
        settings = Settings.from_dict({"rotation": legacy}, strict=False)
    assert settings.rotation.photos == PhotoStream(in_a_row=MAX_PHOTOS_IN_A_ROW, minutes_each=1)
    assert [t.app_id for t in settings.rotation.apps] == ["weather"]
    assert any("capped at 200" in rec.getMessage() for rec in caplog.records)

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"display": {"rotation": 270}, "rotation": legacy}), encoding="utf-8"
    )
    store = SettingsStore(path)
    assert not (tmp_path / "settings.json.invalid").exists()
    assert store.get().display.rotation == 270
    assert store.get().rotation.photos.in_a_row == MAX_PHOTOS_IN_A_ROW
    assert [t.app_id for t in store.get().rotation.apps] == ["weather"]


def test_legacy_list_keeps_app_minutes_and_drops_options() -> None:
    r = RotationSettings.from_dict(
        [
            {"app_id": "alpha", "duration_minutes": 7, "enabled": False, "options": {"x": 1}},
            {"app_id": "alpha", "duration_minutes": 9},
        ]
    )
    assert r.apps == (AppTurn("alpha", enabled=False, minutes=7),)
    assert r.to_dict()["apps"] == [{"app_id": "alpha", "enabled": False, "minutes": 7}]


def test_rotation_unknown_keys_strict_vs_tolerant(caplog: pytest.LogCaptureFixture) -> None:
    data = {
        "photos": {"in_a_row": 2, "zoom": 1},
        "apps": [{"app_id": "weather", "colour": "red"}],
        "cards": [],
    }
    with pytest.raises(ValueError, match="rotation has unknown key"):
        RotationSettings.from_dict(data, strict=True)
    with caplog.at_level("WARNING", logger="inkframe.config"):
        r = RotationSettings.from_dict(data, strict=False)
    assert r.photos.in_a_row == 2 and r.apps == (AppTurn("weather"),)
    warned = " ".join(rec.getMessage() for rec in caplog.records)
    assert "cards" in warned and "zoom" in warned and "colour" in warned


def test_rotation_dataclasses_validate_directly() -> None:
    with pytest.raises(ValueError, match="rotation.photos.in_a_row"):
        PhotoStream(in_a_row=0)
    with pytest.raises(ValueError, match="minutes must be between 1 and 1440, got 0"):
        AppTurn("weather", minutes=0)
    with pytest.raises(ValueError, match=r"rotation.apps\[1\].app_id: duplicate"):
        RotationSettings(apps=(AppTurn("a"), AppTurn("a")))
    with pytest.raises(ValueError, match="photos belongs in rotation.photos"):
        RotationSettings(apps=(AppTurn("photos"),))
    with pytest.raises(ValueError, match="rotation.photos must be a PhotoStream"):
        RotationSettings(photos={"in_a_row": 3})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rotation must be a RotationSettings"):
        Settings(rotation=[])  # type: ignore[arg-type]
    assert RotationSettings(apps=[AppTurn("a")]).apps == (AppTurn("a"),)  # a list is tupled


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
