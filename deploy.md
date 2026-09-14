# O2 Assistant — Deployment Topologies

How to run the Phase 12 HTTP/SSE API (`server.py`) alongside an OpenObserve UI
and the O2 Assistant front-end. Two supported topologies; pick by whether the
front-end and API share an origin.

The API contract is in `api-server.md`. Config keys used here (all from env /
`.env`, see `config.py`):

| Key | Default | Meaning |
|---|---|---|
| `O2_API_HOST` | `127.0.0.1` | bind address (use `0.0.0.0` behind a proxy) |
| `O2_API_PORT` | `8799` | API port |
| `O2_API_TOKEN` | *(empty)* | bearer token; empty = OPEN (dev only). `/api/o2/health` is always open |
| `O2_CORS_ORIGIN` | `*` | `Access-Control-Allow-Origin` for the front-end |

Start the API:

```bash
python -m o2agent serve
```

---

## Topology A — Reverse proxy, same origin (recommended for production)

The front-end and the API are served under **one origin**; a reverse proxy
(nginx/Caddy/Traefik) routes `/api/o2/*` to the agent and everything else to the
front-end (and/or OpenObserve). Because it's same-origin, **no CORS is needed**.

```
                         ┌──────────────────────────────┐
   browser ──────────▶   │  reverse proxy  (one origin)  │
   https://obs.example   └──────────────┬───────────────┘
                                        │
             /api/o2/*  ────────────────┼────────────▶  o2agent serve  (127.0.0.1:8799)
             everything else  ──────────┼────────────▶  OpenObserve UI (127.0.0.1:5080)
```

Agent config:

```bash
O2_API_HOST=127.0.0.1        # only the proxy reaches it
O2_API_PORT=8799
O2_API_TOKEN=<long-random>   # still set one; defense in depth
# O2_CORS_ORIGIN unused (same origin)
```

nginx example:

```nginx
server {
    listen 443 ssl;
    server_name obs.example.com;

    # O2 Assistant API
    location /api/o2/ {
        proxy_pass         http://127.0.0.1:8799;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   Authorization $http_authorization;

        # --- SSE: these three are REQUIRED for /api/o2/chat/stream ---
        proxy_buffering    off;      # do not buffer the event stream
        proxy_cache        off;
        proxy_read_timeout 300s;     # long turns must not be cut off
    }

    # OpenObserve UI (and its own API)
    location / {
        proxy_pass http://127.0.0.1:5080;
    }
}
```

Front-end calls the API at a **relative** path — `POST /api/o2/chat/stream` —
so it inherits the page origin. Nothing CORS-related to configure.

**Pros:** no CORS, one TLS cert, one hostname, token travels same-origin.
**Cons:** you run a proxy.

---

## Co-located with OpenObserve (single-server, recommended)

The agent and OpenObserve run on the **same machine**. This is the simplest,
lowest-latency setup: the agent talks to OpenObserve over loopback, and an nginx
reverse proxy fronts both under one origin (Topology A). Only the LLM gateway is
off-box.

```
                     ┌───────────────────── one server ─────────────────────┐
 browser ──443──▶    │  nginx (same origin)                                  │
 https://obs.host    │    /api/o2/*  ──▶  o2agent serve   127.0.0.1:8799      │
                     │    everything ──▶  OpenObserve     127.0.0.1:5080      │
                     └───────────────────────────┬───────────────────────────┘
                                                 │  (egress)
                                                 ▼
                                        LLM gateway (LiteLLM / DashScope …)
```

### 1. Agent `.env` for co-location

```bash
# talk to the local OpenObserve over loopback (lower latency, stays on-box)
OPENOBSERVE_BASE_URL=http://127.0.0.1:5080
OPENOBSERVE_ORG=default
OPENOBSERVE_AUTH=Basic <base64(user:token)>

# LLM gateway (this is the only outbound dependency)
LLM_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=...

# API: bind loopback only; nginx is the sole way in
O2_API_HOST=127.0.0.1
O2_API_PORT=8799
O2_API_TOKEN=<long-random>          # defense in depth even behind the proxy

# safety: pin the egress allow-list to localhost (Phase 8 check_endpoint enforces)
O2_ENDPOINT_ALLOWLIST=127.0.0.1
# recommended hardening for a real deployment
O2_MEMORY_KEY=<long-random-passphrase>   # encrypt session memory at rest
O2_RETENTION_DAYS=30                       # auto-purge old sessions
```

> Ports: OpenObserve `5080` and the agent `8799` don't collide. The agent is
> light (httpx + stdlib http.server + local SQLite); the heavy LLM compute is
> remote, so co-locating it with OpenObserve adds little load.

### 2. Install & run the agent as a service

```bash
# as root (or with sudo)
useradd --system --home /opt/o2agent --shell /usr/sbin/nologin o2agent
mkdir -p /opt/o2agent && cd /opt/o2agent
git clone <repo> . || true               # or copy the project here
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env                       # then edit .env per section 1
chown -R o2agent:o2agent /opt/o2agent
```

systemd unit — `/etc/systemd/system/o2agent.service`:

```ini
[Unit]
Description=O2 Assistant API (OpenObserve AI agent)
After=network-online.target
Wants=network-online.target
# if OpenObserve runs under systemd on this host, start after it:
After=openobserve.service

[Service]
Type=simple
User=o2agent
Group=o2agent
WorkingDirectory=/opt/o2agent
EnvironmentFile=/opt/o2agent/.env
ExecStart=/opt/o2agent/venv/bin/python -m o2agent serve
Restart=on-failure
RestartSec=3
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
# the agent writes its SQLite memory under the service user's home
ReadWritePaths=/opt/o2agent

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
systemctl daemon-reload
systemctl enable --now o2agent
systemctl status o2agent
journalctl -u o2agent -f          # follow logs
curl -s http://127.0.0.1:8799/api/o2/health
```

### 3. Install nginx as the same-origin reverse proxy

Install:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y nginx
# RHEL / CentOS / Rocky
sudo dnf install -y nginx

sudo systemctl enable --now nginx
```

Site config — `/etc/nginx/conf.d/o2.conf` (or
`/etc/nginx/sites-available/o2` + a symlink into `sites-enabled/` on Debian):

```nginx
server {
    listen 443 ssl;
    server_name obs.example.com;

    ssl_certificate     /etc/ssl/certs/obs.example.com.pem;
    ssl_certificate_key /etc/ssl/private/obs.example.com.key;

    # --- O2 Assistant API ---
    location /api/o2/ {
        proxy_pass         http://127.0.0.1:8799;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   Authorization $http_authorization;

        # SSE (required for /api/o2/chat/stream)
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }

    # --- OpenObserve UI + its own API ---
    location / {
        proxy_pass         http://127.0.0.1:5080;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   Upgrade $http_upgrade;         # OpenObserve websockets
        proxy_set_header   Connection "upgrade";
    }
}

# optional: redirect http -> https
server {
    listen 80;
    server_name obs.example.com;
    return 301 https://$host$request_uri;
}
```

Test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

No TLS cert yet? Get one with certbot:

```bash
sudo apt install -y certbot python3-certbot-nginx   # Debian/Ubuntu
sudo certbot --nginx -d obs.example.com
```

### 4. Verify end-to-end

```bash
# health (no auth)
curl -s https://obs.example.com/api/o2/health
# suggestions (with token)
curl -s -H "Authorization: Bearer $O2_API_TOKEN" \
     https://obs.example.com/api/o2/suggestions
```

The front-end (embedded in the OpenObserve UI) now calls the API at the relative
path `/api/o2/...` — same origin, no CORS.

---

## Topology B — Separate origin, CORS (simplest for a quick start / dev)

The API runs on its own host:port and the browser calls it **cross-origin**. The
agent must return the front-end's origin in `Access-Control-Allow-Origin`.

```
   browser ──▶  front-end            https://o2-ui.example      (origin A)
      │
      └───────▶  o2agent serve        https://o2-api.example:8799 (origin B, CORS)
```

Agent config:

```bash
O2_API_HOST=0.0.0.0
O2_API_PORT=8799
O2_API_TOKEN=<long-random>
O2_CORS_ORIGIN=https://o2-ui.example   # the EXACT front-end origin, not *
```

Notes:

- Set `O2_CORS_ORIGIN` to the exact front-end origin (scheme + host + port). `*`
  is fine only for local dev; a specific origin is required if you ever send
  credentials/cookies.
- The server already handles the `OPTIONS` preflight and echoes
  `Access-Control-Allow-Headers: Authorization, Content-Type`.
- Terminate TLS in front of the API (a proxy or the platform's load balancer);
  `server.py` itself speaks plain HTTP.

**Pros:** no proxy to run; quickest to stand up.
**Cons:** CORS to manage; two hostnames/certs; browser preflight on each POST.

---

## Auth

- Always set `O2_API_TOKEN` outside local dev. The front-end sends
  `Authorization: Bearer <token>` on every call; `/api/o2/health` is intentionally
  open for liveness probes.
- The token is a **shared service token**, not a per-user credential. Per-user
  OpenObserve permission passthrough is plumbed at the Agent level (`actor_auth`)
  but not yet surfaced per-request (needs an Agent pool) — see `tasks.md` Phase 12.

## SSE checklist (both topologies)

`POST /api/o2/chat/stream` is a Server-Sent Events stream. If progress events
don't arrive incrementally, it's almost always proxy buffering:

- nginx: `proxy_buffering off;` (see above). Caddy: `flush_interval -1`.
- Give the route a long read timeout (≥ your longest turn); the server closes the
  connection cleanly after the `final` event.
- Don't gzip the event stream.

## Health check & process management

- Liveness/readiness: `GET /api/o2/health` → `200 {"ok":true,...}` (no auth).
- `server.py` is a foreground process; run it under systemd / a container /
  supervisor. It's single-process, multi-threaded (`ThreadingHTTPServer`), with
  per-thread SQLite connections — safe to serve concurrent requests, but for
  horizontal scale-out put a load balancer in front and give each instance its
  own memory DB (or a shared DB path on a filesystem that supports it).

## Quick verification

```bash
curl -s http://127.0.0.1:8799/api/o2/health
curl -s -H "Authorization: Bearer $O2_API_TOKEN" \
     http://127.0.0.1:8799/api/o2/suggestions
```

For the offline endpoint smoke (ephemeral port, no LLM): `python -m o2agent.server_selftest`.
