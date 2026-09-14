"""Pluggable storage backend (see storage-backend.md).

`Memory` keeps the cross-cutting concerns (encryption, redaction, retention
orchestration) and delegates raw SQL execution to a `StorageBackend`. Phase 1
ships only the embedded `SqliteBackend` (stdlib, zero dependency); a PostgreSQL
backend can be added later behind the same interface without touching callers.

Backends return rows as plain ``dict`` so `Memory` accesses columns uniformly
(``row["col"]``) regardless of the underlying driver.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

# DDL is dialect-specific and therefore owned by each backend. This is the
# SQLite schema (unchanged from the original inline definition).
_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at INTEGER,
    org TEXT,
    context_json TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    seq INTEGER,
    role TEXT,
    content_json TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS tool_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    tool TEXT,
    ok INTEGER,
    summary TEXT,
    scan_records INTEGER,
    duration_ms INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    rating TEXT,
    note TEXT,
    answer_excerpt TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS result_store (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    tool TEXT,
    full_json TEXT,
    row_count INTEGER,
    scan_records INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS scratchpad (
    session_id TEXT PRIMARY KEY,
    data_json TEXT,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS long_term_memory (
    id TEXT PRIMARY KEY,
    org TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT,
    importance INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER,
    UNIQUE(org, key)
);
CREATE INDEX IF NOT EXISTS idx_ltm_org
    ON long_term_memory(org, importance DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);
"""


class StorageBackend:
    """Execution-only persistence interface. Implementations own the connection
    lifecycle, SQL dialect, transactions, and concurrency."""

    #: DB-API paramstyle marker; SQLite uses "?" (qmark), Postgres uses "%s".
    placeholder: str = "?"

    def init_schema(self) -> None:
        raise NotImplementedError

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement (commit) and return the affected row count."""
        raise NotImplementedError

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        raise NotImplementedError

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SqliteBackend(StorageBackend):
    """Embedded SQLite backend. Per-thread connections (a ThreadingHTTPServer
    serves requests on worker threads and a sqlite connection is thread-bound);
    the schema lives in the shared file so all threads see committed data."""

    placeholder = "?"

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")  # wait out brief write locks
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        self._conn.executescript(_SQLITE_DDL)
        self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cur.rowcount

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def build_storage(*, db_url: str = "", db_path: str | Path | None = None,
                  pool_max: int = 8) -> StorageBackend:
    """Factory: select a backend by config. Empty ``db_url`` → embedded SQLite
    (default, no dependency). A ``postgresql://`` URL will select the Postgres
    backend in a later phase."""
    if db_url:
        raise NotImplementedError(
            "PostgreSQL backend is not implemented yet (storage-backend.md "
            "Phase 2). Leave O2_DB_URL empty to use the default SQLite backend."
        )
    from pathlib import Path as _P
    return SqliteBackend(db_path or (_P.home() / ".o2agent" / "memory.db"))
