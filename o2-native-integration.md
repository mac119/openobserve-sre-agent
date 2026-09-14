# Native O2 Assistant chat box ← powered by our Agent (integration record)

This file records how the **AI Assistant chat box built into OpenObserve Community**
is powered by our `o2agent` on the backend — with **zero front-end changes**, using
just one protocol adapter endpoint + one nginx routing rule.

## Background: why this works

- OpenObserve Community **ships** the AI Assistant front-end UI
  (`web/src/components/ai-assistant/` + `O2AIChat.vue` + `useAiChat.ts` +
  `services/ai_chat.ts`), but the community backend has **no AI capability** (the SRE
  Agent is enterprise-only). So that chat box is an inert shell in community builds.
- The front-end posts all chat to one endpoint: `POST /api/{org}/ai/chat_stream`, and
  reads a **newline-delimited `data: {json}` stream**, dispatching frames by `type`.
- We just make that endpoint answered by our agent, emitting the frames the front-end
  expects — lighting it up.

## Front-end chat_stream contract (we must implement)

- Request: `POST /api/{org}/ai/chat_stream`
  - body: `{ messages:[{role,content}], context:{user_timezone,...}, model?, images? }`
  - session: HTTP header `x-o2-assistant-session-id` (a UUID v7 per chat tab)
  - auth: `credentials:include` (cookie, authenticated to OpenObserve, not to us)
- Response: `200` + body, one `data: {json}\n\n` frame at a time, frame types (`data.type`):
  | type | fields | purpose |
  |---|---|---|
  | `message_delta` | `content` | text delta (front-end appends) **core** |
  | `tool_call` | `tool,message,context,call_id` | tool-execution progress (spinner) |
  | `tool_result` | … | tool result (optional) |
  | `error` | `error, suggestion?, error_type?` | error bubble |
  | `complete` | `trace_id?` | end |
  | `title` | `title` | conversation title (optional) |

## Our implementation (Option A: agent adds an O2-compatible endpoint, zero front-end change)

Code: `o2agent/server.py`
- New frame helpers: `_tool_label()` / `_artifact_lang()` / `_chunk_text()`
- `O2Service.o2_chat_stream(messages, session_hint, emit)`:
  - take the last user content in `messages` → call `agent.chat()`
  - `on_event(tool_start)` → emit a `tool_call` frame
  - the agent's full answer → chunk it via `_chunk_text` into `message_delta` frames (typewriter feel)
  - append `last_turn.artifacts` (SQL/VRL/PromQL/regex) as a fenced code block
  - emit `complete` at the end; on `GateError`/exception emit `error` + `complete`
  - session mapping: `_resolve_o2_session(hint)` reuses/creates an agent session keyed
    by the UI's session id (multi-turn memory)
- `_Handler`:
  - route `POST *​/ai/chat_stream` → `_o2_sse()` (emits `data: {json}\n\n`)
  - `_authorized()`: this endpoint is exempt from the bearer token (authed by
    OpenObserve's same-origin cookie)
  - `_cors()`: allow the `traceparent, x-o2-assistant-session-id` headers

> All safety rails stay in place: read-only client, validator gate, scan budget,
> three-tier gate (writes remain propose→approve→run, never auto-executed), same-org
> assertion, memory system.

## vite proxy (dev only)

File: `web/vite.config.ts`. Inject a regex rule at the **top** of `server.proxy`
(before `/api`):

```ts
proxy: {
  '^/api/[^/]+/ai/chat_stream': { target: 'http://localhost:8799', changeOrigin: true },
  '/api':    { target: 'http://localhost:5080', changeOrigin: true },
  '/auth':   { target: 'http://localhost:5080', changeOrigin: true },
  '/config': { target: 'http://localhost:5080', changeOrigin: true },
}
```

- Restart the vite dev server after the change (`vite.config.ts` is not hot-reloaded).
- Production does not use this rule (nginx routes instead), so the open-source release
  can keep OpenObserve's source unmodified.

## Deployment topology

```
browser → http://<server>:8088  (nginx: dist + routing)
          ├ /api/{org}/ai/chat_stream →(proxy)→ o2agent :8799 → LLM + OpenObserve :5080
          └ /api/*                     →(proxy)→ OpenObserve :5080  (login/data)

o2agent: systemd service (enabled), venv, connects to local OpenObserve :5080 (loopback)
```

Production: `vite build` → `web/dist` copied to `/var/www/o2`, served by nginx; nginx
reverse-proxies `/api/{org}/ai/chat_stream` to 8799 and everything else to 5080.

## Verified

- Direct to agent: `POST :8799/api/default/ai/chat_stream` → `tool_call` + several
  `message_delta` (real stream data) + `complete`.
- Via nginx: `POST :8088/api/default/ai/chat_stream` → same frames; multi-turn session
  memory works.
- Browser: open the entry, log in, open the AI Assistant, ask — answered by our agent.

## Gotchas

1. **LLM gateway allow-list**: the server initially couldn't reach the LLM gateway
   (TCP blocked, DNS fine), so the agent's LLM calls timed out (curl exit=28). Fixed by
   allow-listing the server IP. Diagnose by comparing `curl --max-time 6` to a few
   public hosts (all OK) vs the gateway (BLOCKED).
2. **vite proxy order**: `/api` is a prefix match and wins; the `ai/chat_stream` rule
   must come **before** it, using the regex key `^/api/[^/]+/ai/chat_stream`.
3. **vite.config not hot-reloaded**: restart the dev server after editing the proxy.
4. **agent.chat is not token-level streaming**: `on_event` only fires tool_start/
   tool_end; the answer returns at once. The adapter chunks the full answer into
   `message_delta` frames via `_chunk_text` for a typewriter effect.
5. **nginx root under a home dir → 500**: `/home/ubuntu` is `drwxr-x---`, www-data
   can't traverse it, causing `Permission denied` + redirect cycle. Fix: copy dist to
   `/var/www/o2` (chown www-data). Also **a root change needs `restart nginx`, not
   `reload`.**
6. **Cloud security group**: local firewall was fully open, but the cloud security
   group only allowed specific ports; 8081/8088 weren't reachable publicly (loopback
   worked). The entry port must be opened in the cloud security group.
