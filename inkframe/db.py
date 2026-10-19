"""SQLite storage (stdlib ``sqlite3``): photos, per-app settings, runtime state.

One connection shared by every thread (``check_same_thread=False``) and
serialised by an ``RLock``; SQLite work here is tiny, so a pool would buy
nothing on a Pi Zero.  WAL journal, foreign keys on.  The schema evolves
through ``MIGRATIONS``: entry ``i`` upgrades ``PRAGMA user_version`` from
``i`` to ``i + 1`` inside one transaction.  Append new entries; never edit
an applied one.

Ids are ``uuid4().hex``; timestamps are ISO-8601 UTC strings
(``2026-10-19T12:34:56.789+00:00``), which sort chronologically as text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("inkframe.db")

MIGRATIONS: list[str] = [
    # v1: initial schema
    """
    CREATE TABLE photos (
        id            TEXT PRIMARY KEY,
        original_name TEXT    NOT NULL,
        created_at    TEXT    NOT NULL,
        width         INTEGER NOT NULL,
        height        INTEGER NOT NULL,
        portrait      INTEGER NOT NULL,
        excluded      INTEGER NOT NULL DEFAULT 0,
        recipe        TEXT    NULL,
        master_path   TEXT    NOT NULL,
        master_bytes  INTEGER NOT NULL
    );
    CREATE INDEX photos_created_at ON photos (created_at);
    CREATE TABLE app_settings (
        app_id     TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]

#: the schema version this code expects
SCHEMA_VERSION = len(MIGRATIONS)


def new_id() -> str:
    """A fresh id: ``uuid4().hex`` (32 lowercase hex characters)."""
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """The current time as an ISO-8601 UTC string with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Photo:
    """One uploaded photo.  ``recipe`` is ``None`` when it follows the default."""

    id: str
    original_name: str
    created_at: str
    width: int
    height: int
    portrait: bool
    excluded: bool
    recipe: dict[str, Any] | None
    #: path of the stored master; Phase 3 stores it relative to the data dir
    master_path: str
    master_bytes: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Photo:
        return cls(
            id=row["id"],
            original_name=row["original_name"],
            created_at=row["created_at"],
            width=row["width"],
            height=row["height"],
            portrait=bool(row["portrait"]),
            excluded=bool(row["excluded"]),
            recipe=None if row["recipe"] is None else json.loads(row["recipe"]),
            master_path=row["master_path"],
            master_bytes=row["master_bytes"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "original_name": self.original_name,
            "created_at": self.created_at,
            "width": self.width,
            "height": self.height,
            "portrait": self.portrait,
            "excluded": self.excluded,
            "recipe": None if self.recipe is None else dict(self.recipe),
            "master_path": self.master_path,
            "master_bytes": self.master_bytes,
        }


class Database:
    """Thread-safe access to ``inkframe.db``.  Open once per process."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self._path} has schema version {current}, newer than this code "
                f"understands ({SCHEMA_VERSION}); deploy a newer Inkframe"
            )
        for version in range(current, SCHEMA_VERSION):
            with self._transaction() as conn:
                for statement in _split_sql(MIGRATIONS[version]):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version + 1}")
            log.info("migrated %s to schema v%d", self._path.name, version + 1)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        """Close the connection.  Idempotent; the object is unusable afterwards."""
        with self._lock:
            self._conn.close()

    # --- photos -----------------------------------------------------------
    def add_photo(
        self,
        *,
        original_name: str,
        width: int,
        height: int,
        master_path: str,
        master_bytes: int,
        photo_id: str | None = None,
        portrait: bool | None = None,
        recipe: Mapping[str, Any] | None = None,
        excluded: bool = False,
        created_at: str | None = None,
    ) -> Photo:
        """Insert a photo and return it.  ``photo_id`` defaults to a new id (pass
        one when the master file is named after it); ``portrait`` defaults to
        ``height > width``."""
        photo = Photo(
            id=photo_id or new_id(),
            original_name=original_name,
            created_at=created_at or utc_now_iso(),
            width=int(width),
            height=int(height),
            portrait=bool(height > width) if portrait is None else bool(portrait),
            excluded=bool(excluded),
            recipe=None if recipe is None else dict(recipe),
            master_path=str(master_path),
            master_bytes=int(master_bytes),
        )
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO photos (id, original_name, created_at, width, height, portrait,"
                " excluded, recipe, master_path, master_bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    photo.id,
                    photo.original_name,
                    photo.created_at,
                    photo.width,
                    photo.height,
                    int(photo.portrait),
                    int(photo.excluded),
                    None if photo.recipe is None else _dumps(photo.recipe),
                    photo.master_path,
                    photo.master_bytes,
                ),
            )
        return photo

    def get_photo(self, photo_id: str) -> Photo | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return None if row is None else Photo.from_row(row)

    def list_photos(self, include_excluded: bool = True) -> list[Photo]:
        """All photos, oldest first."""
        sql = "SELECT * FROM photos"
        if not include_excluded:
            sql += " WHERE excluded = 0"
        sql += " ORDER BY created_at, rowid"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [Photo.from_row(r) for r in rows]

    def set_photo_recipe(self, photo_id: str, recipe: Mapping[str, Any] | None) -> bool:
        """Set (or with ``None`` clear) a photo's recipe override.  False if no such photo."""
        value = None if recipe is None else _dumps(dict(recipe))
        return self._update_photo("recipe = ?", (value, photo_id))

    def set_photo_excluded(self, photo_id: str, excluded: bool) -> bool:
        """Exclude or include a photo in rotation.  False if no such photo."""
        return self._update_photo("excluded = ?", (int(bool(excluded)), photo_id))

    def _update_photo(self, assignment: str, params: tuple[Any, ...]) -> bool:
        with self._transaction() as conn:
            cur = conn.execute(f"UPDATE photos SET {assignment} WHERE id = ?", params)
        return cur.rowcount > 0

    def delete_photo(self, photo_id: str) -> bool:
        """Delete the row (files are the caller's business).  False if no such photo."""
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        return cur.rowcount > 0

    # --- per-app settings -------------------------------------------------------
    def get_app_settings(self, app_id: str) -> dict[str, Any]:
        """An app's stored settings, ``{}`` if none were saved yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM app_settings WHERE app_id = ?", (app_id,)
            ).fetchone()
        return {} if row is None else json.loads(row["data"])

    def set_app_settings(self, app_id: str, data: Mapping[str, Any]) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO app_settings (app_id, data, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (app_id) DO UPDATE SET data = excluded.data,"
                " updated_at = excluded.updated_at",
                (app_id, _dumps(dict(data)), utc_now_iso()),
            )

    # --- key/value runtime state -------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        """The JSON value stored under ``key``, or ``default``."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_state(self, key: str, value: Any) -> None:
        """Store any JSON-serialisable ``value`` under ``key``."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, _dumps(value)),
            )


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements.  Migrations are plain DDL/DML with no
    semicolons inside literals or triggers, which keeps this trivial and lets
    each migration run inside a real transaction (``executescript`` would
    commit first)."""
    return [s.strip() for s in script.split(";") if s.strip()]
