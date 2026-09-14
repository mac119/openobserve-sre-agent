"""Storage-backend self-test (offline, SQLite).

Runs the full Memory contract against the default SQLite backend in a temp file,
asserting the Phase-1 extraction preserved behavior. Also exercises the backend
directly (rowcount, dict rows) and concurrent access across threads.

Run: python -m o2agent.storage_selftest
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from .memory import Memory
from .storage import SqliteBackend, build_storage


def _check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, name


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        # factory returns SQLite by default; a db_url raises (PG not yet built)
        be = build_storage(db_path=Path(d) / "s.db")
        _check("factory -> sqlite backend", isinstance(be, SqliteBackend))
        raised = False
        try:
            build_storage(db_url="postgresql://x/y")
        except NotImplementedError:
            raised = True
        _check("postgres url not yet implemented", raised)

        # backend basics: rowcount + dict rows
        be.init_schema()
        n = be.execute(
            "INSERT INTO sessions(id, created_at, org, context_json) VALUES(?,?,?,?)",
            ("s1", 1, "default", "{}"))
        _check("execute returns rowcount", n == 1)
        row = be.query_one("SELECT org FROM sessions WHERE id=?", ("s1",))
        _check("query_one returns dict", isinstance(row, dict) and row["org"] == "default")
        _check("query_one miss -> None",
               be.query_one("SELECT org FROM sessions WHERE id=?", ("nope",)) is None)
        _check("query_all returns list of dict",
               be.query_all("SELECT id FROM sessions")[0]["id"] == "s1")
        be.close()

        # Memory contract round-trips over the backend
        mem = Memory(Path(d) / "m.db")
        sid = mem.new_session("default")
        mem.append_message(sid, {"role": "user", "content": "hi"})
        mem.append_message(sid, {"role": "assistant", "content": "hello"})
        msgs = mem.load_messages(sid)
        _check("messages round-trip", [m["content"] for m in msgs] == ["hi", "hello"])
        mem.save_context(sid, {"stream": "app_prod"})
        _check("context round-trip", mem.load_context(sid)["stream"] == "app_prod")
        mem.record_usage(sid, "m", 10, 5, 15, 0.001, 20)
        _check("usage aggregate", mem.session_usage(sid)["total_tokens"] == 15)
        ref = mem.store_result(sid, "search", '{"hits":[1,2]}', row_count=2)
        _check("result offload round-trip", mem.fetch_result(ref)["row_count"] == 2)
        mem.remember(sid, "fact", "e=mc2")
        _check("scratchpad round-trip", "e=mc2" in mem.get_scratchpad(sid)["facts"])
        mem.remember_long_term("default", "k", "v", importance=2)
        _check("ltm round-trip", mem.get_long_term("default")[0]["value"] == "v")
        mem.close()

    # concurrent access across threads (server scenario): shared Memory, one
    # backend, per-thread sqlite connections — no thread-affinity crash.
    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "c.db")
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                s = mem.new_session("default")
                mem.append_message(s, {"role": "user", "content": f"m{i}"})
                mem.load_messages(s)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _check("concurrent threads ok (no thread-affinity error)", errors == [])
        mem.close()

    print("\nALL STORAGE SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
