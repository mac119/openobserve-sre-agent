# Delivery — Long-Term Memory Phase 1

Delivery summary for the third memory tier (cross-session, per-organization),
implemented per `memory-longterm.md`. Date: 2026-09-04. Status: **shipped &
verified**.

## Scope delivered

Phase 1 = active writes + per-turn injection + forgetting. Phases 2 (passive
LLM extract→consolidate) and 3 (semantic retrieval) remain future work.

## What changed

| File | Change |
|---|---|
| `memory.py` | New `long_term_memory` table (`UNIQUE(org,key)`, indexed by `org, importance, updated_at`); methods `remember_long_term` / `get_long_term` / `forget_long_term` / `purge_long_term`. Encrypted at rest via `MemoryCipher`; values redacted on write; per-org capacity eviction; TTL expiry. |
| `registry.py` | `remember` tool gains `scope=long_term` (with `kind` / `key` / `importance` / `ttl_days`); `attach_memory` now carries `org` + `settings`; org-scoped upsert dispatch. |
| `agent.py` | `_ltm_block()` injects the org's top-N as a separate `[REMEMBERED CONTEXT — unverified]` system block (quarantined from grounding); wired into turn assembly right after the system prompt (non-persisted); expired-sweep on startup. |
| `config.py` + `.env.example` | `O2_LTM_ENABLED` / `O2_LTM_MAX_PER_ORG` / `O2_LTM_INJECT_TOP` / `O2_LTM_DEFAULT_TTL_DAYS`. |
| `ctx_selftest.py` | Long-term memory offline cases (see below). |

## Design guarantees upheld

- **Grounding is never polluted.** Long-term items are injected as an explicitly
  UNVERIFIED / assumption block, never merged into the verified-evidence channel;
  the resolver/validator gates remain the sole source of truth. The model is told
  to re-verify identifiers via read tools.
- **Org isolation.** Every read/write is filtered by `org`; memory learned in one
  org is invisible in another (same fail-closed spirit as `assert_same_org`).
- **Encrypted + redacted.** `value` is encrypted at rest; credentials are redacted
  before persistence, so secrets never enter durable memory.
- **Design for forgetting.** Same-key upsert (preserves `created_at`), per-org
  capacity eviction (lowest importance/oldest), TTL expiry, expired-sweep on
  startup.
- **Opt-in, backward compatible.** `O2_LTM_ENABLED=1` by default but empty stores
  are a no-op; nothing changes for existing single-session behavior.

## Verification

- `ctx_selftest` long-term cases (offline): upsert overwrites value & preserves
  `created_at`; no duplicate key; org isolation; capacity cap eviction; expired
  row excluded from reads + swept by purge; credential value stored redacted;
  forget; injection block carries the "unverified / ASSUMPTIONS" label; disabled →
  no block.
- All four suites pass at runtime: `ctx_selftest`, `sec_selftest`, `ux_selftest`,
  `server_selftest`.
- All modules import cleanly.
- End-to-end: after writing one memory, a fresh Agent's `_ltm_block()` injects it
  for that org, labeled unverified.

## Docs updated

- `memory-longterm.md` / `memory-longterm.zh.md` → status "Phase 1 implemented".
- `tasks.md` → new **Phase 13** section (Phase 1 boxes checked; Phases 2–3 open).
- `README.md` → doc index, "Not yet implemented", Changelog, self-test list.
- `wiki.md` → 6 long-term-memory lessons.

## Known follow-ups

- Phase 2: passive extract→consolidate at end of turn (async / sleep-time).
- Phase 3: semantic retrieval (embeddings) if the memory set grows large.
- Edit/inspect long-term memory over the HTTP API (CLI/tool path first for now).

## Note on linting

A `read_lints` run surfaced many `basedpyright` **strict-mode** style findings
(e.g. `reportUnknownMemberType`, bare `dict`/`list`, `reportUnannotatedClassAttribute`)
spread across the **entire existing codebase**, including code untouched by this
work. The new code matches the surrounding style; these are pre-existing baseline
style warnings, not regressions from Phase 1. Runtime correctness is green across
all four self-test suites. Migrating the project to strict typed annotations is a
separate, worthwhile task if desired.
