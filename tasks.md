# O2 Assistant Implementation Tasks

Implementation blueprint lives in `architecture.md`; behavioral policy in
`spec.md`. Phases map 1:1 to the pipeline components.

## Phase 0: OpenObserve API Surface Research

Result recorded in `api-surface.md` (probed against v0.91.0-rc1 test env).

- [x] Enumerate read endpoints (orgs, streams, schema, stream settings, sample records, search/SQL, PromQL, alerts, alert evaluation history, incidents, dashboards, pipelines, functions, enrichment tables, saved views, search history, reports, RUM, users/roles/service accounts, quota/usage, traces).
- [ ] Enumerate write endpoints (alerts, dashboards, pipelines, functions, enrichment tables, saved views, reports, stream settings) and confirm exact request/response payloads.
- [x] Enumerate administrative/destructive endpoints (delete resources, users/roles/permissions, service accounts/tokens, org settings, quota). Roles/groups exist but return 403 for the test service account.
- [x] Confirm the auth model — Basic auth with a service account token; header `Authorization: Basic <base64>`. Ingestion token (`o2oi_...`) is NOT an API credential (returns 401).
- [x] Confirm pagination, rate limits, and result-size limits per endpoint. Search uses body `from`+`size` (size NOT capped server-side — agent must enforce a limit); lists use query `limit`+`offset`; scan cost is exposed via `scan_records`/`scan_size`; no 429 observed.
- [x] Verify alert evaluation history endpoint — **exists** at `GET /api/v2/{org}/alerts/history`; "Why did my last alert fire?" can be answered factually.
- [x] Produce an endpoint → tool mapping (see `api-surface.md`). Findings: usage → use `/api/{org}/summary`; **no dedicated incidents endpoint** (404); RUM token at `/api/{org}/rumtoken`.

Key path corrections found during probing:

- Alerts use the **v2** path: `/api/v2/{org}/alerts` (v1 returns 404).
- Alert history: `/api/v2/{org}/alerts/history`.
- Enrichment tables: `/api/{org}/streams?type=enrichment_tables` (no dedicated `/enrichment_tables`).
- Version/build: use `/config` (`/api/version` returns 401).

## Phase 1: Runtime & Intent Router

Implemented in the `o2agent/` package (see README).

- [x] Define the agent runtime (function-calling loop per `architecture.md`) and supported model providers. `agent.py` + pluggable `llm.py` (`LLMProvider` / `OpenAICompatProvider` / `build()`).
- [x] Load `spec.md` as the system-level behavioral policy. `agent._system_prompt()`.
- [x] Implement conversation state and tool-result history. `memory.py` (SQLite: sessions/messages/tool_results), cross-process persistence verified.
- [x] Implement the Intent Router: classify into `query_generation` / `investigation` / `resource_generation` / `administration` / `general_help`. `router.py` (rule-first).
- [x] Route each intent to its pipeline entry point (`administration` never auto-initiated). Intent injected as a hint; admin routing reserved for the gated write path (Phase 7).
- [x] Add structured logging for agent requests and tool calls. `telemetry.py` emits redacted JSON-line events (request_start/llm_call/tool_call/request_end).
- [x] Add token, latency, error, and cost telemetry. Per-call token usage + USD cost recorded to `llm_usage` table; per-session totals surfaced in CLI.
- [x] Prevent telemetry records from being interpreted as agent instructions. Tool results fenced as DATA + system-prompt directive.

## Phase 2: OpenObserve Read Tools

Implement typed read-only tools (`tools.py`, exposed via `registry.py`):

- [x] List organizations
- [x] List streams by stream type
- [x] Get stream schema
- [ ] Read stream settings (retention, partitioning, full-text index) — schema call returns settings; dedicated tool TODO
- [x] Retrieve sample records (via bounded `search`)
- [x] Execute bounded SQL (`search`, size clamped to `O2_MAX_ROWS`)
- [ ] Execute PromQL — endpoint verified; tool TODO
- [x] Read alert definitions (`/api/v2/{org}/alerts`)
- [x] Read alert evaluation history (`/api/v2/{org}/alerts/history`)
- [x] Read org summary / usage (`/api/{org}/summary`) — no dedicated incidents endpoint in this version
- [x] Inspect dashboards and panels (list)
- [x] Inspect pipelines and transformations (list)
- [x] Inspect functions (reusable VRL functions) (list)
- [x] Inspect enrichment tables (via streams endpoint)
- [x] List saved views and search history
- [x] List and inspect reports
- [ ] Read RUM data — `rumtoken` verified; RUM data tool TODO
- [ ] List users, roles, and service accounts (read-only) — users/service_accounts verified; roles/groups return 403 for the test account
- [x] Read quota and usage (served by `summary`)
- [ ] Read trace details and correlated logs — TODO

Each tool defines: input schema (pydantic), normalized output (`ToolResult`),
timeout, retry/backoff, max result size, error normalization, and scan-cost
metadata. Pagination: search uses body `from`+`size`, lists use `limit`+`offset`.

## Phase 3: Context Resolver

Implemented in `resolver.py`.

- [x] Resolve the active organization. (from settings / `list_organizations`)
- [x] Resolve stream type and stream name. (`resolve_stream`, fails closed if not found)
- [x] Retrieve schema before generating any field-specific query. (`resolve_schema`)
- [ ] Retrieve alert configuration and evaluation history before explaining a trigger. (Phase 6)
- [x] Fail-closed: on missing schema/stream, raise `ResolveError` instead of fabricating.
- [x] Cache schemas briefly (TTL) and expose `invalidate()`.
- [x] Apply bounded default time ranges when the user omits one (`default_range_us`, per spec §5).

## Phase 4: Query / Analysis Agent

Generation happens in the grounded agent loop; artifacts pass a validate-and-repair
gate (`generation.py`) before being shown.

- [x] Create OpenObserve SQL generation workflow. (agent loop + validator-gated `search`)
- [x] Create VRL generation workflow. (agent loop + `validate_query` tool, real dry-run)
- [x] Create PromQL generation workflow. (agent loop + `validate_query` tool)
- [x] Create regex generation and validation workflow. (`RegexValidator`, local compile)
- [ ] Create Datadog-to-OpenObserve conversion workflow (also ES/KQL, Splunk, Loki, Prometheus). (handled by the model in-loop; dedicated conversion workflow TODO)
- [ ] Generate resource specifications (dashboard/alert/pipeline/report specs) as previewable artifacts before any write. (Phase 7)
- [x] Enforce schema-grounding: never invent field or metric names. (SQLValidator gate)

## Phase 5: Validator (standalone gate)

Implemented in `validator.py` + `generation.py`. SQL is gated pre-execution in
`registry._gated_search`; VRL/PromQL/regex are gated via the `validate_query`
tool with auto-repair.

- [x] SQL validation: stream + field existence against real schema; reject unknown; bounded range + `SELECT *` checks.
- [x] VRL compile validation. (real dry-run via `POST /api/{org}/functions/test`)
- [x] PromQL syntax validation. (via `GET .../prometheus/api/v1/format_query`)
- [x] Regex validation. (Python `re.compile`)
- [x] Automatically repair syntax errors with a bounded retry count. (`GenerationWorkflow`, feeds validator error back to the model, max_repairs=2)
- [x] On repeated failure, downgrade to unvalidated + explicit error; never present an unvalidated artifact as valid.

## Phase 6: Investigation Workflows

### Alert investigation

Implemented in `investigation.py` (`AlertInvestigator`), exposed via the
`investigate_alert` tool. Fail-closed verified against the real (empty) env.

- [x] Resolve "last alert" using organization context. (`_pick_last_alert`)
- [x] Fetch alert definition and evaluation history. (v2 endpoints)
- [x] Reconstruct the evaluation window. (`_reconstruct_window` from trigger time + period)
- [x] Run the alert query for that window. (`_collect_evidence`)
- [ ] Identify matching groups and threshold crossings. (needs a fired alert to exercise; logic present, unverified)
- [ ] Correlate nearby logs, metrics, and traces. (evidence query in place; multi-signal correlation TODO)
- [x] Generate an evidence-based explanation. (structured Outcome; agent renders Finding/Evidence/Actions)
- [x] Display confidence and missing evidence. (fail-closed: `no_alerts`/`no_history` → reports "cannot determine", never guesses)

### Incident investigation

> Note: v0.91.0-rc1 has no dedicated incidents endpoint. Reframe "incident" as a
> correlation over alerts + alert history + telemetry, or mark unsupported until
> a version with incidents is targeted.

- [ ] Reconstruct an incident view from linked alerts and alert history.
- [ ] Identify affected services and dimensions.
- [ ] Compare the incident period to a baseline.
- [ ] Locate correlated errors, latency, and resource saturation.
- [ ] Generate ranked hypotheses.
- [ ] Recommend verification steps rather than asserting causation.

## Phase 7: Confirmation Gate & Write Tools

Implemented in `gate.py` (state machine), `write_client.py` (isolated mutating
client, dry-run by default), `write_tools.py` (change builders). The model may
only PROPOSE (`propose_change` tool); approval/execution are external actions.

- [x] Implement the confirmation state machine: `pending → approved → executing → done/rejected/failed`.
- [x] Implement two confirmation levels: standard (Tier 2) and elevated (Tier 3).
- [x] Show a preview or diff before mutation. (`PendingChange.diff`; surfaced to the user)
- [x] Implement Tier-2 write tools: create/update dashboards, alerts (v2), functions. (pipelines/enrichment/views/reports: same pattern, TODO)
- [x] Implement Tier-3 write tools (explicit-request only): delete resources (dashboard/alert/function/stream). (users/roles/service-accounts/org/quota: same gate, tools TODO)
- [x] Require elevated confirmation naming the exact resource and impact for Tier-3 actions.
- [x] Ensure the assistant never auto-initiates Tier-3 operations. (model can only propose; gate never auto-approves/executes)
- [x] Record user, organization, request, and result in an audit log. (telemetry: change_proposed/approved/rejected/result, write_dry_run/executed)
- [ ] Implement rollback where APIs support it. (TODO)

> Safety default: `WriteClient(dry_run=True)`. Real writes require explicit
> `dry_run=False`; until then `/run` simulates the request and audits it without
> touching the environment.

## Phase 8: Security & Injection Defense

Implemented in `security.py` (pure helpers) wired into the client/registry/agent
paths, plus `crypto.py` (opt-in at-rest memory encryption). Self-test:
`python -m o2agent.sec_selftest` (28 checks, no network/LLM needed).

- [x] Enforce the current user's OpenObserve permissions. (permission failures normalized via `normalize_permission_error`; the service account's own grants remain the source of truth — 403s are surfaced clearly, never worked around.)
- [x] Do not expose cross-organization data. (`assert_same_org` fail-closes in both `ReadOnlyClient._request` and `WriteClient.execute`; a path targeting another org raises `SecurityError`.)
- [x] Redact passwords, API keys, tokens, and authorization headers from logs, traces, and model-visible prompts. (`telemetry.redact` on the log channel; `safe_for_model` applies the same redaction to every tool result before it reaches the model.)
- [x] Defend against prompt injection: treat telemetry content as data on a separate channel, never as instructions. (`fence_tool_result` wraps tool output in explicit UNTRUSTED-DATA delimiters + a directive; system prompt reinforces it.)
- [x] Restrict network access to approved OpenObserve endpoints. (`check_endpoint` + `O2_ENDPOINT_ALLOWLIST`; both clients refuse a non-allowlisted host at construction.)
- [x] Add query timeout, row limit, and scan-budget controls. (timeout `O2_HTTP_TIMEOUT`; rows clamped to `O2_MAX_ROWS`; `over_scan_budget` flags over-budget searches with a warning + `scan_budget_exceeded` meta in `registry._gated_search`.)
- [x] Encrypt stored conversation and credential data. (`crypto.py` `MemoryCipher`: opt-in field-level Fernet encryption of message content / tool summaries / context, keyed by `O2_MEMORY_KEY` via PBKDF2. Backward compatible — no key = plaintext, legacy rows still read; wrong key fails closed. Credentials themselves never touch the DB — they live only in env/.env.)
- [x] Define data-retention and deletion policies. (`Memory.purge(retention_days)` deletes sessions + messages/tool_results/llm_usage older than the window; run on agent startup per `O2_RETENTION_DAYS`.)

## Phase 9: Evaluation

Golden set implemented in `golden.py` — 19 cases with pass/fail + grounding and
fail-closed assertions. Grouped by resource needs: `offline` (no net/LLM),
`readonly` (real read-only env), `llm` (generation quality). Bias toward
code-enforced invariants so most cases run without an LLM. Verified: **19/19
passed** (offline 7 + readonly 9 + llm 3), grounding 6/6, fail-closed 8/8.

Run: `python -m o2agent.golden` (offline+readonly) / `--no-net` / `--llm`.

- [x] Nginx JSON parsing with VRL (`nginx_json_vrl`, real dry-run compile)
- [x] Malformed JSON handling (`malformed_json_vrl`, fallible parse, no abort)
- [x] Email redaction (`email_redaction`, regex matches uppercase/subdomain/punct)
- [x] Stream schema mapping (`schema_mapping`, real `bench_group_by` schema)
- [x] Kubernetes CPU PromQL (`k8s_cpu_promql`, real `format_query` validation)
- [x] CPU usage versus request and limit semantics (`cpu_request_vs_limit_semantics`, llm)
- [x] Datadog query conversion (`datadog_query_conversion`, llm; SQL + assumptions)
- [x] Alert trigger explanation (`alert_trigger_explanation`, fail-closed `no_alerts` — cites config/history, never guesses)
- [x] Missing schema → refuse field-specific query (`missing_schema_refuse`, validator rejects unknown field pre-execution)
- [x] Missing stream → refuse or template (`missing_stream_refuse`, resolver fail-closed)
- [x] SQL syntax error repair (`sql_syntax_error_repair`, llm; repaired-or-downgraded, never faked)
- [x] Empty query result (`empty_query_result`, impossible match returns [], not a fabricated row)
- [x] High-cardinality query protection (`high_cardinality_protection`, unbounded `SELECT *` warns; scan budget enforced in `_gated_search`)
- [x] Prompt injection inside a log message (`prompt_injection_in_log`, redacted + fenced as untrusted DATA)
- [x] Cross-organization access denial (`cross_org_denial`, `assert_same_org` blocks other org)
- [x] Confirmation required before resource mutation (Tier 2) (`tier2_confirmation`, pending + refuses execution)
- [x] Elevated confirmation required before delete / role / quota change (Tier 3) (`tier3_elevated_confirmation`, must name resource)
- [x] Assistant does not auto-initiate a Tier-3 operation (`no_auto_tier3`, no auto-approve/execute path)
- [x] Reusable function / report / saved-view generation (`reusable_function_generation`, gated proposal + diff)

## Phase 10: User Experience

Implemented in the CLI (`cli.py`) with presentation helpers in `ux.py` and
turn-tracking support in `agent.py` (`TurnContext`) + feedback storage in
`memory.py`. Self-test: `python -m o2agent.ux_selftest` (offline).

- [x] Add suggested prompts. (`ux.SUGGESTED_PROMPTS` grouped by intent; shown on startup and via `/suggest`.)
- [x] Stream intermediate progress for longer investigations. (`agent.chat(on_event=…)` fires `tool_start`/`tool_end`; CLI prints `… tool` / `↳ tool ok (Nms, scanned=…)` lines.)
- [x] Allow generated queries to be copied into the query editor. (agent records `TurnContext.artifacts`; `/copy [n]` prints the raw SQL/VRL/PromQL/regex block.)
- [x] Allow users to edit queries before execution. (`/sql <query>` runs a user-edited query through the validator gate with a bounded default range.)
- [x] Show organization, stream, and time range used. (`ux.render_context_footer`; printed after every answer and via `/context`.)
- [x] Display assumptions separately from verified evidence. (spec §7 format loaded as system prompt + reinforced runtime directive to separate FACTS/ASSUMPTIONS and state org/stream/range.)
- [x] Provide a "Create alert/dashboard/pipeline" confirmation flow. (`propose_change` tool → `/changes` (now shows diffs) → `/approve` → `/run`; dry-run by default.)
- [x] Add thumbs-up/down and correction feedback. (`/feedback up|down [note]` → `memory.feedback` table with the rated answer excerpt.)

## Phase 11: Context Management (design: `context-management.md`)

Keeping the context window healthy over long sessions. Rollout P0→P3.
Self-test: `python -m o2agent.ctx_selftest` (offline).

### P0-1 — Tool-result trimming & offload  ✅

- [x] `result_store` table + `store_result`/`fetch_result` in `memory.py` (encrypted at rest, purged on retention).
- [x] `_trim_or_offload` in `agent.py`: large/list results are offloaded; the model gets `{row_count, scan_records, columns, sample, result_ref, truncated}`. Small results still inline.
- [x] `fetch_result` read-only tool (registry `attach_memory`) to page offloaded results on demand; clamps `limit` to `O2_MAX_ROWS`.
- [x] Config `O2_RESULT_SAMPLE_ROWS` / `O2_RESULT_INLINE_MAX_BYTES`. Verified end-to-end: a 50-row query is offloaded and the model auto-calls `fetch_result`.

### P0-2 — Compaction  ✅

- [x] `compaction.py` `Compactor`: token estimate + `should_compact` (budget × ratio); keeps system prompt + last `O2_KEEP_RECENT_TURNS` turns verbatim, replaces older turns with one structured summary (`goal`/`verified_facts`/`assumptions`/`open_tasks`/`key_ids`/`dropped_outputs`).
- [x] LLM summary (low-temp, no-tools) with a deterministic fallback that keeps user/assistant text and only drops raw tool outputs; never fabricates.
- [x] Wired into `agent.chat` after loading history; emits `context_compacted` telemetry. Config `O2_TOKENS_PER_CHAR` / `O2_CONTEXT_TOKEN_BUDGET` / `O2_COMPACT_RATIO` / `O2_KEEP_RECENT_TURNS`. Verified via `ctx_selftest` (both LLM + fallback paths).

### P0-3 — Memory tiers (scratchpad)  ✅

- [x] `scratchpad` table + `remember`/`get_scratchpad` in `memory.py` (encrypted, purged on retention).
- [x] `remember` read-only tool (kind = fact|task|key_id) so the model persists durable notes that survive compaction.
- [x] Compaction seeds its summary from the scratchpad (`_merge_seed`), so persisted facts/tasks/key_ids are never lost even if the LLM summary misses them. Verified via `ctx_selftest`.

### P1 — Loop control (repeat detection / budget / goal re-injection)  ✅

- [x] Repeat-signature detection: `(tool, canonical_args)` fingerprints in `agent.chat`; a call repeated past `O2_MAX_REPEAT_CALLS` is short-circuited with a "change strategy" nudge (`repeat_blocked`, `repeat_call_blocked` telemetry).
- [x] Cost/time budget gate: `over_turn_budget` checks per-turn spend vs `O2_TURN_COST_BUDGET_USD` and elapsed vs `O2_TURN_WALL_CLOCK_S`; on overrun the turn stops gracefully. Verified end-to-end (tiny budget → early stop).
- [x] Goal re-injection: `_goal_reminder` prepends a compact goal + open-tasks note (from the scratchpad) on every iteration after the first, non-persisted. Pure helpers unit-tested in `ctx_selftest`.

### P2 — Cost & latency (parallel tools / model routing / prompt cache)  ✅

- [x] Parallel tool calls: `_execute_planned` runs read-safe tools concurrently (thread pool, `O2_MAX_PARALLEL`) while mutating tools (`propose_change`/`remember`) stay serial; order preserved for the model. Verified 4× concurrent vs serial timing.
- [x] Model routing: `pick_model` sends `general_help`/simple queries to `O2_MODEL_SMALL` and investigation/resource/admin to `O2_MODEL_LARGE`; falls back to the single `LLM_MODEL` when unset (`llm.chat(model=…)` per-call override).
- [x] Prompt caching: the stable system prompt is placed first (cache-friendly); explicit cache hints are gateway-specific and left as automatic. Config `O2_PARALLEL_TOOLS` / `O2_MAX_PARALLEL` / `O2_MODEL_SMALL` / `O2_MODEL_LARGE`.

### P3 — Hardening (per-user creds / trajectory-quality eval)  ✅

- [x] Per-user credential passthrough: `ReadOnlyClient`/`WriteClient` accept an `auth` override and `Agent(actor_auth=…)` threads it through, so a multi-user host uses the requesting user's token (real permissions) instead of the shared service account. Defaults to the service account. Verified in `sec_selftest`.
- [x] Trajectory-quality evaluation: `golden.score_trajectory` scores path quality (steps / redundant / failed / dangerous / clean); two offline golden cases assert a clean path scores clean and a wasteful (repeated-call) path is flagged. golden set now 21 cases.

**Phase 11 (context management) complete: P0–P3 all done.** Self-tests:
`sec_selftest` / `ctx_selftest` / `ux_selftest` / `golden`.

## Phase 12: HTTP/SSE API surface (design: `api-server.md`)

A standard HTTP + SSE API over the shared `Agent`, so an external front-end (the
O2 Assistant panel embedded in OpenObserve) can drive it. The CLI is retained as a
peer sub-command. Stdlib `http.server` — no new dependency. Self-test:
`python -m o2agent.server_selftest` (offline; boots on an ephemeral port).

- [x] `server.py`: `O2Service` (transport-agnostic logic) + threaded HTTP handler; endpoints per `api-server.md` (health / suggestions / sessions / messages / chat / chat/stream / sql / changes(approve|reject|run) / feedback).
- [x] SSE streaming for `chat/stream` (maps the Phase 10 `on_event` callback to `progress` + `final` frames; closes connection cleanly after the turn).
- [x] Auth (`O2_API_TOKEN` bearer guard; health always open) + CORS (`O2_CORS_ORIGIN`, preflight handled).
- [x] Confirmation flow over HTTP: propose (via chat) → `/changes` → approve/reject/run; write path still dry-run + gated.
- [x] Thread-safe memory: per-thread SQLite connections (ThreadingHTTPServer workers) + `busy_timeout`.
- [x] `__main__.py` dispatcher: `python -m o2agent {chat|serve|smoke|golden}`; CLI retained. Direct module entry points still work.
- [ ] Per-request per-user OpenObserve credentials (plumbed at Agent level; needs an Agent pool — deferred).
- [ ] Front-end integration into the OpenObserve UI is external (the API is the contract).

## Phase 13: Long-Term Memory (design: `memory-longterm.md`)

Cross-session, per-org memory tier. Phase 1 implemented; Phases 2–3 future.
Self-test: `python -m o2agent.ctx_selftest`.

- [x] `long_term_memory` table (per-org, `UNIQUE(org,key)`) + `remember_long_term` / `get_long_term` / `forget_long_term` / `purge_long_term` in `memory.py` (encrypted, redacted on write).
- [x] `remember` tool gains `scope=long_term` (kind/key/importance/ttl_days); org-scoped upsert.
- [x] Per-turn injection of the org's top-N as a separate **UNVERIFIED / assumption** system block (`agent._ltm_block`), quarantined from grounding.
- [x] Forgetting: same-key upsert (preserves `created_at`), per-org capacity eviction, TTL expiry, expired-sweep on startup.
- [x] Config `O2_LTM_ENABLED` / `O2_LTM_MAX_PER_ORG` / `O2_LTM_INJECT_TOP` / `O2_LTM_DEFAULT_TTL_DAYS`; org isolation verified.
- [ ] Phase 2: passive LLM extract→consolidate at end of turn (async / sleep-time).
- [ ] Phase 3: semantic retrieval (embeddings) instead of top-N injection, if volume grows.
- [ ] Edit/inspect long-term memory over the HTTP API (CLI/tool path first).

## Phase 14: Pluggable Storage Backend (design: `storage-backend.md`)

Decouple persistence so the embedded SQLite store and an optional PostgreSQL
store can coexist, selected by config, without changing callers. Phase 1 done
(pure refactor); Phases 2–3 future. Self-test: `python -m o2agent.storage_selftest`.

- [x] `storage.py`: `StorageBackend` interface (`init_schema` / `execute` / `query_one` / `query_all` / `close` / `placeholder`) + `SqliteBackend` (per-thread connections, dict rows, DDL owned by backend) + `build_storage()` factory.
- [x] `memory.py` delegates all raw SQL to the backend; keeps encryption / redaction / retention orchestration. All 19 public methods unchanged; behavior identical (verified by the full existing suite).
- [x] `config.py` + `.env.example`: `O2_DB_URL` (empty = SQLite) / `O2_DB_PATH` / `O2_DB_POOL_MAX`; `Agent` builds the backend via `build_storage` (Phase 2 needs no Agent change).
- [x] `storage_selftest`: backend basics (rowcount/dict rows), full Memory contract round-trip, concurrent multi-thread access (no thread-affinity crash).
- [ ] Phase 2: `PostgresBackend` (psycopg + pool + rollback-on-error) behind `O2_DB_URL`; gated PG self-tests; `deploy.md` shared-Postgres topology.
- [ ] Phase 3: `migrate_store` script (SQLite → Postgres; ciphertext copied verbatim).
