# O2 Assistant — HTTP/SSE API (Phase 12)

The agent exposes a small, standard HTTP + SSE API so an external front-end (e.g.
the O2 Assistant panel embedded in the OpenObserve UI) can drive it. The CLI is
retained as a peer entry point; both share the same `Agent` core, `memory`, and
confirmation gate.

## Design goals

- **Standard & framework-light.** Stdlib `http.server` (ThreadingHTTPServer) — no
  new heavy dependency. JSON request/response, SSE for streaming.
- **Reuse, don't re-implement.** The Phase 10 `on_event` progress callback maps
  1:1 to SSE events; sessions/feedback/gate all come from the existing `Agent`.
- **Safe by default.** Optional bearer-token guard (`O2_API_TOKEN`); CORS origin
  is configurable; the write path stays confirmation-gated (propose → approve →
  run), never auto-executed.

## Auth & CORS

- If `O2_API_TOKEN` is set, every endpoint except `/api/o2/health` requires
  `Authorization: Bearer <token>`. If unset, the API is open (dev only — warned
  at startup).
- CORS: `Access-Control-Allow-Origin` = `O2_CORS_ORIGIN` (default `*`). Preflight
  `OPTIONS` is handled; allowed headers include `Authorization`, `Content-Type`.

## Concurrency model

- `ThreadingHTTPServer` — one thread per request.
- A single shared `Agent` (service-account creds). Read-only endpoints
  (`health`, `suggestions`, `changes`, `messages`) run without locking.
- `chat` / `chat/stream` are serialized by one `threading.Lock`, because a turn
  writes `agent.last_turn`. The turn's `TurnContext` is captured inside the lock.
- Per-user credential passthrough (P3) is plumbed at the `Agent` level but not yet
  surfaced per-request here; a multi-user deployment would use an Agent pool.

## Endpoints (prefix `/api/o2`)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET  | `/health` | — | `{ok, org, service}` (no auth) |
| GET  | `/suggestions` | — | `{groups: {query:[…], investigation:[…], …}}` |
| POST | `/sessions` | — | `{session_id}` |
| GET  | `/sessions/{sid}/messages` | — | `{messages:[{role,content}]}` |
| POST | `/chat` | `{session_id, message}` | `{answer, context, usage}` |
| POST | `/chat/stream` | `{session_id, message}` | SSE stream (see below) |
| POST | `/sql` | `{session_id?, sql, size?}` | `{ok, rows|error, scan_records, time_range, warnings}` |
| GET  | `/changes` | — | `{changes:[{id,tier,state,action,resource_kind,diff}]}` |
| POST | `/changes/{id}/approve` | `{phrase?}` | `{change}` |
| POST | `/changes/{id}/reject` | — | `{change}` |
| POST | `/changes/{id}/run` | — | `{change, result}` |
| POST | `/feedback` | `{session_id, rating, note?}` | `{ok}` |

### SSE stream (`POST /chat/stream`)

`Content-Type: text/event-stream`. Events:

```
event: progress
data: {"stage":"tool_start","tool":"search"}

event: progress
data: {"stage":"tool_end","tool":"search","ok":true,"scan_records":2,"duration_ms":9}

event: final
data: {"answer":"…","context":{"org":"default","streams":["bench_group_by"],
        "time_range":[start_us,end_us],"scan_records":2,"tools":[…],"artifacts":[…]},
        "usage":{"total_tokens":…,"cost_usd":…}}
```

On failure: `event: error` with `{"error":"…"}`.

### `context` object

Serialized `TurnContext`: `org`, `streams`, `time_range` (`[start_us,end_us]`),
`scan_records`, `tools` (called tool names), `artifacts`
(`[{kind,text,status}]` — the copyable SQL/VRL/PromQL/regex).

## Running

```bash
python -m o2agent serve          # start the API (defaults O2_API_HOST/PORT)
python -m o2agent chat           # interactive CLI (unchanged)
python -m o2agent smoke          # read-only smoke test
```

`python -m o2agent.cli` and `python -m o2agent.server` also still work directly.

For production deployment (reverse-proxy same-origin vs. separate-origin CORS,
SSE proxy tuning, auth, health checks), see `deploy.md`.

## Out of scope (v1)

- "Auto Navigation" of the OpenObserve UI (a front-end concern).
- Per-request per-user OpenObserve credentials (needs an Agent pool).
- WebSocket transport (SSE is sufficient for one-way progress streaming).
