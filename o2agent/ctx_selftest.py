"""Context-management self-test (offline, no network/LLM).

Run: python -m o2agent.ctx_selftest
Covers P0-1 (result trim/offload + fetch_result) so far; later phases append here.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .agent import (
    _extract_rows, _trim_or_offload, over_turn_budget, repeat_blocked, pick_model,
)
from .compaction import Compactor
from .memory import Memory


def _check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, name


class _R:
    """Minimal ToolResult stand-in."""
    def __init__(self, data, ok=True, scan_records=None, meta=None):
        self.ok = ok
        self.data = data
        self.scan_records = scan_records
        self.meta = meta or {}
        self.error = None


class _FakeLLMResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Returns a canned structured summary; or raises to test the fallback."""
    def __init__(self, content=None, raise_it=False):
        self.content = content
        self.raise_it = raise_it

    def chat(self, messages, tools=None, *, temperature=0.0, max_tokens=2048):
        if self.raise_it:
            raise RuntimeError("llm down")
        return _FakeLLMResp(self.content)


class _Cfg:
    tokens_per_char = 4
    context_token_budget = 100      # tiny budget so tests trip compaction
    compact_ratio = 0.5
    keep_recent_turns = 1


def _mk_messages(n_old_turns: int) -> list:
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(n_old_turns):
        msgs.append({"role": "user", "content": f"old question {i} " + "x" * 80})
        msgs.append({"role": "assistant", "content": f"old answer {i} " + "y" * 80})
        msgs.append({"role": "tool", "tool_call_id": "t", "content": "Z" * 200})
    msgs.append({"role": "user", "content": "the LATEST question " + "w" * 80})
    msgs.append({"role": "assistant", "content": "latest answer"})
    return msgs


def _compaction_cases() -> None:
    cfg = _Cfg()
    # below budget -> no compaction
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    c = Compactor(_FakeLLM(), cfg)
    _check("small history not compacted", c.compact(small)[1] is None)

    # large history -> compacted; LLM summary path
    msgs = _mk_messages(6)
    summary_json = '{"goal":"g","verified_facts":["f"],"assumptions":[],' \
                   '"open_tasks":["t"],"key_ids":{"stream":"s"}}'
    c = Compactor(_FakeLLM(content=summary_json), cfg)
    new, summ = c.compact(msgs)
    _check("large history compacted", summ is not None)
    _check("system prompt kept first", new[0]["content"] == "SYS")
    _check("summary marker present", "COMPACTED HISTORY" in new[1]["content"])
    _check("latest turn kept verbatim", any("LATEST question" in str(m.get("content"))
                                            for m in new))
    _check("old raw tool output dropped", not any(m.get("content") == "Z" * 200
                                                  for m in new))
    _check("compacted shorter than original", len(new) < len(msgs))

    # LLM failure -> deterministic fallback keeps text, still compacts
    c = Compactor(_FakeLLM(raise_it=True), cfg)
    new2, summ2 = c.compact(_mk_messages(6))
    _check("fallback still produces summary", summ2 is not None)
    _check("fallback marked", summ2.get("note", "").startswith("deterministic"))
    _check("fallback counts dropped outputs", summ2["dropped_outputs"] == 6)


def _scratchpad_cases() -> None:
    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "s.db")
        sid = mem.new_session("default")
        mem.remember(sid, "fact", "error rate is 5%")
        mem.remember(sid, "fact", "error rate is 5%")  # dup ignored
        mem.remember(sid, "task", "check the alert config")
        mem.remember(sid, "key_id", "bench_group_by", key="stream")
        pad = mem.get_scratchpad(sid)
        _check("scratchpad fact stored once", pad["facts"] == ["error rate is 5%"])
        _check("scratchpad task stored", pad["tasks"] == ["check the alert config"])
        _check("scratchpad key_id stored", pad["key_ids"]["stream"] == "bench_group_by")

        # compaction merges scratchpad seed even if the LLM summary omits it
        cfg = _Cfg()
        summary_json = '{"goal":"g","verified_facts":[],"assumptions":[],' \
                       '"open_tasks":[],"key_ids":{}}'
        c = Compactor(_FakeLLM(content=summary_json), cfg)
        _new, summ = c.compact(_mk_messages(6), seed=pad)
        _check("seed fact merged", "error rate is 5%" in summ["verified_facts"])
        _check("seed key_id merged", summ["key_ids"].get("stream") == "bench_group_by")

        mem._store.execute("UPDATE sessions SET created_at=0 WHERE id=?", (sid,))
        res = mem.purge(1)
        _check("purge removes scratchpad", res["scratchpad"] == 1)
        mem.close()


def _loop_control_cases() -> None:
    # repeat detection
    _check("repeat allowed under limit", not repeat_blocked(2, 2))
    _check("repeat blocked over limit", repeat_blocked(3, 2))
    _check("repeat disabled when max=0", not repeat_blocked(99, 0))
    # budget gate
    _check("time budget tripped",
           over_turn_budget(wall_s=10, elapsed_s=11, cost_budget=0, spent_usd=0) == "time")
    _check("cost budget tripped",
           over_turn_budget(wall_s=0, elapsed_s=0, cost_budget=0.5, spent_usd=0.6) == "cost")
    _check("under budget ok",
           over_turn_budget(wall_s=10, elapsed_s=1, cost_budget=1.0, spent_usd=0.1) is None)
    _check("zero budgets disabled",
           over_turn_budget(wall_s=0, elapsed_s=999, cost_budget=0, spent_usd=999) is None)


def _cost_latency_cases() -> None:
    # model routing
    _check("large intent -> large model",
           pick_model("investigation", default="d", small="s", large="L") == "L")
    _check("simple intent -> small model",
           pick_model("general_help", default="d", small="s", large="L") == "s")
    _check("routing falls back to default",
           pick_model("investigation", default="d", small="", large="") == "d")

    # parallel execution: use a tiny stub Agent-like object exercising _execute_planned
    import time as _t
    from types import SimpleNamespace
    from .agent import Agent

    class _StubReg:
        def call(self, name, args):
            _t.sleep(0.1)
            return _R({"ok": True, "name": name})

    class _StubTel:
        def emit(self, *a, **k):
            pass

    stub = SimpleNamespace(
        registry=_StubReg(), tel=_StubTel(),
        s=SimpleNamespace(parallel_tools=True, max_parallel=4, max_repeat_calls=2),
    )
    planned = [{"tc": {"id": str(i)}, "name": "search", "args": {"q": i}, "blocked": False}
               for i in range(4)]
    t0 = _t.monotonic()
    results = Agent._execute_planned(stub, planned, "sid")
    elapsed = _t.monotonic() - t0
    _check("all parallel results returned", len(results) == 4)
    _check("parallel faster than serial", elapsed < 0.35)  # ~0.1s vs 0.4s serial

    # blocked call resolves without running
    planned2 = [{"tc": {"id": "x"}, "name": "search", "args": {}, "blocked": True}]
    res2 = Agent._execute_planned(stub, planned2, "sid")
    _check("blocked call not executed", not res2[0].ok and "blocked" in res2[0].error)


def _long_term_memory_cases() -> None:
    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "ltm.db")
        # upsert: same (org,key) overwrites value, preserves created_at
        r1 = mem.remember_long_term("default", "prod_stream", "was app_v1", kind="env")
        got = mem.get_long_term("default")
        created = [x for x in got if x["key"] == "prod_stream"][0]["created_at"]
        import time as _t; _t.sleep(1)
        mem.remember_long_term("default", "prod_stream", "now app_v2", kind="env")
        got = mem.get_long_term("default")
        row = [x for x in got if x["key"] == "prod_stream"][0]
        _check("ltm upsert overwrites value", row["value"] == "now app_v2")
        _check("ltm upsert preserves created_at", row["created_at"] == created)
        _check("ltm no duplicate key", len([x for x in got if x["key"] == "prod_stream"]) == 1)

        # org isolation
        mem.remember_long_term("orgA", "k", "secretA")
        _check("ltm org isolation", mem.get_long_term("orgB") == [])

        # capacity cap: evict lowest importance/oldest
        for i in range(5):
            mem.remember_long_term("cap", f"k{i}", f"v{i}", importance=1, max_per_org=3)
        _check("ltm capacity cap", len(mem.get_long_term("cap")) == 3)

        # expiry: expired row excluded + swept
        mem.remember_long_term("exp", "temp", "gone soon", ttl_days=1)
        mem._store.execute("UPDATE long_term_memory SET expires_at=1 WHERE org='exp'")
        _check("ltm expired excluded from read", mem.get_long_term("exp") == [])
        swept = mem.purge_long_term(expired_only=True)
        _check("ltm expired swept by purge", swept >= 1)

        # redaction on write
        mem.remember_long_term("red", "cred", "token Authorization: Basic Zm9vOmJhcg==")
        stored = mem.get_long_term("red")[0]["value"]
        _check("ltm redacts credentials", "Zm9vOmJhcg==" not in stored and "[REDACTED]" in stored)

        # forget
        _check("ltm forget", mem.forget_long_term("default", "prod_stream") is True)

        # injection format carries the unverified/assumption label (grounding guard)
        from types import SimpleNamespace
        from .agent import Agent
        mem.remember_long_term("inj", "answer_style", "concise Chinese", kind="preference")
        stub = SimpleNamespace(
            s=SimpleNamespace(ltm_enabled=True, org="inj", ltm_inject_top=20),
            mem=mem)
        block = Agent._ltm_block(stub)
        _check("ltm block present", block is not None and block["role"] == "system")
        _check("ltm block labeled unverified",
               "unverified" in block["content"] and "ASSUMPTIONS" in block["content"])
        _check("ltm block content", "answer_style" in block["content"])
        # disabled -> no block
        stub.s.ltm_enabled = False
        _check("ltm block disabled", Agent._ltm_block(stub) is None)
        mem.close()


def main() -> None:
    # row extraction
    rows, n = _extract_rows({"hits": [{"a": 1}, {"a": 2}], "total": 2})
    _check("extract hits", rows is not None and n == 2)
    rows2, _ = _extract_rows({"schema": [{"name": "x"}]})
    _check("non-hits dict not treated as rows", rows2 is None)

    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "m.db")
        sid = mem.new_session("default")

        # small result: passes through, NOT offloaded
        small = _R({"hits": [{"a": 1}]})
        msg = _trim_or_offload(mem, sid, "search", small,
                               sample_rows=5, inline_max_bytes=2048)
        _check("small result inlined", "result_ref" not in msg)

        # large result (many rows): offloaded + summarized
        big_rows = [{"i": i, "msg": "x" * 50} for i in range(200)]
        big = _R({"hits": big_rows, "total": 200}, scan_records=999)
        msg = _trim_or_offload(mem, sid, "search", big,
                               sample_rows=5, inline_max_bytes=2048)
        _check("large result offloaded", "result_ref" in msg and "truncated" in msg)
        # the raw 200 rows must NOT be in the model-visible message
        _check("full payload not inlined", msg.count('"i":') <= 5)
        # extract the ref and fetch it back
        ref = msg.split('"result_ref":')[1].split('"')[1]
        rec = mem.fetch_result(ref)
        _check("offloaded record retrievable", rec is not None)
        restored = json.loads(rec["full_json"])
        _check("round-trip rows intact", len(restored["hits"]) == 200)

        # failing result passes through untouched (no offload)
        fail = _R(None, ok=False)
        fail.error = "boom"
        msg = _trim_or_offload(mem, sid, "search", fail,
                               sample_rows=5, inline_max_bytes=2048)
        _check("error passes through", "result_ref" not in msg and "boom" in msg)

        # purge cascades result_store
        mem._store.execute("UPDATE sessions SET created_at=0 WHERE id=?", (sid,))
        res = mem.purge(1)
        _check("purge removes result_store", res["result_store"] >= 1)
        mem.close()

    _compaction_cases()
    _scratchpad_cases()
    _loop_control_cases()
    _cost_latency_cases()
    _long_term_memory_cases()

    print("\nALL CONTEXT SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
