# Context Management Design

Design spec for how O2 Assistant keeps its context window healthy over long,
multi-step sessions. Blueprint for the P0–P3 work items derived from `advice.md`.
Implementation follows this doc the way pipeline code follows `architecture.md`.

Chinese version: `context-management.zh.md`.

## 1. Motivation

The agent loop currently reloads the **entire** message history every turn
(`agent.chat` -> `mem.load_messages`) and feeds tool results back with only a
crude byte-truncation (`_summarize` -> `s[:4000]`). Two failure modes follow:

- **Context blow-up / rot.** Long sessions exceed the window; even before that,
  the model's attention to earlier instructions and key facts degrades as the
  transcript grows.
- **Result flooding.** A single log query can return thousands of rows. Dumping
  that JSON into context drowns the signal and burns tokens.

These are the two problems `advice.md` ranks first. Grounding, security, and
evaluation are already strong; context management is the real gap.

## 2. Goals & non-goals

**Goals**

- Bound the tokens sent to the model per turn, regardless of session length.
- Never lose decisions, verified facts, open tasks, or key identifiers when
  compacting — only drop redundant raw tool output.
- Keep full tool results retrievable on demand (offload + reference).
- Stop runaway loops (repeated calls, budget overruns) before they cost money.
- Preserve every existing invariant: grounding gates, read-only transport,
  cross-org guard, redaction, dry-run writes.

**Non-goals**

- Vector / semantic long-term memory (deferred; noted as a future tier).
- A planner/executor rewrite — the fixed alert-investigation workflow already is
  the right pattern and stays as-is.
- Multi-agent / subagent orchestration.

## 3. Architecture overview

Five mechanisms, layered by priority:

- **P0-1 trim + offload** — full tool JSON goes to a `result_store` table; the
  model gets `{summary, sample, result_ref}` instead of the raw payload.
- **P0-2 compaction** — when loaded history exceeds a token threshold, old turns
  are replaced by one structured summary; recent turns stay verbatim.
- **P0-3 memory tiers** — short (messages), mid (task scratchpad), long (future).
- **P1 loop control** — repeat-signature detection, cost/time budget, goal
  re-injection.
- **P2 cost/latency** — parallel tool calls, model routing, prompt caching.

## 4. P0-1 — Tool-result trimming & offload

**Principle.** The model sees a summary + a small sample + a reference, never a
raw multi-thousand-row payload. Full results are offloaded and fetched on demand.

### 4.1 Storage: `result_store` table (sqlite, in `memory.py`)

- `id` TEXT — reference id (`res_<12hex>`)
- `session_id` TEXT — owning session
- `tool` TEXT — tool that produced it
- `full_json` TEXT — complete payload, redacted via `safe_for_model`
- `row_count` INTEGER — rows/hits if a list
- `scan_records` INTEGER — scan cost carried through
- `created_at` INTEGER — epoch seconds

Offloaded content is encrypted at rest when `O2_MEMORY_KEY` is set (reuses
`MemoryCipher`) and purged by `Memory.purge` like every session-scoped table.

### 4.2 Model-visible summary schema

The tool message the model sees (instead of the raw payload) carries: `ok`,
`tool`, `row_count`, `scan_records`, `columns` (keys of the first row),
`sample` (first N rows, N = `O2_RESULT_SAMPLE_ROWS`, default 5), `result_ref`,
`truncated` (bool), and any `warnings`. Non-list results pass through when small;
large ones get the same offload treatment.

### 4.3 New read-only tool: `fetch_result`

`fetch_result(result_ref, offset=0, limit=20)` returns `rows[offset:offset+limit]`
from the offloaded result plus `row_count`. It reads only from `result_store`,
hits no network, and lets the model pull detail when it needs it. `limit` is
clamped to `O2_MAX_ROWS`.

### 4.4 Where trimming happens

In `agent.chat`, after `registry.call(...)`: if the payload exceeds
`O2_RESULT_INLINE_MAX_BYTES` (default 2048) or a list exceeds
`O2_RESULT_SAMPLE_ROWS`, offload and hand the model the summary. Small results
(e.g. a schema lookup) skip offload entirely.

## 5. P0-2 — Compaction

**Trigger.** Estimate the token footprint of loaded history each turn
(`chars / O2_TOKENS_PER_CHAR`, default 4). When it exceeds
`O2_CONTEXT_TOKEN_BUDGET * O2_COMPACT_RATIO` (defaults 128000 x 0.7), compact.

**Strategy.** Keep the system prompt and the last `O2_KEEP_RECENT_TURNS`
(default 3) turns verbatim. Everything older collapses into one structured
summary message.

**Structured summary fields** (fixed schema — free-form summaries lose
precision): `goal`, `verified_facts` (each with its source tool), `assumptions`,
`open_tasks`, `key_ids` (streams / alert names / result_refs / change_ids),
`dropped_outputs` (count of raw tool outputs discarded).

The summary is produced by the LLM with a low-temperature, no-tools call and
persisted so it survives restarts. On failure, compaction degrades to a
deterministic fallback: keep recent turns + drop the oldest raw tool outputs
(never drop user/assistant text). Compaction must never fabricate — it summarizes
only what is already in history.

## 6. P0-3 — Memory tiers

- **Short-term:** the `messages` table (current session transcript) — exists.
- **Mid-term:** a per-session `scratchpad` — a small structured store the agent
  writes intermediate conclusions / open tasks to, and which survives compaction
  (compaction reads it to seed the summary). Backed by sqlite, reusing the
  existing DB + cipher + purge.
- **Long-term:** user preferences / durable environment facts. Deferred; would
  need explicit write + invalidation to avoid accumulating stale facts.

## 7. P1 — Loop control

- **Repeat-signature detection.** Fingerprint each `(tool, canonical_args)`. If
  the same signature repeats `O2_MAX_REPEAT_CALLS` (default 2) times in a turn,
  short-circuit that call with a nudge telling the model to change strategy
  instead of re-running it.
- **Cost / time budget.** `O2_TURN_COST_BUDGET_USD` and `O2_TURN_WALL_CLOCK_S`.
  Checked in the tool loop against the running `session_usage` and a turn start
  clock; on overrun the turn stops gracefully with a partial answer + reason,
  rather than looping. Complements the existing `_MAX_TOOL_ITERS`.
- **Goal re-injection.** On each iteration after the first, prepend a compact
  reminder of the original user goal + open tasks (from the scratchpad) so long
  tool chains don't drift.

## 8. P2 — Cost & latency

- **Parallel tool calls.** When the model returns multiple tool calls in one
  turn, run the read-only ones concurrently (thread pool); keep write-path calls
  serial. Results are reassembled in the model's original call order.
- **Model routing.** Extend the router so `general_help` / simple queries use a
  smaller/cheaper model and `investigation` / `resource_generation` use the
  larger one. Config: `O2_MODEL_SMALL` / `O2_MODEL_LARGE`.
- **Prompt caching.** Put the stable system prompt first (already true) and set
  cache hints on the LLM call where the gateway supports it.

## 9. P3 — Architecture hardening

- **Per-user credential passthrough.** In a multi-user deployment, use the
  requesting user's token instead of a shared service account, eliminating
  privilege amplification (`advice.md` §6).
- **Trajectory-quality evaluation.** Extend the golden set to score not just
  outcome correctness but path quality (redundant steps, unnecessary/dangerous
  tool calls).

## 10. Config keys (new)

`O2_RESULT_SAMPLE_ROWS` (5), `O2_RESULT_INLINE_MAX_BYTES` (2048),
`O2_TOKENS_PER_CHAR` (4), `O2_CONTEXT_TOKEN_BUDGET` (128000),
`O2_COMPACT_RATIO` (0.7), `O2_KEEP_RECENT_TURNS` (3),
`O2_MAX_REPEAT_CALLS` (2), `O2_TURN_COST_BUDGET_USD` (0 = off),
`O2_TURN_WALL_CLOCK_S` (0 = off), `O2_MODEL_SMALL`, `O2_MODEL_LARGE`.

All default to preserving current behavior where a `0`/empty value disables the
control, so the features are opt-in and backward compatible.

## 11. Testing strategy

- **Offline self-tests** (extend `ux_selftest`/new `ctx_selftest`): summary
  schema shape, offload round-trip via `fetch_result`, compaction keeps recent
  turns + structured fields, repeat-signature detection fires, budget gate stops
  the loop, purge cascades `result_store`/`scratchpad`.
- **Golden regressions:** existing 19 cases must stay green; add a
  large-result case asserting the model receives a trimmed summary + ref, and a
  loop case asserting a repeated call is short-circuited.
- **Real read env:** a real search that returns many rows must offload and remain
  fetchable; grounding/security invariants unchanged.

## 12. Rollout order

P0-1 -> P0-2 -> P0-3 -> P1 -> P2 -> P3. Each step updates `tasks.md`
(progress), `README.md` (changelog), and `wiki.md` (gotchas) per project rules.
