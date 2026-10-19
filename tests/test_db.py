"""SQLite layer: migrations, photos, per-app settings, state."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from inkframe import db as dbmod
from inkframe.db import SCHEMA_VERSION, Database, Photo, new_id


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "inkframe.db")
    yield database
    database.close()


def add(db: Database, name: str = "a.jpg", **kw) -> Photo:
    defaults = dict(
        original_name=name,
        width=1800,
        height=1200,
        master_path=f"masters/{name}",
        master_bytes=12345,
    )
    defaults.update(kw)
    return db.add_photo(**defaults)


def test_migrations_apply_once(tmp_path: Path) -> None:
    path = tmp_path / "inkframe.db"
    first = Database(path)
    assert first.schema_version == SCHEMA_VERSION == len(dbmod.MIGRATIONS)
    first.set_state("k", 1)
    first.close()
    again = Database(path)  # re-running CREATE TABLE would fail if migrations re-applied
    assert again.schema_version == SCHEMA_VERSION
    assert again.get_state("k") == 1
    again.close()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"photos", "app_settings", "state"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "inkframe.db"
    Database(path).close()
    monkeypatch.setattr(
        dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, "CREATE TABLE extra (x INT); SELECT nope FROM nah"]
    )
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", len(dbmod.MIGRATIONS))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone() is None


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "inkframe.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        Database(path)


def test_photo_crud(db: Database) -> None:
    p = add(db)
    assert len(p.id) == 32 and int(p.id, 16) >= 0
    assert p.portrait is False and p.excluded is False and p.recipe is None
    assert datetime.fromisoformat(p.created_at).utcoffset().total_seconds() == 0
    assert db.get_photo(p.id) == p
    assert db.get_photo("missing") is None

    tall = add(db, "tall.jpg", width=800, height=1200)
    assert tall.portrait is True
    assert [x.id for x in db.list_photos()] == [p.id, tall.id]

    recipe = {"method": "atkinson", "strength": 0.9}
    assert db.set_photo_recipe(p.id, recipe) is True
    assert db.get_photo(p.id).recipe == recipe
    assert db.set_photo_recipe(p.id, None) is True
    assert db.get_photo(p.id).recipe is None
    assert db.set_photo_recipe("missing", recipe) is False

    assert db.set_photo_excluded(tall.id, True) is True
    assert db.get_photo(tall.id).excluded is True
    assert [x.id for x in db.list_photos(include_excluded=False)] == [p.id]
    assert len(db.list_photos()) == 2
    assert db.set_photo_excluded("missing", True) is False

    assert db.delete_photo(p.id) is True
    assert db.delete_photo(p.id) is False
    assert db.get_photo(p.id) is None
    assert [x.id for x in db.list_photos()] == [tall.id]


def test_add_photo_with_given_id_and_dict(db: Database) -> None:
    pid = new_id()
    p = add(db, photo_id=pid, recipe={"method": "none"}, excluded=True)
    assert p.id == pid
    d = db.get_photo(pid).to_dict()
    assert d["recipe"] == {"method": "none"} and d["excluded"] is True
    with pytest.raises(sqlite3.IntegrityError):
        add(db, photo_id=pid)


def test_app_settings(db: Database) -> None:
    assert db.get_app_settings("weather") == {}
    db.set_app_settings("weather", {"lat": 52.1, "lon": 4.3, "place": "Den Haag"})
    assert db.get_app_settings("weather") == {"lat": 52.1, "lon": 4.3, "place": "Den Haag"}
    db.set_app_settings("weather", {"place": "Leiden"})
    assert db.get_app_settings("weather") == {"place": "Leiden"}
    assert db.get_app_settings("headlines") == {}


@pytest.mark.parametrize(
    "value",
    [None, True, 3, 2.5, "text", [1, "two", None], {"type": "photo", "id": "abc", "n": [1, 2]}],
)
def test_state_round_trip(db: Database, value: object) -> None:
    db.set_state("key", value)
    assert db.get_state("key") == value


def test_state_default_and_overwrite(db: Database) -> None:
    assert db.get_state("nope") is None
    assert db.get_state("nope", default={"x": 1}) == {"x": 1}
    db.set_state("last_refresh_at", "2026-10-19T10:00:00.000+00:00")
    db.set_state("last_refresh_at", "2026-10-19T11:00:00.000+00:00")
    assert db.get_state("last_refresh_at") == "2026-10-19T11:00:00.000+00:00"


def test_threads_share_one_connection(db: Database) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                db.set_state(f"t{n}", i)
                add(db, f"{n}-{i}.jpg")
        except BaseException as e:  # surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(db.list_photos()) == 80
    assert [db.get_state(f"t{n}") for n in range(4)] == [19] * 4


def test_close_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.db")
    database.close()
    database.close()
    with pytest.raises(sqlite3.ProgrammingError):
        database.get_state("k")
