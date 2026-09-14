"""Phase 8 security self-test — pure, no network/LLM required.

Run: python -m o2agent.sec_selftest
Exercises the security helpers, scan-budget path, redaction/fencing, and the
memory retention purge against an isolated temp DB.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .memory import Memory
from .crypto import build_cipher, MemoryCipher
from .security import (
    SecurityError,
    assert_same_org,
    check_endpoint,
    fence_tool_result,
    normalize_permission_error,
    over_scan_budget,
    safe_for_model,
)


def _check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, name


def main() -> None:
    # endpoint allowlist
    _check("allowlist empty -> allow", check_endpoint("http://1.2.3.4:5080", ()))
    _check("allowlist host match", check_endpoint("http://1.2.3.4:5080", ("1.2.3.4",)))
    _check("allowlist host miss", not check_endpoint("http://evil:5080", ("1.2.3.4",)))

    # cross-org
    assert_same_org("/api/default/streams", "default")  # ok
    assert_same_org("/api/v2/default/alerts", "default")  # ok
    assert_same_org("/api/organizations", "default")  # global, ok
    assert_same_org("/config", "default")  # non-api, ok
    raised = False
    try:
        assert_same_org("/api/other_org/streams", "default")
    except SecurityError:
        raised = True
    _check("cross-org blocked", raised)

    # scan budget
    _check("over budget", over_scan_budget(10_000_001, 5_000_000))
    _check("under budget", not over_scan_budget(2, 5_000_000))
    _check("budget disabled", not over_scan_budget(10_000_001, 0))
    _check("none scan", not over_scan_budget(None, 5_000_000))

    # 403/401 normalization
    _check("403 normalized", "permission denied (403)" in normalize_permission_error("[403] forbidden"))
    _check("401 normalized", "authentication failed (401)" in normalize_permission_error("[401] nope"))

    # redaction (model-visible)
    red = safe_for_model({"h": "Authorization: Basic Zm9vOmJhcg==", "k": "sk-abcdef123456"})
    _check("basic redacted", "[REDACTED]" in red["h"] and "Zm9vOmJhcg" not in red["h"])
    _check("apikey redacted", "[REDACTED]" in red["k"])

    # fencing
    fenced = fence_tool_result("search", '{"ok": true}')
    _check("fence markers", "<<<O2_TOOL_DATA>>>" in fenced and "<<<END_O2_TOOL_DATA>>>" in fenced)
    _check("fence directive", "UNTRUSTED DATA" in fenced)

    # memory retention purge (isolated temp db)
    with tempfile.TemporaryDirectory() as d:
        mem = Memory(Path(d) / "m.db")
        sid = mem.new_session("default")
        mem.append_message(sid, {"role": "user", "content": "hi"})
        # backdate the session beyond retention
        mem._store.execute("UPDATE sessions SET created_at=0 WHERE id=?", (sid,))
        res = mem.purge(1)
        _check("purge disabled noop", mem.purge(0)["purged"] is False)
        _check("purge removed session", res["sessions"] == 1)
        _check("purge removed messages", res["messages"] == 1)
        _check("session gone", mem.load_messages(sid) == [])
        mem.close()

    # at-rest encryption: cipher round-trip + plaintext passthrough
    _check("no key -> no cipher", build_cipher("") is None)
    cipher = MemoryCipher("test-passphrase")
    tok = cipher.encrypt('{"a": 1}')
    _check("ciphertext prefixed", tok.startswith("enc:v1:"))
    _check("ciphertext hides plaintext", '{"a": 1}' not in tok)
    _check("cipher round-trip", cipher.decrypt(tok) == '{"a": 1}')
    _check("legacy plaintext passthrough", cipher.decrypt('{"a": 1}') == '{"a": 1}')
    wrong = MemoryCipher("other-passphrase")
    bad = False
    try:
        wrong.decrypt(tok)
    except ValueError:
        bad = True
    _check("wrong key fails closed", bad)

    # encrypted memory: data on disk is ciphertext, reads decrypt transparently
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "enc.db"
        mem = Memory(p, cipher=MemoryCipher("k"))
        sid = mem.new_session("default")
        secret = "Authorization Basic super-secret-value"
        mem.append_message(sid, {"role": "user", "content": secret})
        _check("encrypted read round-trips", mem.load_messages(sid)[0]["content"] == secret)
        mem.close()
        raw = p.read_bytes()
        _check("secret not on disk in plaintext", secret.encode() not in raw)
        _check("enc marker on disk", b"enc:v1:" in raw)
        # reopening with the same key still reads
        mem2 = Memory(p, cipher=MemoryCipher("k"))
        _check("reopen decrypts", mem2.load_messages(sid)[0]["content"] == secret)
        mem2.close()

    # per-user credential passthrough (P3): a supplied auth overrides the
    # shared service-account header on the read client
    from types import SimpleNamespace
    from .client import ReadOnlyClient
    st = SimpleNamespace(base_url="http://127.0.0.1:5080", endpoint_allowlist=(),
                         auth="Basic service-account", http_timeout=5, org="default")
    rc = ReadOnlyClient(st, auth="Basic user-token")
    _check("read client honors auth override",
           rc._http.headers.get("Authorization") == "Basic user-token")
    rc.close()
    rc2 = ReadOnlyClient(st)
    _check("read client defaults to service account",
           rc2._http.headers.get("Authorization") == "Basic service-account")
    rc2.close()

    print("\nALL SECURITY SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
