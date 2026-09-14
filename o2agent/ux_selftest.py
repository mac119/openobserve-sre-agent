"""Phase 10 UX self-test — pure, no network/LLM required.

Run: python -m o2agent.ux_selftest
Checks suggested prompts, context/progress rendering, TurnContext artifact
extraction, and feedback persistence against an isolated temp DB.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import ux
from .agent import Artifact, TurnContext, _stream_from_sql
from .memory import Memory


def _check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, name


def main() -> None:
    # suggested prompts
    s = ux.render_suggestions()
    _check("suggestions non-empty", "Suggested prompts:" in s)
    _check("suggestions have investigation", "Why was my last alert fired?" in s)

    # time range + footer
    rng = (1_700_000_000_000_000, 1_700_000_900_000_000)
    _check("time range formatted", "→" in (ux.render_time_range(rng) or ""))
    footer = ux.render_context_footer(
        org="default", streams=["bench_group_by"], time_range=rng,
        scan_records=10_000_001, scan_budget=5_000_000,
    )
    _check("footer shows org", "org=default" in footer)
    _check("footer shows stream", "stream=bench_group_by" in footer)
    _check("footer flags over budget", "OVER-BUDGET" in footer)

    # progress lines
    p1 = ux.render_progress("tool_start", tool="search", args={"sql": "SELECT 1"})
    p2 = ux.render_progress("tool_end", tool="search", ok=True, duration_ms=9,
                            scan_records=2)
    _check("progress start", p1 is not None and "search" in p1)
    _check("progress end ok", p2 is not None and "ok" in p2)

    # TurnContext artifact extraction
    _check("stream from sql", _stream_from_sql('SELECT * FROM "abc"') == "abc")
    turn = TurnContext(org="default")
    turn.note_stream("s1")
    turn.note_stream("s1")  # dedup
    turn.note_stream(None)
    _check("stream dedup", turn.streams == ["s1"])
    turn.artifacts.append(Artifact(kind="sql", text="SELECT 1", status="executed"))
    _check("artifact recorded", turn.artifacts[0].kind == "sql")

    # feedback persistence
    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "m.db")
        sid = mem.new_session("default")
        mem.record_feedback(sid, "up", "great", "the answer")
        row = mem._store.query_one(
            "SELECT rating, note FROM feedback WHERE session_id=?", (sid,)
        )
        _check("feedback stored", row["rating"] == "up" and row["note"] == "great")
        # purge cascades feedback
        mem._store.execute("UPDATE sessions SET created_at=0 WHERE id=?", (sid,))
        res = mem.purge(1)
        _check("purge removes feedback", res["feedback"] == 1)
        mem.close()

    print("\nALL UX SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
