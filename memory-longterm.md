# O2 Assistant — Long-Term Memory (design)

Status: **Phase 1 implemented.** This document specifies the third memory tier
(cross-session, per-organization) for O2 Assistant, grounded in the 2025
landscape (Mem0, Letta/MemGPT, Graphiti/Zep, and the "production CLI tools are
surprisingly simple" finding) and adapted to this project's constraints. Phase 1
(active writes + injection + forgetting) is live; Phases 2–3 remain future work.

Companion docs: `context-management.md` (tiers overview), `architecture.md`
(components), `spec.md` §6 (grounding invariant). Self-test target:
`o2agent.ctx_selftest`.

---

## 1. Where this fits

Memory tiers (only the first two exist today):

| Tier | Scope | Store | Status |
|---|---|---|---|
| Short-term — transcript | one session | `messages` (+ compaction) | ✅ |
| Mid-term — scratchpad | one session | `scratchpad` (facts/tasks/key_ids) | ✅ |
| **Long-term — env facts & preferences** | **one org, all sessions** | **`long_term_memory` (new)** | **this doc** |

## 2. Guiding decisions (why this shape)

Drawn from the research, filtered through what an *OpenObserve ops assistant*
actually needs:

1. **Content is a small set of structured facts, not bulk chat.** e.g. "prod
   error logs live in stream `app_prod`", "team watches p99 not avg", "error-rate
   alert convention is 5% over 10m", "user prefers concise Chinese answers". Low
   volume (tens of items/org), low churn, high value.
2. **Therefore: no vector DB, no graph — start simple.** Mirror the production
   CLI tools (Claude Code/Codex/Gemini) that use plain storage + injection, not
   RAG. Use the existing SQLite + `MemoryCipher`; **zero new dependency**.
3. **Injection over retrieval.** Because the set is small, inject the org's
   top-N memories into the system context each turn (the ChatGPT-style "always
   inject" model), rather than a Claude-style retrieval tool. Cheaper and simpler
   at this scale.
4. **LLM-in-the-loop for writes (Mem0-style), agent-initiated (Letta-style).**
   The model explicitly records durable facts via an extended `remember` tool;
   an optional background pass can extract+consolidate later (Phase 2).
5. **Hard project red line — long-term memory must never pollute grounding.**
   `spec.md` §6 says any concrete org/stream/field/alert claim must be backed by
   a real read-tool result. Remembered items are **assumptions, not verified
   facts**: injected under a clearly separate "REMEMBERED (unverified)" heading,
   never merged into the verified-evidence channel. The model must re-verify
   (e.g. `get_schema`) before acting on a remembered identifier.

## 3. Data model

New table (per-organization; NOT per-session):

```sql
CREATE TABLE IF NOT EXISTS long_term_memory (
    id          TEXT PRIMARY KEY,          -- uuid4 hex[:12]
    org         TEXT NOT NULL,             -- isolation boundary (see §6)
    key         TEXT NOT NULL,             -- stable slug, e.g. "prod_error_stream"
    value       TEXT NOT NULL,             -- the fact/preference (encrypted at rest)
    kind        TEXT NOT NULL,             -- fact | preference | env
    source      TEXT,                      -- "user" | "assistant" | "extracted"
    importance  INTEGER DEFAULT 1,         -- 1..5, drives eviction + inject order
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    expires_at  INTEGER,                   -- NULL = no expiry
    UNIQUE(org, key)
);
CREATE INDEX IF NOT EXISTS idx_ltm_org ON long_term_memory(org, importance DESC, updated_at DESC);
```

- `value` is encrypted through the existing `MemoryCipher` (same as other
  columns), so at-rest encryption is inherited.
- `UNIQUE(org, key)` makes writes an **upsert** — the conflict-resolution
  primitive (§5).
- A light nod to Graphiti's bi-temporal idea without the graph: we keep both
  `created_at` (first learned) and `updated_at` (last changed). We do **not**
  version history in v1; an update overwrites `value` (see §5 trade-off).

## 4. Write path

### 4.1 Tool surface (active, Letta-style)

Extend the existing `remember` tool with a `scope`:

```
remember(kind, note, key="", scope="session"|"long_term", importance=1, ttl_days=0)
```

- `scope="session"` (default) → unchanged behavior (writes the session
  scratchpad).
- `scope="long_term"` → upsert into `long_term_memory` for the active org.
  `key` is **required** for long-term (it's the upsert/identity anchor); if the
  model omits it, derive a slug from `note` and return it so the model can reuse
  it.
- `value` is `safe_for_model`-redacted before storage (no credentials ever land
  in durable memory).
- `expires_at = now + ttl_days*86400` when `ttl_days > 0`, else NULL.

`memory.py` gains:

```python
def remember_long_term(self, org, key, value, *, kind="fact",
                       source="assistant", importance=1, ttl_days=0) -> dict
def get_long_term(self, org, limit=None) -> list[dict]   # ordered by importance, updated_at
def forget_long_term(self, org, key) -> bool             # explicit deletion
```

### 4.2 Passive extraction + consolidation (Phase 2, Mem0-style — optional)

At end of turn (or async, borrowing Letta's *sleep-time compute* to keep it off
the user's latency path):

1. **Extract**: prompt the LLM to pull ≤K candidate durable facts from the turn.
2. **Consolidate**: for each candidate, the LLM decides `ADD | UPDATE | NOOP`
   against existing memories for that org (semantic dedup + conflict). This is
   Mem0's extract→update pipeline, kept small.

Phase 2 is deferred; Phase 1 ships with active writes only.

## 5. Forgetting & conflict resolution (design for forgetting first)

- **Conflict**: same `(org, key)` → upsert. New `value` wins, `updated_at`
  refreshed, `created_at` preserved. (Rationale: ops facts are current-state;
  "the prod stream is now X" should replace "was Y". If we later need history,
  add an append-only `long_term_memory_history` table — noted, not built.)
- **Expiry**: rows past `expires_at` are filtered on read and swept by `purge`.
- **Capacity cap**: `O2_LTM_MAX_PER_ORG` (default 100). On insert past the cap,
  evict the lowest `(importance, updated_at)` row. Prevents unbounded growth and
  stale-fact accumulation (the failure mode the research explicitly warns about).
- **Retention**: long-term memory is org-scoped, so the session-based `purge`
  does NOT delete it by default. A separate `purge_long_term(expired_only=True)`
  sweeps expired rows; a full org wipe is an explicit admin action.

## 6. Security & isolation

- **Org isolation.** Every read/write is filtered by `org` (the active
  `settings.org`). No query ever spans orgs — the same fail-closed spirit as
  `security.assert_same_org`. A memory learned in org A is invisible in org B.
- **Encryption at rest.** `value` uses `MemoryCipher` like every other sensitive
  column.
- **Redaction on write.** `safe_for_model` runs before persistence; credentials
  / tokens can never enter durable memory.
- **Injection is quarantined from grounding.** See §7 format. Remembered facts
  are labeled unverified and must be re-grounded before use.
- **No cross-tenant learning.** v1 has no shared/global memory tier.

## 7. Injection format (read path)

At turn assembly (same hook where compaction seeds the scratchpad), inject the
org's memories as a **separate, clearly-labeled** system block:

```
[REMEMBERED CONTEXT — unverified, org=default]
These are durable notes from earlier sessions. Treat them as ASSUMPTIONS, not
verified facts. Re-verify any stream/field/alert via a read tool before relying
on it.
- (env) prod_error_stream: errors are in stream "app_prod"
- (preference) answer_style: user prefers concise Chinese answers
- (fact) alert_convention: error-rate alerts use 5% over 10m
```

Rules:
- Injected block is capped (top-N by importance, e.g. `O2_LTM_INJECT_TOP=20`) to
  bound token cost.
- Placed after the spec/system prompt, before recent turns — consistent with the
  compaction summary placement, so prompt-cache friendliness (P2) is preserved.
- The block is **advisory**; the grounding gates (resolver/validator) are
  unchanged and still the sole source of verified truth.

## 8. Config

| Key | Default | Meaning |
|---|---|---|
| `O2_LTM_ENABLED` | `1` | master switch for long-term memory |
| `O2_LTM_MAX_PER_ORG` | `100` | capacity cap per org (eviction threshold) |
| `O2_LTM_INJECT_TOP` | `20` | how many memories to inject per turn |
| `O2_LTM_DEFAULT_TTL_DAYS` | `0` | default expiry for new items (0 = none) |

## 9. Phasing

- **Phase 1 (MVP, recommended first):** table + `remember(scope=long_term)` +
  per-turn injection + cap/expiry/encryption + `ctx_selftest` cases. Active
  writes only. Zero new dependency.
- **Phase 2 (optional):** passive LLM extract→consolidate, run async
  (sleep-time). Adds a background pass; no new storage.
- **Phase 3 (only if volume grows):** semantic retrieval (embeddings in SQLite
  or a light store) instead of top-N injection; graph/bi-temporal only if a
  strong "state changes over time" need appears.

## 10. Test plan (Phase 1, offline)

Add to `ctx_selftest`:

- upsert semantics: same `(org,key)` overwrites value, preserves `created_at`.
- org isolation: a memory written under org A is not returned for org B.
- capacity cap: inserting past the cap evicts the lowest importance/oldest.
- expiry: an expired row is excluded from `get_long_term` and swept by
  `purge_long_term`.
- redaction: a credential-like `value` is stored redacted.
- injection format: the rendered block carries the "unverified / assumption"
  label (grounding-separation guard).

## 11. Explicitly out of scope (v1)

- Vector / semantic retrieval and knowledge-graph / bi-temporal modeling.
- Shared cross-org or global memory.
- Automatic (unattended) writes without either the model calling `remember` or
  the Phase-2 extraction pass.
- Editing memory through the HTTP API (add later; CLI/tool path first).
