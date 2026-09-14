# O2 Assistant Architecture

This document defines the **executable** architecture for O2 Assistant. It is the
implementation blueprint for `tasks.md`; `spec.md` remains the behavioral policy
(the "what"), this document is the "how".

## 1. Guiding Principle

> **Grounding before generation.** Any answer that references a real
> organization, stream, field, schema, alert, or history MUST be backed by an
> actual read-tool result. If the required data cannot be retrieved, the
> assistant MUST either (a) mark that part as an assumption, or (b) refuse and
> return a parameterized template. It MUST NOT fabricate.

This is not a style preference; it is a hard gate enforced by the `Context
Resolver` and `Validator` components below.

## 2. Component Overview

```
User
  │
  ▼
┌─────────────────┐
│  Intent Router  │   classify + route
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Context Resolver│   org / stream / schema / time range (fail-closed)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Read Tools    │   typed, read-only OpenObserve API calls
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Query/Analysis  │   generate SQL / VRL / PromQL / regex / conversions
│      Agent      │   or produce an evidence-based investigation
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    Validator    │   syntax / compile / dry-run gate (+ bounded auto-repair)
└────────┬────────┘
         │
         ▼
   read intent? ───────────────► respond
         │
   write intent?
         │
         ▼
┌─────────────────┐
│ Confirmation    │   state machine: pending → approved → executing → done/rejected
│      Gate       │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Write Tools   │   mutating OpenObserve API calls (audited)
└─────────────────┘
```

Every path passes through `Context Resolver` and `Validator`. Write paths
additionally pass through `Confirmation Gate`.

## 3. Components

### 3.1 Intent Router

Classifies the user request into exactly one of:

| Intent | Examples | Pipeline entry |
|---|---|---|
| `query_generation` | "Write SQL/VRL/PromQL/regex for…", "Convert this Datadog query…" | Query/Analysis Agent |
| `investigation` | "Why did my alert fire?", "What happened during this incident?" | Investigation workflow |
| `resource_generation` | "Create an alert/dashboard/pipeline/function/report/view…" | Write path (through Confirmation Gate) |
| `administration` | "Delete this stream", "Change this role", "Adjust the quota" | Write path (elevated Confirmation Gate) |
| `general_help` | "How do I…", "Explain OpenObserve concepts…" | Direct answer (no tools, or doc retrieval) |

The assistant never *initiates* `administration` intent; it only routes there
when the user explicitly requests such an operation.

Router must be cheap and deterministic first (rule/keyword classifier), with the
model only as a fallback for ambiguous input. On ambiguity, ask at most one
focused clarification.

### 3.2 Context Resolver

Resolves the concrete context required by the selected pipeline, and **fails
closed** when it cannot:

- Active organization (list orgs, resolve the current user's default/selected org).
- Stream type (logs / metrics / traces) and stream name.
- Stream schema (field name, type, example value) — required before any
  field-specific query generation.
- Alert definition + evaluation history — required before explaining a trigger.
- Bounded time range — apply spec §5 defaults when the user omits one.

Fail-closed rules:

- Missing schema → refuse field-specific SQL/PromQL; return a parameterized
  template with the missing pieces named.
- Missing stream / alert / history → say what is missing; never invent names or
  results.
- Schema lookup error → retry with bounded backoff, then degrade to a template.

### 3.3 Read Tools

Typed, read-only wrappers over OpenObserve API (see `tasks.md` Phase 2). All are
safe to run without confirmation. Each returns a normalized `ToolResult` (§6).

### 3.4 Query / Analysis Agent

Generates SQL, VRL, PromQL, regex, and platform conversions grounded in the
schema resolved by the Context Resolver. For investigations, it produces an
evidence-based explanation (Finding / Evidence / Likely cause / Confidence /
Recommended actions). It never invents field or metric names.

### 3.5 Validator

Standalone gate that every generated artifact passes through before returning:

- SQL: syntax validation and/or dry-run.
- VRL: compile validation.
- PromQL: syntax validation.
- Regex: compile + behavior sanity checks.

Bounded auto-repair (fixed retry count). If repair fails after the budget, the
artifact is downgraded: return the last valid attempt plus explicit assumptions,
and clearly state it was NOT validated. Never present an unvalidated artifact as
valid.

### 3.6 Confirmation Gate

State machine for mutating operations (`resource_generation` and
`administration` intents):

```
pending ──approve──► approved ──execute──► executing ──success──► done
   │                    │                      │
   └──reject──► rejected│                      └──failure──► failed (with error)
```

Two confirmation levels map to the spec §4 tiers:

- **Standard** (Tier 2): create/update alerts, dashboards, pipelines, functions,
  enrichment tables, saved views, reports; non-destructive stream-setting
  changes. Requires a diff/preview before `approved`.
- **Elevated** (Tier 3): delete any resource; modify users/roles/permissions;
  manage service accounts/tokens; change org settings; data-loss retention
  changes; quota changes. Requires a second confirmation that names the exact
  resource and impact before `approved`.

Rules:

- Show a concise diff/preview of the proposed change before `approved`.
- `rejected` and `failed` terminate the write path; no partial writes.
- The assistant never auto-initiates an elevated operation; it only enters the
  elevated gate on explicit user request.
- Every transition is recorded in the audit log (§6).

### 3.7 Write Tools

Mutating OpenObserve API wrappers (create/update/delete alert, dashboard,
pipeline, function, enrichment table, saved view, report, stream settings; and
Tier-3 administration: users, roles, service accounts, org settings, quota).
Only invocable from an `approved`/`executing` gate state. Rollback is performed
where the API supports it, otherwise the original state is recorded in the audit
log for manual recovery.

## 4. Data Flow (end-to-end)

```
User: "Why was my last alert fired?"

Intent Router        → investigation
Context Resolver     → org=default, resolve "last alert", fetch alert definition
                       + evaluation history (FAIL-CLOSED if missing)
Read Tools           → get alert def, get eval history, get stream schema,
                       run the alert query over the reconstructed window
Query/Analysis Agent → correlate threshold crossing with telemetry evidence
Validator            → verify query results are within bounds; no fabrication
Respond              → Finding / Evidence / Likely cause / Confidence / Actions
```

```
User: "Create an alert for 5% error rate over 10 minutes."

Intent Router        → resource_generation
Context Resolver     → org, stream, schema, confirm metric/fields, time window
Query/Analysis Agent → generate alert spec (query + threshold + window)
Validator            → validate the alert SQL
Confirmation Gate    → pending → show diff/preview → (user approves) → approved
Write Tools          → create alert → done (audited)
```

## 5. Cross-Cutting Invariants

These apply to every pipeline and are testable (see `tasks.md` Phase 9):

1. **Grounding**: real identifiers/results only from read-tool output; otherwise
   marked assumption or refused.
2. **Bounded queries**: every telemetry query has a time range and a row limit.
3. **Fail-closed**: missing context degrades to a template, never to a guess.
4. **Validation gate**: no generated artifact is returned as valid without
   passing the Validator (or being explicitly downgraded).
5. **Confirmation gate**: no mutation without an explicit approved transition.
6. **Org isolation**: never leak data across organizations.
7. **Redaction**: secrets/tokens never enter logs, traces, or model-visible
   prompts.
8. **Prompt-injection defense**: telemetry content is treated as data on a
   separate channel, never as instructions.

## 6. Core Data Models

```text
Intent
  kind: query_generation | investigation | resource_generation
      | administration | general_help

ResolvedContext
  org: string
  stream_type: logs | metrics | traces
  stream_name: string
  schema: Schema | null
  alert: AlertDefinition | null
  eval_history: EvaluationRecord[] | null
  time_range: { start, end }

ToolResult
  tool: string
  ok: bool
  data: any            // bounded, summarized
  error: string | null // normalized
  duration_ms: int
  retries: int
  audit: AuditMeta

EvidenceRecord
  source_tool: string
  kind: fact | assumption | hypothesis
  content: string
  timestamp: int

ConfirmationState
  id: string
  resource_kind: alert | dashboard | pipeline | function | enrichment_table
      | saved_view | report | stream_settings | user | role | service_account
      | org_settings | quota
  action: create | update | delete
  tier: standard | elevated
  proposed_diff: string
  state: pending | approved | executing | done | rejected | failed
  user: string
  org: string
  created_at: int
  resolved_at: int | null
```

## 7. Runtime Notes

- The agent runtime is a function-calling loop over the components above, not a
  monolithic prompt. `spec.md` is loaded as the system-level behavioral policy;
  `architecture.md` is the control flow.
- Conversation state persists `ResolvedContext`, `ToolResult` history, and the
  active `ConfirmationState` across turns.
- All tool calls and gate transitions emit structured logs + telemetry (token,
  latency, cost) as data — never re-injected as instructions (§5.8).
