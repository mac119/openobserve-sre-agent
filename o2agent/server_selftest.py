"""Phase 12 API server self-test — offline (no LLM turns).

Boots the server on an ephemeral port in a background thread and exercises the
non-LLM endpoints with httpx: health, suggestions, session create, message
history, SQL validation-reject, CORS preflight, and the token guard.

Run: python -m o2agent.server_selftest
"""
from __future__ import annotations

import threading

import httpx

from .config import Settings
from .server import build_server


def _check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, name


def _make_settings(token: str = "") -> Settings:
    base = Settings.load()
    # port 0 -> OS picks a free port; keep everything else from the real env
    return Settings(**{**base.__dict__, "api_host": "127.0.0.1", "api_port": 0,
                       "api_token": token, "cors_origin": "*"})


def _serve(token: str = ""):
    httpd, agent = build_server(_make_settings(token))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, agent, port


def main() -> None:
    # --- open (no token) ---------------------------------------------------
    httpd, agent, port = _serve(token="")
    base = f"http://127.0.0.1:{port}/api/o2"
    try:
        r = httpx.get(f"{base}/health", timeout=5)
        _check("health ok", r.status_code == 200 and r.json()["ok"] is True)
        _check("health cors header", r.headers.get("access-control-allow-origin") == "*")

        r = httpx.get(f"{base}/suggestions", timeout=5)
        _check("suggestions groups", "query" in r.json()["groups"])

        r = httpx.request("OPTIONS", f"{base}/chat", timeout=5)
        _check("preflight 204", r.status_code == 204)

        sid = httpx.post(f"{base}/sessions", timeout=5).json()["session_id"]
        _check("session created", bool(sid))

        r = httpx.get(f"{base}/sessions/{sid}/messages", timeout=5)
        _check("messages endpoint", "messages" in r.json())

        # SQL against a non-existent stream must be rejected by the validator gate
        r = httpx.post(f"{base}/sql", json={"sql": 'SELECT * FROM "no_such_stream_xyz"'},
                       timeout=15)
        _check("sql reject", r.status_code == 200 and r.json()["ok"] is False)

        r = httpx.post(f"{base}/chat", json={"session_id": sid}, timeout=5)
        _check("chat needs message (400)", r.status_code == 400)

        r = httpx.get(f"{base}/nope", timeout=5)
        _check("unknown path 404", r.status_code == 404)
    finally:
        httpd.shutdown()
        httpd.server_close()
        agent.close()

    # --- token-protected ---------------------------------------------------
    httpd, agent, port = _serve(token="secret")
    base = f"http://127.0.0.1:{port}/api/o2"
    try:
        _check("health open without token",
               httpx.get(f"{base}/health", timeout=5).status_code == 200)
        _check("suggestions 401 without token",
               httpx.get(f"{base}/suggestions", timeout=5).status_code == 401)
        r = httpx.get(f"{base}/suggestions",
                      headers={"Authorization": "Bearer secret"}, timeout=5)
        _check("suggestions ok with token", r.status_code == 200)
    finally:
        httpd.shutdown()
        httpd.server_close()
        agent.close()

    print("\nALL SERVER SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
