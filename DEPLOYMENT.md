# OpenObserve SRE Agent — Deployment Guide

Put a **standalone AI agent** behind the **AI Assistant chat box shipped in
OpenObserve Community**: users ask questions in the native OpenObserve UI, and the
answer comes from your own agent. **The OpenObserve front-end is unchanged** — the
integration is done entirely via a "protocol adapter endpoint + reverse proxy".

> Applies to: OpenObserve Community (open-source build). It ships the AI Assistant
> front-end, but the community backend has no AI capability, so that chat box is an
> inert shell by default. This guide lights it up.

---

## 1. Architecture

```
                         ┌─────────────────────── one server ───────────────────────┐
 browser ──▶ nginx :8088  │                                                          │
            (entry)        │   location ~ /api/{org}/ai/chat_stream ──▶ o2agent :8799 │──▶ LLM gateway
                          │   location /api /auth /config          ──▶ OpenObserve   │──▶ :5080
                          │   location /                           ──▶ web/dist       │
                          └──────────────────────────────────────────────────────────┘
```

- **o2agent**: your agent (this repo). Exposes an "OpenObserve-compatible endpoint"
  `POST /api/{org}/ai/chat_stream` that translates the agent's execution into the
  streaming frames the OpenObserve front-end expects. It reads data from OpenObserve
  REST as a read-only client.
- **OpenObserve**: community build, provides the data API (`/api`), login (`/auth`),
  and the front-end source (`web/`).
- **nginx**: the unified entry; routes AI chat to o2agent, everything else to
  OpenObserve, and serves the front-end.

### Why the front-end needs no changes

All AI chat in the OpenObserve front-end posts to a single endpoint:
`POST /api/{org}/ai/chat_stream` (body `{messages, context, ...}`) and reads a
**newline-delimited `data: {json}` stream**, dispatching frames by `data.type`. We
only need this endpoint answered by o2agent, emitting frames the front-end recognizes
— without touching any `.vue` / service code.

### The chat_stream frame protocol (the agent must emit these)

| `type` | fields | purpose |
|---|---|---|
| `message_delta` | `content` | text delta, appended progressively (core) |
| `tool_call` | `tool, message, context, call_id` | tool-execution progress (spinner) |
| `tool_result` | … | tool result (optional) |
| `error` | `error, suggestion?, error_type?` | error bubble |
| `complete` | `trace_id?` | end of turn |
| `title` | `title` | conversation title (optional) |

Transport: `fetch` reads `response.body`, takes lines prefixed `data: `, `JSON.parse`s
them, dispatches by `type`.
Session: HTTP header `x-o2-assistant-session-id` (one id per chat tab).
Auth: `credentials:include` (cookie, authenticated to OpenObserve itself).

### 1.1 Full sequence of one turn (with auth cookie / session header)

```
┌────────┐        ┌───────────┐        ┌──────────────┐      ┌──────────────┐   ┌─────────┐
│ browser │        │  nginx     │        │  o2agent     │      │ OpenObserve  │   │  LLM    │
│(O2 front)│       │  :8088     │        │  :8799       │      │  :5080       │   │ gateway │
└───┬────┘        └─────┬─────┘        └──────┬───────┘      └──────┬───────┘   └────┬────┘
    │                   │                     │                     │                │
    │ (0) user logs into OpenObserve first: browser now holds the O2 session cookie  │
    │───────────────────┼─────────────────────┼────────────────────▶│  (Set-Cookie)  │
    │                   │                     │                     │                │
    │ (1) POST /api/default/ai/chat_stream                          │                │
    │     Cookie: <O2 session cookie>         (credentials:include) │                │
    │     x-o2-assistant-session-id: <UUIDv7> (one per chat tab)    │                │
    │     traceparent: <trace id>                                   │                │
    │     body: {messages:[…], context:{user_timezone,…}}           │                │
    │──────────────────▶│                     │                     │                │
    │                   │ (2) location regex ^/api/[^/]+/ai/chat_stream$ matches      │
    │                   │     → proxy to o2agent (other /api goes to 5080)           │
    │                   │     forwards Cookie / x-o2-assistant-session-id / traceparent│
    │                   │────────────────────▶│                     │                │
    │                   │                     │ (3) endpoint exempt from the agent's  │
    │                   │                     │     own bearer token (authed by O2    │
    │                   │                     │     same-origin cookie); take the last │
    │                   │                     │     user message from `messages`       │
    │                   │                     │                     │                │
    │                   │                     │ (4) session mapping: use the header    │
    │                   │                     │     x-o2-assistant-session-id to find/  │
    │                   │                     │     create an agent session (memory)    │
    │                   │                     │                     │                │
    │                   │                     │ (5) run agent: tools read data         │
    │                   │                     │─────────GET /api────▶│                │
    │                   │                     │◀──── streams/alerts/query results ────│
    │                   │                     │                     │                │
    │                   │                     │ (6) call the LLM     │                │
    │                   │                     │──────────────────────┼───────────────▶│
    │                   │                     │◀──────── answer ─────┼────────────────│
    │                   │                     │                     │                │
    │                   │  (7) stream frames back (SSE, proxy_buffering off)          │
    │                   │◀────data: {type:"tool_call",…}────────────│                │
    │◀──────────────────│      data: {type:"message_delta",content}  │  (chunks)      │
    │  render text      │      … (N message_delta frames)            │                │
    │                   │◀────data: {type:"complete","trace_id":…}──│                │
    │◀──────────────────│                     │                     │                │
    │ (8) front-end dispatches by type: tool_call→spinner; message_delta→append; complete→end │
    │                   │                     │                     │                │
```

**Auth chain:**
- The request carries the **OpenObserve session cookie** (set when the user logs into
  O2). Because nginx keeps the front-end, O2 and the agent **same-origin** (all on
  `:8088`), the cookie is sent automatically.
- **The agent does not verify this cookie, nor does it need its own bearer token** for
  this endpoint: it's exempted in `server.py`'s `_authorized()`. Access control is
  enforced by the **same-origin boundary** — only a browser that can reach nginx :8088
  and is logged into O2 can call it. **Therefore never expose the agent's 8799 to the
  public internet**; it must only be reached by the local nginx (`O2_API_HOST=127.0.0.1`).
- The agent reads OpenObserve data using the **service account in `.env`**
  (`OPENOBSERVE_AUTH`), independent of the browser cookie — i.e. two separate chains:
  "front-end authenticates to O2" and "agent reads with its own service account".

**Session header:**
- The front-end generates a **UUID v7** per chat tab in `x-o2-assistant-session-id`;
  all turns of the same conversation carry the same id.
- The agent maps it to an internal agent session (`_resolve_o2_session`), preserving
  **multi-turn context and memory**; different tabs / ids are isolated.

---

## 2. Prerequisites

- A Linux server (Ubuntu 22.04 in this guide) with `sudo`.
- A running OpenObserve Community (listening on `127.0.0.1:5080` here).
- Python ≥ 3.10 (to run o2agent).
- Node ≥ 20 + pnpm/npm (only if you rebuild the front-end).
- An **OpenAI-compatible** LLM endpoint (OpenAI / DashScope / self-hosted, etc.).

---

## 3. Deploy o2agent (systemd)

### 3.1 Code and dependencies

```bash
sudo mkdir -p /opt/o2agent && sudo chown $USER /opt/o2agent
cd /opt/o2agent
# copy this repo here (o2agent/ requirements.txt spec.md ...)
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3.2 Configure `.env`

```bash
# --- OpenObserve (agent reads data as a read-only client) ---
OPENOBSERVE_BASE_URL=http://127.0.0.1:5080     # loopback when co-located with OpenObserve
OPENOBSERVE_ORG=default
OPENOBSERVE_AUTH=Basic <base64(user:token)>    # OpenObserve service account

# --- LLM (OpenAI-compatible) ---
LLM_PROVIDER=openai
LLM_BASE_URL=https://your-llm-gateway/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=gpt-4o                                # or your model name

# --- API server ---
O2_API_HOST=127.0.0.1                           # reached only by the local nginx
O2_API_PORT=8799

# --- Safety (recommended) ---
O2_ENDPOINT_ALLOWLIST=127.0.0.1                 # restrict the agent's egress to local OpenObserve
O2_MEMORY_KEY=<long-random>                     # encrypt conversation memory at rest (optional)
O2_MAX_ROWS=1000
O2_SCAN_RECORDS_BUDGET=...
```

> **Important**: the `ai/chat_stream` endpoint is authenticated by OpenObserve's
> same-origin cookie; the agent exempts this endpoint from its own bearer token (see
> `server.py`'s `_authorized`). Other `/api/o2/*` management endpoints stay protected
> by `O2_API_TOKEN` (if set).

### 3.3 systemd unit

`/etc/systemd/system/o2agent.service`:

```ini
[Unit]
Description=OpenObserve SRE Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/o2agent
EnvironmentFile=/opt/o2agent/.env
ExecStart=/opt/o2agent/venv/bin/python -m o2agent serve
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now o2agent
systemctl status o2agent
curl -s http://127.0.0.1:8799/api/o2/health      # {"ok":true,...}
journalctl -u o2agent -f                          # follow logs
```

---

## 4. Build the front-end (production static assets)

The front-end source needs **no changes** for this approach: it posts the relative
path `/api/{org}/ai/chat_stream`, routed by nginx in production (no vite dev proxy).

```bash
cd <openobserve>/web
# skip strict type-check, plain vite build (more robust on large projects)
NODE_OPTIONS=--max-old-space-size=6144 npx vite build
# output is web/dist; copy to an nginx-readable dir (avoid /home due to permission 500)
sudo rm -rf /var/www/o2 && sudo mkdir -p /var/www/o2
sudo cp -r web/dist/. /var/www/o2/
sudo chown -R www-data:www-data /var/www/o2
```

---

## 5. nginx unified entry

`/etc/nginx/conf.d/o2-agent.conf`:

```nginx
server {
    listen 8088;                 # change port / add server_name as needed
    root /var/www/o2;
    index index.html;

    # (1) AI chat -> our agent (must precede the generic /api/, exact regex)
    location ~ ^/api/[^/]+/ai/chat_stream$ {
        proxy_pass         http://127.0.0.1:8799;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        # SSE streaming: no buffering, long timeout
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }

    # (2) everything else API / auth / config -> OpenObserve
    location /api/  {
        proxy_pass         http://127.0.0.1:5080;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   Upgrade $http_upgrade;      # OpenObserve websocket
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 300s;
    }
    location /auth/  { proxy_pass http://127.0.0.1:5080; proxy_set_header Host $host; }
    location /config { proxy_pass http://127.0.0.1:5080; proxy_set_header Host $host; }
    location /web/   { proxy_pass http://127.0.0.1:5080; proxy_set_header Host $host; }

    # (3) front-end SPA
    location / { try_files $uri $uri/ /index.html; }
}
```

Apply:

```bash
sudo nginx -t
sudo systemctl restart nginx     # note: a root change needs restart, reload is not enough
```

### HTTPS (optional, recommended for production)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```
Replace `listen 8088;` with `listen 443 ssl; server_name your-domain.com;` and let
certbot manage it.

---

## 6. Network

- Open the entry port (e.g. 8088) in the server's local firewall (ufw/iptables).
- **Cloud security group**: your cloud platform (Tencent Cloud / AWS / Aliyun) must
  allow inbound TCP 8088 (or 443). Common gotcha: `127.0.0.1:8088` works locally but
  the public IP doesn't — that's an unopened security group.
- Agent → LLM gateway: if the gateway has an IP allow-list, add the server's public
  IP, otherwise the agent's LLM calls time out (chat box spins then fails). Check:
  `curl --max-time 6 https://<llm-gateway>/v1/models`.

---

## 7. Verify

```bash
# agent health
curl -s http://127.0.0.1:8799/api/o2/health

# the agent's O2-compatible endpoint directly (should emit message_delta / complete)
curl -s -X POST http://127.0.0.1:8799/api/default/ai/chat_stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"list my streams"}],"context":{"user_timezone":"UTC"}}'

# via nginx (production entry)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/          # 200 front-end
curl -s http://127.0.0.1:8088/config | head -c 60                        # OpenObserve version
curl -s -X POST http://127.0.0.1:8088/api/default/ai/chat_stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}],"context":{}}'      # frames
```

Open `http://<server>:8088` (or your domain) → log in to OpenObserve → open the AI
Assistant → ask a question; the answer comes from your agent.

---

## 8. Development mode (optional, hot-reload)

Production uses nginx + dist; for development you can use the vite dev server with
hot-reload and proxy the AI endpoint to the agent.

Add to the **top** of `server.proxy` in `web/vite.config.ts` (before `/api`):

```ts
proxy: {
  '^/api/[^/]+/ai/chat_stream': { target: 'http://localhost:8799', changeOrigin: true },
  '/api':    { target: 'http://localhost:5080', changeOrigin: true },
  '/auth':   { target: 'http://localhost:5080', changeOrigin: true },
  '/config': { target: 'http://localhost:5080', changeOrigin: true },
}
```

```bash
cd web && npx vite   # dev server (port in its output); restart after editing the proxy
```

> This is the **only optional change** to OpenObserve's source in this approach.
> Production does not need it; if you want OpenObserve's source strictly unmodified,
> develop against a standalone static panel instead.

---

## 9. Components & ports

| Component | Port | Process mgmt | Notes |
|---|---|---|---|
| nginx (entry) | 8088 | systemd | serves dist + routing |
| o2agent | 8799 | systemd (enabled) | O2-compatible endpoint + REST reads |
| OpenObserve | 5080 | existing | data / login / front-end source |
| vite dev (optional) | see output | manual | dev hot-reload |

---

## 10. FAQ

- **Chat box spins then errors**: usually the agent can't reach the LLM (gateway
  allow-list / network). Check `journalctl -u o2agent`.
- **Static page 500 + Permission denied**: dist under `/home/...` isn't readable by
  www-data; copy it to `/var/www`.
- **nginx root change not taking effect**: `restart nginx`, not `reload`.
- **AI request goes to 5080 (404 / no response)**: the `ai/chat_stream` location must
  come **before** `/api/`.
- **Will writes auto-execute**: no. The agent's write path is a three-tier
  propose → approve → run gate; it only produces "change proposals" and never
  auto-commits.

---

## 11. Security notes (for open source)

- The agent accesses OpenObserve **read-only** by default (GET + a bounded `_search`
  POST); mutating operations go through the confirmation gate.
- The `ai/chat_stream` endpoint relies on OpenObserve's same-origin session auth;
  **always** keep nginx/OpenObserve same-origin and never expose the agent's 8799
  publicly.
- LLM keys and OpenObserve credentials live in `.env`; do not commit them; use a
  secrets manager in production.
