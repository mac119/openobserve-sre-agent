"""Conversation memory & persistence (architecture §7).

Stores per-session message history, tool-result history, resolved context, and
long-term (per-org) memory so conversations survive across process restarts.

`Memory` owns the cross-cutting concerns — transparent field encryption,
redaction on write, and retention orchestration — and delegates raw SQL to a
pluggable `StorageBackend` (see storage-backend.md). The default backend is
embedded SQLite (stdlib, zero dependency).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .storage import StorageBackend, build_storage
from .telemetry import redact as _redact

_DEFAULT_DB = Path.home() / ".o2agent" / "memory.db"


class Memory:
    def __init__(self, db_path: str | Path | None = None, cipher=None,
                 backend: StorageBackend | None = None):
        self._cipher = cipher  # optional MemoryCipher; None = plaintext
        # Persistence is delegated to a StorageBackend. Callers may pass one
        # explicitly (e.g. Postgres later); otherwise a SQLite backend is built
        # from db_path, preserving the original constructor behavior.
        self._store = backend or build_storage(
            db_path=str(db_path) if db_path else str(_DEFAULT_DB))
        self.path = getattr(self._store, "path", None)
        self._store.init_schema()

    # -- transparent field encryption -------------------------------------

    def _enc(self, plaintext: str) -> str:
        return self._cipher.encrypt(plaintext) if self._cipher else plaintext

    def _dec(self, value: str) -> str:
        return self._cipher.decrypt(value) if self._cipher else value

    # -- sessions ----------------------------------------------------------

    def new_session(self, org: str) -> str:
        sid = uuid.uuid4().hex[:12]
        self._store.execute(
            "INSERT INTO sessions(id, created_at, org, context_json) VALUES(?,?,?,?)",
            (sid, int(time.time()), org, self._enc("{}")),
        )
        return sid

    def save_context(self, session_id: str, context: dict) -> None:
        self._store.execute(
            "UPDATE sessions SET context_json=? WHERE id=?",
            (self._enc(json.dumps(context)), session_id),
        )

    def load_context(self, session_id: str) -> dict:
        row = self._store.query_one(
            "SELECT context_json FROM sessions WHERE id=?", (session_id,)
        )
        return json.loads(self._dec(row["context_json"])) if row else {}

    # -- messages ----------------------------------------------------------

    def append_message(self, session_id: str, message: dict) -> None:
        seq_row = self._store.query_one(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE session_id=?",
            (session_id,),
        )
        seq = ((seq_row["m"] if seq_row else 0) or 0) + 1
        self._store.execute(
            "INSERT INTO messages(session_id, seq, role, content_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, seq, message.get("role"),
             self._enc(json.dumps(message)), int(time.time())),
        )

    def load_messages(self, session_id: str) -> list[dict]:
        rows = self._store.query_all(
            "SELECT content_json FROM messages WHERE session_id=? ORDER BY seq",
            (session_id,),
        )
        return [json.loads(self._dec(r["content_json"])) for r in rows]

    # -- tool results ------------------------------------------------------

    def record_tool_result(
        self,
        session_id: str,
        tool: str,
        ok: bool,
        summary: str,
        *,
        scan_records: int | None = None,
        duration_ms: int = 0,
    ) -> None:
        self._store.execute(
            "INSERT INTO tool_results(session_id, tool, ok, summary, scan_records, "
            "duration_ms, created_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, tool, 1 if ok else 0, self._enc(summary), scan_records,
             duration_ms, int(time.time())),
        )

    # -- llm usage ---------------------------------------------------------

    def record_usage(
        self,
        session_id: str,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        self._store.execute(
            "INSERT INTO llm_usage(session_id, model, prompt_tokens, completion_tokens, "
            "total_tokens, cost_usd, duration_ms, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, model, prompt_tokens, completion_tokens, total_tokens,
             cost_usd, duration_ms, int(time.time())),
        )

    def session_usage(self, session_id: str) -> dict:
        row = self._store.query_one(
            "SELECT COALESCE(SUM(total_tokens),0) AS tok, COALESCE(SUM(cost_usd),0) AS cost, "
            "COUNT(*) AS calls FROM llm_usage WHERE session_id=?",
            (session_id,),
        ) or {"tok": 0, "cost": 0, "calls": 0}
        return {"total_tokens": row["tok"], "cost_usd": round(row["cost"], 6),
                "llm_calls": row["calls"]}

    # -- feedback ----------------------------------------------------------

    def record_feedback(self, session_id: str, rating: str, note: str = "",
                        answer_excerpt: str = "") -> None:
        """Store a thumbs-up/down or correction for the last answer (UX §10)."""
        self._store.execute(
            "INSERT INTO feedback(session_id, rating, note, answer_excerpt, "
            "created_at) VALUES(?,?,?,?,?)",
            (session_id, rating, note, answer_excerpt[:500], int(time.time())),
        )

    # -- result offload (context management P0-1) --------------------------

    def store_result(self, session_id: str, tool: str, full_json: str,
                     row_count: int | None = None,
                     scan_records: int | None = None) -> str:
        """Offload a full tool result; return a short reference id.

        `full_json` is expected to be already redacted by the caller. Encrypted
        at rest when a cipher is configured."""
        rid = "res_" + uuid.uuid4().hex[:12]
        self._store.execute(
            "INSERT INTO result_store(id, session_id, tool, full_json, row_count, "
            "scan_records, created_at) VALUES(?,?,?,?,?,?,?)",
            (rid, session_id, tool, self._enc(full_json), row_count, scan_records,
             int(time.time())),
        )
        return rid

    def fetch_result(self, result_ref: str) -> dict | None:
        """Return {tool, full_json (decrypted str), row_count, scan_records} or None."""
        row = self._store.query_one(
            "SELECT tool, full_json, row_count, scan_records FROM result_store "
            "WHERE id=?", (result_ref,),
        )
        if not row:
            return None
        return {
            "tool": row["tool"],
            "full_json": self._dec(row["full_json"]),
            "row_count": row["row_count"],
            "scan_records": row["scan_records"],
        }

    # -- scratchpad (mid-term memory, context management P0-3) -------------

    def get_scratchpad(self, session_id: str) -> dict:
        row = self._store.query_one(
            "SELECT data_json FROM scratchpad WHERE session_id=?", (session_id,)
        )
        if not row:
            return {"facts": [], "tasks": [], "key_ids": {}}
        try:
            return json.loads(self._dec(row["data_json"]))
        except (ValueError, TypeError):
            return {"facts": [], "tasks": [], "key_ids": {}}

    def remember(self, session_id: str, kind: str, note: str, key: str = "") -> dict:
        """Append a fact/task/key_id to the session scratchpad. Idempotent-ish:
        duplicate facts/tasks are not re-added."""
        pad = self.get_scratchpad(session_id)
        kind = (kind or "fact").lower()
        if kind == "task":
            if note and note not in pad["tasks"]:
                pad["tasks"].append(note)
        elif kind == "key_id":
            if key:
                pad["key_ids"][key] = note
        else:  # fact
            if note and note not in pad["facts"]:
                pad["facts"].append(note)
        self._store.execute(
            "INSERT INTO scratchpad(session_id, data_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET data_json=excluded.data_json, "
            "updated_at=excluded.updated_at",
            (session_id, self._enc(json.dumps(pad)), int(time.time())),
        )
        return pad

    # -- long-term memory (cross-session, per-org; see memory-longterm.md) --

    def _slugify(self, note: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", (note or "").lower()).strip("_")
        return (base[:40] or "note")

    def remember_long_term(self, org: str, key: str, value: str, *,
                           kind: str = "fact", source: str = "assistant",
                           importance: int = 1, ttl_days: int = 0,
                           max_per_org: int = 100) -> dict:
        """Upsert a durable, org-scoped memory. Redacts credentials, enforces a
        per-org capacity cap (evicting the lowest importance/oldest row)."""
        key = key or self._slugify(value)
        value = _redact(value)
        now = int(time.time())
        expires_at = now + ttl_days * 86400 if ttl_days and ttl_days > 0 else None
        existing = self._store.query_one(
            "SELECT id, created_at FROM long_term_memory WHERE org=? AND key=?",
            (org, key),
        )
        if existing:
            self._store.execute(
                "UPDATE long_term_memory SET value=?, kind=?, source=?, "
                "importance=?, updated_at=?, expires_at=? WHERE id=?",
                (self._enc(value), kind, source, importance, now, expires_at,
                 existing["id"]),
            )
            mid = existing["id"]
        else:
            mid = uuid.uuid4().hex[:12]
            self._store.execute(
                "INSERT INTO long_term_memory(id, org, key, value, kind, source, "
                "importance, created_at, updated_at, expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, org, key, self._enc(value), kind, source, importance,
                 now, now, expires_at),
            )
            self._evict_over_cap(org, max_per_org)
        return {"id": mid, "org": org, "key": key, "kind": kind,
                "importance": importance, "expires_at": expires_at}

    def _evict_over_cap(self, org: str, max_per_org: int) -> None:
        if not max_per_org or max_per_org <= 0:
            return
        row = self._store.query_one(
            "SELECT COUNT(*) AS c FROM long_term_memory WHERE org=?", (org,)
        )
        n = row["c"] if row else 0
        if n <= max_per_org:
            return
        # evict lowest (importance, updated_at) rows down to the cap
        for r in self._store.query_all(
            "SELECT id FROM long_term_memory WHERE org=? "
            "ORDER BY importance ASC, updated_at ASC LIMIT ?",
            (org, n - max_per_org),
        ):
            self._store.execute("DELETE FROM long_term_memory WHERE id=?", (r["id"],))

    def get_long_term(self, org: str, limit: int | None = None) -> list[dict]:
        """Return non-expired memories for an org, ordered by importance then
        recency. Values are decrypted for use."""
        now = int(time.time())
        sql = ("SELECT key, value, kind, source, importance, created_at, updated_at "
               "FROM long_term_memory WHERE org=? AND (expires_at IS NULL OR expires_at>?) "
               "ORDER BY importance DESC, updated_at DESC")
        params: tuple = (org, now)
        if limit is not None:
            sql += " LIMIT ?"
            params = (org, now, limit)
        rows = self._store.query_all(sql, params)
        return [
            {"key": r["key"], "value": self._dec(r["value"]), "kind": r["kind"],
             "source": r["source"], "importance": r["importance"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows
        ]

    def forget_long_term(self, org: str, key: str) -> bool:
        n = self._store.execute(
            "DELETE FROM long_term_memory WHERE org=? AND key=?", (org, key))
        return n > 0

    def purge_long_term(self, org: str | None = None, expired_only: bool = True) -> int:
        """Sweep expired rows (default) or wipe an org's memories entirely."""
        now = int(time.time())
        if expired_only:
            if org:
                return self._store.execute(
                    "DELETE FROM long_term_memory WHERE org=? AND expires_at IS NOT NULL "
                    "AND expires_at<=?", (org, now))
            return self._store.execute(
                "DELETE FROM long_term_memory WHERE expires_at IS NOT NULL "
                "AND expires_at<=?", (now,))
        if not org:
            raise ValueError("full wipe requires an explicit org")
        return self._store.execute("DELETE FROM long_term_memory WHERE org=?", (org,))

    def close(self) -> None:
        self._store.close()

    # -- data retention ----------------------------------------------------

    def purge(self, retention_days: int) -> dict:
        """Delete sessions (and their messages/tool_results/llm_usage/…) older
        than ``retention_days``. A non-positive value is a no-op (keep all).

        Implements the Phase 8 data-retention policy: bounded storage of
        conversation + credential-adjacent data. Returns per-table delete counts.
        """
        if not retention_days or retention_days <= 0:
            return {"purged": False, "reason": "retention disabled"}
        cutoff = int(time.time()) - retention_days * 86400
        old = [
            r["id"] for r in self._store.query_all(
                "SELECT id FROM sessions WHERE created_at < ?", (cutoff,)
            )
        ]
        counts = {"sessions": 0, "messages": 0, "tool_results": 0, "llm_usage": 0,
                  "feedback": 0, "result_store": 0, "scratchpad": 0}
        if not old:
            return {"purged": True, "cutoff": cutoff, **counts}
        ph = self._store.placeholder
        marks = ",".join(ph for _ in old)
        for table in ("messages", "tool_results", "llm_usage", "feedback",
                      "result_store", "scratchpad"):
            counts[table] = self._store.execute(
                f"DELETE FROM {table} WHERE session_id IN ({marks})", old
            )
        counts["sessions"] = self._store.execute(
            f"DELETE FROM sessions WHERE id IN ({marks})", old
        )
        return {"purged": True, "cutoff": cutoff, **counts}
