"""Rebuildable SQLite index for session metadata and UI preferences."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from harness.permissions import STARTUP_MODES


_SCHEMA_VERSION = 1
_CONTEXT_MODES = ("compaction", "folding")


@dataclass(frozen=True)
class NewSession:
    session_id: str
    workspace: Path
    title: str
    mode: str
    context_mode: str

    @classmethod
    def defaults(cls, session_id: str, workspace: Path) -> NewSession:
        return cls(
            session_id=session_id,
            workspace=workspace,
            title="New session",
            mode="default",
            context_mode="compaction",
        )


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    workspace: Path
    title: str
    mode: str
    context_mode: str
    created_at: str
    updated_at: str
    last_opened_at: str | None
    archived_at: str | None


@dataclass(frozen=True)
class PreferenceRecord:
    key: str
    value: object
    updated_at: str


class MetadataStore:
    """Persist rebuildable metadata through a single-thread-confined connection.

    One manager owns this store and calls every method from the thread that
    created it. The default sqlite same-thread protection deliberately remains
    enabled; cross-thread callers require an explicitly synchronized design.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._closed = False
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize_schema()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> MetadataStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned SQLite connection; repeated calls are harmless."""
        if not self._closed:
            self._connection.close()
            self._closed = True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            if version is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,)
                )
            elif version["version"] != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported metadata schema version: {version['version']}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    context_mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    archived_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    service_id TEXT NOT NULL UNIQUE
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO service_identity (singleton, service_id)
                VALUES (1, ?)
                """,
                (str(uuid4()),),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @property
    def service_id(self) -> str:
        """Return the non-secret identity of this metadata database."""
        row = self._connection.execute(
            "SELECT service_id FROM service_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("metadata service identity is missing")
        return str(row["service_id"])

    @staticmethod
    def _validated_session(session: NewSession) -> NewSession:
        if session.mode not in STARTUP_MODES:
            raise ValueError(f"mode must be one of {STARTUP_MODES}")
        if session.context_mode not in _CONTEXT_MODES:
            raise ValueError(f"context_mode must be one of {_CONTEXT_MODES}")
        return NewSession(
            session_id=session.session_id,
            workspace=session.workspace.resolve(),
            title=session.title,
            mode=session.mode,
            context_mode=session.context_mode,
        )

    @staticmethod
    def _session_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            workspace=Path(row["workspace"]),
            title=row["title"],
            mode=row["mode"],
            context_mode=row["context_mode"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_opened_at=row["last_opened_at"],
            archived_at=row["archived_at"],
        )

    def _get_session_row(self, session_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def _required_session(self, session_id: str) -> SessionRecord:
        row = self._get_session_row(session_id)
        if row is None:
            raise KeyError(session_id)
        return self._session_record(row)

    def create_session(self, session: NewSession) -> SessionRecord:
        session = self._validated_session(session)
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, title, mode, context_mode, created_at,
                    updated_at, last_opened_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session.session_id,
                    str(session.workspace),
                    session.title,
                    session.mode,
                    session.context_mode,
                    now,
                    now,
                    now,
                ),
            )
            return self._required_session(session.session_id)

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._get_session_row(session_id)
        return None if row is None else self._session_record(row)

    def list_sessions(self, *, include_archived: bool = False) -> list[SessionRecord]:
        query = "SELECT * FROM sessions"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY last_opened_at DESC, session_id"
        return [self._session_record(row) for row in self._connection.execute(query)]

    def rename_session(self, session_id: str, title: str) -> SessionRecord:
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, self._now(), session_id),
            )
            if result.rowcount != 1:
                raise KeyError(session_id)
            return self._required_session(session_id)

    def set_session_mode(self, session_id: str, mode: str) -> SessionRecord:
        if mode not in STARTUP_MODES:
            raise ValueError(f"mode must be one of {STARTUP_MODES}")
        with self._transaction() as connection:
            result = connection.execute(
                "UPDATE sessions SET mode = ?, updated_at = ? WHERE session_id = ?",
                (mode, self._now(), session_id),
            )
            if result.rowcount != 1:
                raise KeyError(session_id)
            return self._required_session(session_id)

    def touch_session(self, session_id: str) -> SessionRecord:
        now = self._now()
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE sessions SET updated_at = ?, last_opened_at = ?
                WHERE session_id = ?
                """,
                (now, now, session_id),
            )
            if result.rowcount != 1:
                raise KeyError(session_id)
            return self._required_session(session_id)

    def archive_session(self, session_id: str) -> SessionRecord:
        now = self._now()
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE sessions SET archived_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (now, now, session_id),
            )
            if result.rowcount != 1:
                raise KeyError(session_id)
            return self._required_session(session_id)

    def upsert_discovered_session(self, session: NewSession) -> SessionRecord:
        session = self._validated_session(session)
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, title, mode, context_mode, created_at,
                    updated_at, last_opened_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace = excluded.workspace,
                    mode = excluded.mode,
                    context_mode = excluded.context_mode
                """,
                (
                    session.session_id,
                    str(session.workspace),
                    session.title,
                    session.mode,
                    session.context_mode,
                    now,
                    now,
                    now,
                ),
            )
            return self._required_session(session.session_id)

    def get_preference(self, key: str) -> PreferenceRecord | None:
        row = self._connection.execute(
            "SELECT key, value_json, updated_at FROM preferences WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return PreferenceRecord(
            key=row["key"], value=json.loads(row["value_json"]), updated_at=row["updated_at"]
        )

    def set_preference(self, key: str, value: object) -> PreferenceRecord:
        value_json = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO preferences (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, value_json, now),
            )
            preference = self.get_preference(key)
            if preference is None:
                raise RuntimeError("preference write did not produce a row")
            return preference

    def raw_session_columns(self) -> tuple[str, ...]:
        return tuple(
            row["name"] for row in self._connection.execute("PRAGMA table_info(sessions)")
        )
