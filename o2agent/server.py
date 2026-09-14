"""HTTP + SSE API server (Phase 12).

A small, framework-light API over the shared `Agent` so an external front-end
(e.g. the O2 Assistant panel in the OpenObserve UI) can drive it. The CLI stays
a peer entry point; both share the same Agent/memory/gate.

See `api-server.md` for the endpoint contract. Stdlib only (http.server) — no new
dependency. Read-only endpoints run unlocked; chat turns are serialized by a lock
because a turn writes `agent.last_turn`.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import ux
from .agent import Agent
from .config import Settings
from .gate import GateError


# --- serialization helpers ------------------------------------------------

def _turn_to_dict(turn) -> dict:
    if turn is None:
        return {}
    return {
        "org": turn.org,
        "streams": turn.streams,
        "time_range": list(turn.time_range) if turn.time_range else None,
        "scan_records": turn.scan_records,
        "tools": turn.tools_called,
        "artifacts": [
            {"kind": a.kind, "text": a.text, "status": a.status}
            for a in turn.artifacts
        ],
    }


def _change_to_dict(c) -> dict:
    return {
        "id": c.id,
        "tier": c.tier.value,
        "state": c.state.value,
        "action": c.action,
        "resource_kind": c.resource_kind,
        "diff": c.diff,
    }


# --- OpenObserve chat_stream frame helpers --------------------------------

# Human-readable labels for the tool_call frame (shown in the UI's progress box).
_TOOL_LABELS = {
    "list_streams": "Listing streams",
    "get_schema": "Fetching stream schema",
    "search": "Running query",
    "list_alerts": "Listing alerts",
    "alert_history": "Reading alert history",
    "investigate_alert": "Investigating alert",
    "list_dashboards": "Listing dashboards",
    "list_functions": "Listing functions",
    "list_pipelines": "Listing pipelines",
    "summary": "Summarizing org",
    "validate_query": "Validating query",
    "propose_change": "Preparing a change proposal",
}


def _tool_label(tool: str, args: dict) -> str:
    base = _TOOL_LABELS.get(tool, f"Running {tool}")
    if tool == "search" and isinstance(args, dict) and args.get("sql"):
        sql = str(args["sql"])
        return f"{base}: {sql[:60]}" + ("…" if len(sql) > 60 else "")
    if tool == "get_schema" and isinstance(args, dict) and args.get("stream_name"):
        return f"{base} ({args['stream_name']})"
    return base


def _artifact_lang(kind: str) -> str:
    return {"vrl": "coffee", "promql": "promql", "regex": "regex",
            "sql": "sql"}.get((kind or "").lower(), "sql")


def _chunk_text(text: str, size: int = 24):
    """Yield the answer in small chunks so the UI types it out progressively.

    Splits on whitespace boundaries near `size` chars to avoid breaking words,
    falling back to a hard slice for very long tokens.
    """
    if not text:
        return
    i, n = 0, len(text)
    while i < n:
        end = min(i + size, n)
        # extend to the next whitespace so we don't cut mid-word
        if end < n:
            nxt = text.find(" ", end)
            if nxt != -1 and nxt - end < size:
                end = nxt + 1
        yield text[i:end]
        i = end


class O2Service:
    """Wraps a shared Agent with a chat lock. Owns request handling logic
    (transport-agnostic) so it can be unit-tested without a socket."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._chat_lock = threading.Lock()

    # -- read-only (no lock) ----------------------------------------------

    def health(self) -> dict:
        return {"ok": True, "org": self.agent.s.org, "service": "o2-assistant"}

    def suggestions(self) -> dict:
        return {"groups": ux.SUGGESTED_PROMPTS}

    def new_session(self) -> dict:
        return {"session_id": self.agent.start_session()}

    def messages(self, sid: str) -> dict:
        msgs = self.agent.mem.load_messages(sid)
        out = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        return {"messages": out}

    def changes(self) -> dict:
        return {"changes": [_change_to_dict(c) for c in self.agent.gate._changes.values()]}

    # -- mutating gate ops ------------------------------------------------

    def approve(self, cid: str, phrase: str | None) -> dict:
        c = self.agent.gate.approve(cid, elevated_confirmation=phrase)
        return {"change": _change_to_dict(c)}

    def reject(self, cid: str) -> dict:
        return {"change": _change_to_dict(self.agent.gate.reject(cid))}

    def run(self, cid: str) -> dict:
        c = self.agent.gate.execute(cid)
        r = c.result
        return {"change": _change_to_dict(c),
                "result": {"ok": bool(r and r.ok),
                           "dry_run": bool(r and r.dry_run),
                           "error": r.error if r else None}}

    def feedback(self, sid: str, rating: str, note: str) -> dict:
        excerpt = ""
        for m in reversed(self.agent.mem.load_messages(sid)):
            if m.get("role") == "assistant" and m.get("content"):
                excerpt = m["content"]
                break
        self.agent.mem.record_feedback(sid, rating, note, excerpt)
        return {"ok": True}

    def run_sql(self, sid: str | None, sql: str, size: int) -> dict:
        start, end = self.agent.resolver.default_range_us("interactive")
        result = self.agent.registry.call(
            "search", {"sql": sql, "start_time": start, "end_time": end, "size": size})
        if not result.ok:
            return {"ok": False, "error": result.error,
                    "time_range": [start, end]}
        hits = result.data.get("hits", []) if isinstance(result.data, dict) else result.data
        return {"ok": True, "rows": hits, "scan_records": result.scan_records,
                "time_range": [start, end],
                "warnings": result.meta.get("warnings", [])}

    # -- chat (locked; writes last_turn) ----------------------------------

    def chat(self, sid: str, message: str) -> dict:
        with self._chat_lock:
            answer = self.agent.chat(sid, message)
            ctx = _turn_to_dict(self.agent.last_turn)
            usage = self.agent.mem.session_usage(sid)
        return {"answer": answer, "context": ctx, "usage": usage}

    def chat_stream(self, sid: str, message: str, emit) -> None:
        """Run a turn, pushing SSE frames via `emit(event, data_dict)`."""
        with self._chat_lock:
            def on_event(event: str, **info):
                if event in ("tool_start", "tool_end"):
                    emit("progress", {"stage": event, **{
                        k: v for k, v in info.items() if k != "args"}})
            try:
                answer = self.agent.chat(sid, message, on_event=on_event)
            except Exception as e:  # surface a clean error frame
                emit("error", {"error": f"{type(e).__name__}: {e}"})
                return
            emit("final", {
                "answer": answer,
                "context": _turn_to_dict(self.agent.last_turn),
                "usage": self.agent.mem.session_usage(sid),
            })

    # -- OpenObserve-native compatibility (drives the built-in O2 Assistant) --
    #
    # The stock OpenObserve UI posts to `/api/{org}/ai/chat_stream` and reads a
    # newline-delimited `data: {json}` stream whose frames are dispatched by a
    # `type` field (message_delta / tool_call / complete / error / title). The
    # community build has no backend for this, so the panel is an inert shell.
    # This method makes OUR agent answer that endpoint, translating the agent's
    # progress + final answer into the exact frames the UI expects — so the
    # native chat box is driven by us with ZERO front-end changes.

    def o2_chat_stream(self, messages: list, session_hint: str | None, emit) -> None:
        """Translate an O2 `chat_stream` request into agent execution + O2 frames.

        `emit(dict)` writes one `data: {json}` frame. `messages` is the O2
        conversation array; we run a turn for the latest user message. Session
        continuity is keyed by `session_hint` (the UI's per-chat session id).
        """
        # Extract the latest user message (the UI sends the whole history).
        user_text = ""
        for m in reversed(messages or []):
            if m.get("role") == "user" and m.get("content"):
                user_text = m["content"]
                break
        if not user_text:
            emit({"type": "error", "error": "no user message in request"})
            emit({"type": "complete"})
            return

        with self._chat_lock:
            sid = self._resolve_o2_session(session_hint)

            def on_event(event: str, **info):
                if event == "tool_start":
                    emit({"type": "tool_call",
                          "tool": info.get("tool", "tool"),
                          "message": _tool_label(info.get("tool", "tool"),
                                                  info.get("args") or {}),
                          "context": {},
                          "call_id": f"{info.get('tool','tool')}-{id(info)}"})

            try:
                answer = self.agent.chat(sid, user_text, on_event=on_event)
            except GateError as e:  # confirmation-gated write surfaced cleanly
                emit({"type": "error", "error": str(e)})
                emit({"type": "complete"})
                return
            except Exception as e:
                emit({"type": "error",
                      "error": f"{type(e).__name__}: {e}",
                      "suggestion": "Check the agent logs; the query may need a "
                                    "narrower time range or a valid stream."})
                emit({"type": "complete"})
                return

            # Stream the answer as message_delta chunks so the UI renders it
            # progressively (the agent returns the full string at once, so we
            # chunk it into reasonably sized deltas for a live-typing feel).
            for chunk in _chunk_text(answer):
                emit({"type": "message_delta", "content": chunk})

            # Surface copyable artifacts (SQL/VRL/PromQL/regex) as a fenced code
            # block appended after the prose, so they render in the UI's code box.
            turn = self.agent.last_turn
            for a in (getattr(turn, "artifacts", None) or []):
                if getattr(a, "text", None):
                    lang = _artifact_lang(getattr(a, "kind", ""))
                    emit({"type": "message_delta",
                          "content": f"\n\n```{lang}\n{a.text}\n```\n"})

            emit({"type": "complete",
                  "trace_id": sid})

    # Map the UI's per-chat session id to one of our agent sessions, creating
    # one on first sight so multi-turn context is preserved per chat tab.
    def _resolve_o2_session(self, hint: str | None) -> str:
        if not hasattr(self, "_o2_sessions"):
            self._o2_sessions: dict[str, str] = {}
        key = hint or "_default"
        sid = self._o2_sessions.get(key)
        if sid is None:
            sid = self.agent.start_session()
            self._o2_sessions[key] = sid
        return sid


class _Handler(BaseHTTPRequestHandler):
    service: O2Service = None       # injected on the server instance
    settings: Settings = None
    server_version = "O2Assistant/1"

    def log_message(self, *a):  # quiet by default
        pass

    # -- helpers ----------------------------------------------------------

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.settings.cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, traceparent, x-o2-assistant-session-id")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, path: str) -> bool:
        if path == "/api/o2/health" or not self.settings.api_token:
            return True
        # The native OpenObserve chat endpoint is authenticated by OpenObserve
        # itself (cookie/session on the same origin), not by our bearer token —
        # exempt it so the built-in Assistant panel can reach us unchanged.
        if path.endswith("/ai/chat_stream"):
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {self.settings.api_token}"

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- verbs ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized(path):
            return self._json(401, {"error": "unauthorized"})
        svc = self.service
        try:
            if path == "/api/o2/health":
                return self._json(200, svc.health())
            if path == "/api/o2/suggestions":
                return self._json(200, svc.suggestions())
            if path == "/api/o2/changes":
                return self._json(200, svc.changes())
            if path.startswith("/api/o2/sessions/") and path.endswith("/messages"):
                sid = path[len("/api/o2/sessions/"):-len("/messages")]
                return self._json(200, svc.messages(sid))
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized(path):
            return self._json(401, {"error": "unauthorized"})
        svc = self.service
        body = self._body()
        try:
            # OpenObserve-native endpoint: POST /api/{org}/ai/chat_stream — this
            # is what the built-in O2 Assistant panel posts to. We answer it in
            # the exact frame format the stock UI parses, so the native chat box
            # is driven by our agent with no front-end changes.
            if path.endswith("/ai/chat_stream"):
                messages = body.get("messages") or []
                sess = self.headers.get("x-o2-assistant-session-id")
                return self._o2_sse(messages, sess)
            if path == "/api/o2/sessions":
                return self._json(200, svc.new_session())
            if path == "/api/o2/chat":
                sid, msg = body.get("session_id"), body.get("message", "")
                if not sid or not msg:
                    return self._json(400, {"error": "session_id and message required"})
                return self._json(200, svc.chat(sid, msg))
            if path == "/api/o2/chat/stream":
                sid, msg = body.get("session_id"), body.get("message", "")
                if not sid or not msg:
                    return self._json(400, {"error": "session_id and message required"})
                return self._sse(sid, msg)
            if path == "/api/o2/sql":
                sql = body.get("sql", "")
                if not sql:
                    return self._json(400, {"error": "sql required"})
                return self._json(200, svc.run_sql(
                    body.get("session_id"), sql, int(body.get("size", 20))))
            if path == "/api/o2/feedback":
                sid, rating = body.get("session_id"), body.get("rating", "")
                if not sid or rating not in ("up", "down"):
                    return self._json(400, {"error": "session_id and rating up|down required"})
                return self._json(200, svc.feedback(sid, rating, body.get("note", "")))
            if path.startswith("/api/o2/changes/"):
                rest = path[len("/api/o2/changes/"):]
                cid, _, action = rest.partition("/")
                if action == "approve":
                    return self._json(200, svc.approve(cid, body.get("phrase")))
                if action == "reject":
                    return self._json(200, svc.reject(cid))
                if action == "run":
                    return self._json(200, svc.run(cid))
        except GateError as e:
            return self._json(409, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})

    def _sse(self, sid: str, msg: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # close after the turn so the client reads a clean EOF (one-shot stream)
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True

        def emit(event: str, data: dict) -> None:
            frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        self.service.chat_stream(sid, msg, emit)

    def _o2_sse(self, messages: list, session_hint: str | None) -> None:
        """Stream an OpenObserve-native `chat_stream` response.

        Frames are newline-delimited `data: {json}` objects dispatched by their
        `type` (message_delta / tool_call / complete / error / title) — exactly
        what the stock O2 Assistant UI parses.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True

        def emit(data: dict) -> None:
            frame = f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            self.service.o2_chat_stream(messages, session_hint, emit)
        except Exception as e:  # last-resort frame so the UI never hangs
            emit({"type": "error", "error": f"{type(e).__name__}: {e}"})
            emit({"type": "complete"})


def build_server(settings: Settings | None = None, agent: Agent | None = None):
    settings = settings or Settings.load()
    agent = agent or Agent(settings)
    handler = type("O2Handler", (_Handler,), {
        "service": O2Service(agent), "settings": settings})
    httpd = ThreadingHTTPServer((settings.api_host, settings.api_port), handler)
    return httpd, agent


def main() -> None:
    settings = Settings.load()
    httpd, agent = build_server(settings)
    host, port = settings.api_host, settings.api_port
    guard = "token-protected" if settings.api_token else "OPEN (set O2_API_TOKEN to protect)"
    print(f"# O2 Assistant API on http://{host}:{port}  (org={settings.org}, {guard})")
    print(f"# CORS origin: {settings.cors_origin}   docs: api-server.md")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        agent.close()
        print("\n# server stopped")


if __name__ == "__main__":
    main()
