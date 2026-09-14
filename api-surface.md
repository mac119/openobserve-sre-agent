# OpenObserve API Surface (v0.91.0-rc1)

Phase 0 research result. Probed against a test environment
`http://<openobserve-host>:5080`, org `default`, using service-account Basic auth.

All probing used **read-only (GET / bounded POST search)** calls. No resource was
created, modified, or deleted.

## Auth

- Basic auth with a **service account**: `<email>:<token>`. This is the
  only credential that works for API browsing (read/write endpoints).
- Header form: `Authorization: Basic <base64(user:token)>`.
- The **ingestion token** (`o2oi_...`) is for data ingestion only. It does NOT
  authenticate API browsing under any username — a normal user with an ingestion
  token returns 401. Do not use it as an API credential.

## Verified endpoints

| Capability | Method | Path | Status | Notes |
|---|---|---|---|---|
| List streams | GET | `/api/{org}/streams` | 200 | Returns `list[]` with `schema`, `settings`, `stats` |
| Stream schema | GET | `/api/{org}/streams/{name}/schema?type=logs` | 200 | `schema: [{name, type}]`, types like `Int64`, `Utf8` |
| Search (SQL) | POST | `/api/{org}/_search?type=logs` | 200 | body `{query:{sql,start_time,end_time,size}}`, times in **microseconds** |
| PromQL instant | GET | `/api/{org}/prometheus/api/v1/query?query=` | 200 | Prometheus-compatible |
| **Alerts** | GET | `/api/v2/{org}/alerts` | 200 | **v2 path** — v1 `/api/{org}/alerts` returns 404 |
| **Alert history** | GET | `/api/v2/{org}/alerts/history` | 200 | **Evaluation history exists** — enables factual "why did it fire" |
| Alert templates | GET | `/api/{org}/alerts/templates` | 200 | |
| Alert destinations | GET | `/api/{org}/alerts/destinations` | 200 | |
| Dashboards | GET | `/api/{org}/dashboards` | 200 | |
| Functions | GET | `/api/{org}/functions` | 200 | Reusable VRL functions |
| Pipelines | GET | `/api/{org}/pipelines` | 200 | |
| Reports | GET | `/api/{org}/reports` | 200 | |
| Saved views | GET | `/api/{org}/savedviews` | 200 | |
| Enrichment tables | GET | `/api/{org}/streams?type=enrichment_tables` | 200 | **Not** `/enrichment_tables` (404) |
| Users | GET | `/api/{org}/users` | 200 | Management surface (read) |
| Service accounts | GET | `/api/{org}/service_accounts` | 200 | Management surface (read) |
| Organizations | GET | `/api/organizations` | 200 | Org-level, no `{org}` prefix |
| Org settings | GET | `/api/{org}/settings` | 200 | |
| Org summary / usage | GET | `/api/{org}/summary` | 200 | Global counts + health for streams/pipelines/alerts/functions/dashboards |
| RUM token | GET | `/api/{org}/rumtoken` | 200 | Returns `{user, rum_token}` |
| Health | GET | `/healthz` | 200 | Unauthenticated |
| Config | GET | `/config` | 200 | Unauthenticated; use for version/build info |

## Access-controlled (exist but forbidden for this service account)

| Capability | Path | Status | Notes |
|---|---|---|---|
| Roles | `/api/{org}/roles` | 403 | Exists; current service account lacks permission — good Tier-1 permission-propagation test |
| Groups | `/api/{org}/groups` | 403 | Same |

## Not found / needs follow-up

| Capability | Attempted path | Status | Action |
|---|---|---|---|
| Incidents | `/api/{org}/incidents`, `/api/v2/{org}/incidents` | 404 | **No dedicated incidents endpoint in this version.** Incident features in `spec.md` §3.7 must be reframed around alerts + alert history, or marked unsupported |
| Dedicated usage | `/api/{org}/usage` | 404 | Superseded by `/api/{org}/summary` |
| Version (API) | `/api/version`, `/api/_meta/version` | 401 | Use `/config` instead |

## Impact on `tasks.md`

1. **Alerts and alert history use the `v2` path** — Phase 2 alert tools must target
   `/api/v2/{org}/alerts` and `/api/v2/{org}/alerts/history`, not v1.
2. **Alert evaluation history is available** — the "Why did my last alert fire?"
   investigation (Phase 6) can be grounded in real data, not a guess.
3. **Enrichment tables are accessed via the streams endpoint** with
   `type=enrichment_tables`, not a dedicated path.
4. **Usage/quota** is served by `/api/{org}/summary` (global counts + health),
   not a dedicated `/usage` endpoint.
5. **No dedicated incidents endpoint** in v0.91.0-rc1 — the incident-investigation
   capability (`spec.md` §3.7, `tasks.md` Phase 6) must be reframed around alerts
   + alert history, or explicitly marked unsupported for this version.
6. **Roles/groups return 403** for the test service account — a good fixture for
   the Tier-1 permission-propagation and cross-permission-denial tests (Phase 9).
7. **Ingestion token is not an API credential** — API tools must use the service
   account only.

## Pagination, limits, and query cost

Probed against `bench_group_by` (10,000,001 records).

**Search (`_search`) pagination:** uses `from` + `size` in the query body.
Verified `from:0/2` paging returns distinct pages.

**Result size is NOT capped server-side.** `size:100000` returned 100,000 rows
with HTTP 200 — no truncation, no rejection. **The agent must enforce a row
limit itself**; OpenObserve will not. This confirms `spec.md` §5 (raw-event
queries need an explicit limit) is mandatory, not optional.

**Scan cost signal.** Every response includes `scan_records` and `scan_size`.
For a query without index/filter pushdown, `scan_records` equals the full stream
(10M) even when only 2 rows are returned, and even for `GROUP BY` aggregations.
Therefore **scan protection must be driven by `scan_records`/`scan_size`, not by
the number of returned rows.** After a query, inspect these fields and warn/gate
when they exceed a budget.

**List-endpoint pagination:** uses query params `limit` + `offset`
(e.g. `/api/{org}/streams?offset=1&limit=1`). `page_size`/`page_num` are ignored.
Responses carry `total` for the full count.

**Rate limiting:** no 429 observed during probing at this request volume; retry
with bounded backoff should still be implemented defensively.

## Sample data observed (test env)

- Streams: `bench_group_by` (logs, 10M docs), `test_colorize` (logs, 15 docs).
- `test_colorize` schema: `_timestamp` (Int64), `level` (Utf8), `message` (Utf8), `ts` (Utf8).
- Contains a real error record: `level=error, message="Failed to write to disk: no space left"` — useful as a golden-set fixture.
