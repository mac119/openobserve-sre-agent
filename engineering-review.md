# O2 Assistant — Engineering Review

This is a retrospective on how O2 Assistant is actually built — the design calls
we made, and the concrete problem each one buys us. Everything here maps to real
code across Phases 0–10, not to a wish list.

The thesis, up front:

> The interesting part of this project isn't that we wired up an LLM. It's that
> we took two fuzzy, aspirational rules — *"be safe"* and *"don't make things
> up"* — and turned them into hard constraints the model physically cannot
> route around.

Everything below is a variation on that theme.

---

## 1. Grounding is enforced by code, not by asking the model nicely

**The problem.** LLMs will invent stream names, field names, and alert history
without blinking, and they'll do it *confidently*. A line in the system prompt
that says "always check the schema first" is theater. A sufficiently sure-of-
itself model will still hand you SQL with a field that doesn't exist.

**What we did instead.** We pushed grounding down into two gates that run
*before* anything executes, so the model doesn't get a vote:

- `resolver.py` fails closed. If it can't fetch the stream or schema, it raises
  `ResolveError` — it does not return a best guess. An empty schema is rejected
  too, with a blunt message: *"refusing to generate field-specific queries
  without a known schema."*
- `validator.py`, wired into `registry._gated_search`, parses the SQL before we
  ever hit the wire. It pulls the stream out of the `FROM`, checks every
  referenced field against the real schema, and rejects unknown fields — handing
  back the actual field list so the caller (or the model) can correct itself.

You can see it work: `/sql SELECT bogus FROM "bench_group_by"` gets rejected with
all eight real fields listed. The golden cases `missing_schema_refuse` and
`missing_stream_refuse` pin this behavior down so it can't regress.

**The part I'm proud of:** the "let the user hand-edit a query" feature (`/sql`)
runs through the *exact same* gate. There's no back door. Editing a query before
you run it can't buy you a way around grounding, because there's only one door.

**A detail that's easy to get wrong:** `validator._referenced_fields` strips
single-quoted string literals *before* it extracts identifiers. Without that,
`WHERE status = 'error'` would treat `error` as a column name. Regex-based SQL
validation trips on this constantly; we handle it on purpose.

---

## 2. Security is layered, and every layer fails closed

**The problem.** For a read-only monitoring agent, the nightmares are short and
specific: an accidental write, a cross-tenant read, or a credential (or an
injected instruction) leaking through.

The theme here is **physical isolation and transport-layer enforcement** — not
"we'll be careful."

1. **The read and write clients are different objects.** `ReadOnlyClient`
   refuses any non-GET at the transport layer, allow-listing only the two
   side-effect-free POSTs it actually needs (`_search` and `functions/test`).
   Writes live in a completely separate `WriteClient`. A read path can't mutate
   data because it literally doesn't hold an object that can. That's a much
   stronger guarantee than a code review catching it.

2. **The cross-org check lives in the transport layer.** `assert_same_org` runs
   inside both `ReadOnlyClient._request` and `WriteClient.execute`. It parses the
   org segment out of `/api/{org}` and `/api/v2/{org}` and throws `SecurityError`
   on a mismatch. Putting it there means *no tool — present or future — can wander
   into another org*, whether someone remembers to add the check or not. Global
   paths like `/api/organizations` and `/config` are exempted deliberately.

3. **Redaction runs on two channels, not one.** `telemetry.redact` scrubs the
   logs; `safe_for_model` reuses the same patterns to scrub anything the model
   sees. This closes a hole that's easy to miss: you can lock down your log file
   and still leak a credential straight into the model's context by feeding it a
   raw tool result.

4. **Injection defense is a real boundary, not a sentence.** `fence_tool_result`
   wraps every tool payload in explicit `<<<O2_TOOL_DATA>>> … <<<END>>>` markers
   plus a directive to ignore anything inside them. A structural fence beats a
   polite reminder in the system prompt every time.

5. **Writes are dry-run by default and gated by a propose-only state machine.**
   `gate.py` runs `pending → approved → executing → done`, and `execute()`
   refuses anything that isn't already `approved` — so there is no path where the
   agent quietly executes something. Tier-3 (destructive) actions require an
   elevated confirmation that *names the resource*; a generic "yes" bounces. And
   we validated the whole state machine against the live environment with
   `dry_run=True`, which means we exercised it end-to-end **without ever creating
   a real resource.**

6. **At-rest encryption, with a sense of proportion.** `crypto.py` is opt-in — no
   key means plaintext, and it stays backward compatible. The `enc:v1:` prefix
   lets old and new rows coexist so a database upgrades in place. A wrong key
   fails closed (it raises) instead of quietly returning ciphertext. And we were
   honest about the threat model: credentials never touch the DB (they live in
   the environment), so the encryption is there to protect conversation content —
   not crypto for its own sake.

---

## 3. Pluggable where it counts, single-responsibility everywhere

**The problem.** The things that change will change: the LLM gateway
(LiteLLM today, who-knows tomorrow), the set of validators, the shape of the UI.

**The approach.** Each layer does one job and talks to interfaces:

- `llm.py` — the agent only ever knows about the abstract `LLMProvider`;
  `build()` picks the concrete one from config. Switching vendors is one new
  class and zero changes to the agent loop. Responses normalize into
  `LLMResponse`, with the OpenAI message/tool shape as the canonical internal
  format.
- The `ToolResult` envelope threads through the entire stack (carrying
  `scan_records`, `scan_size`, `meta`), so nothing upstream has to care whether a
  call went out as a GET or a `_search` POST.
- `ux.py` holds all the presentation strings; `agent.py` stays UI-agnostic. When
  Phase 10 bolted on the UX, `chat` gained one *optional* `on_event` callback and
  still returns a plain `str`. The only caller — the CLI — didn't break. That's
  what a backward-compatible extension is supposed to look like.
- The router is rule-first (`router.py`, plain regex, bilingual EN/ZH) and
  deliberately orders "administration/destructive" ahead of everything else, so
  the dangerous intents get caught cheaply and deterministically. An LLM fallback
  is left as a clean seam for later.

---

## 4. The real-world problems Phase 0 paid for

The most pragmatic thing we did was refuse to trust the docs and probe the API
instead. `wiki.md` is a running log of gotchas that only showed up when we poked
the actual v0.91.0-rc1 environment — and each one changed the implementation:

| What we assumed | What was actually true | How the code responds |
|---|---|---|
| Alerts live at `/api/{org}/alerts` | v1 is a 404; alerts moved to **v2** | `tools.py` uses `api/v2` |
| There's a usage / incidents endpoint | `/usage` and `/incidents` both 404 | use `/summary`; mark incidents "unsupported in this version" instead of faking it |
| Enrichment tables have their own path | they don't | go through `streams?type=enrichment_tables` |
| The ingestion token can browse the API | it 401s everywhere | config keeps it separate; the API uses the service account only |
| **`size` is capped server-side** | `size: 100000` really returns 100k rows | `ReadOnlyClient.search` clamps to `O2_MAX_ROWS` on the client |
| **`scan_records` ≈ rows returned** | a 2-row result can scan 10M records | scan protection keys off `scan_records`; `over_scan_budget` judges it independently |

That last row forced an honest call. Scan cost only comes back *after* the query
runs, so a scan budget can **warn, but it can't pre-empt.** Rather than pretend
otherwise, `_gated_search` sets a `scan_budget_exceeded` flag and appends a
warning after the fact. Owning the limitation beats over-promising.

A few more implementation-level wins:

- **We don't ship our own VRL/PromQL parser.** We call the platform's real
  dry-run endpoints (`functions/test`, `format_query`) and let it be the source
  of truth. Using the fallible form — `parsed, err = parse_json()` rather than
  `parse_json!` — is what lets "malformed input doesn't blow up the pipeline"
  actually hold.
- **Generate → validate → repair** (`generation.py`): a bad artifact gets the
  concrete validator error fed back to the model for a bounded number of retries.
  If it still won't validate, we **downgrade it to `unvalidated` and surface the
  error** — we never dress it up as valid. The golden set shows broken SQL coming
  out the other side as `repaired`.
- **Alert investigation fails closed** (`investigation.py`). No alerts or no
  history means `NO_ALERTS` / `NO_HISTORY` and a plain "can't determine this";
  `is_grounded()` is only true when there's real evidence. "Why did my alert
  fire?" is precisely where a model wants to guess, and a structured `Outcome`
  shuts that down at the source.

---

## 5. Testability and operability

**The problem.** How do you keep the security and grounding invariants from
quietly rotting as the code changes?

**The answer** is a three-tier test design, most of which runs deterministically
with no LLM and no network:

- `golden.py` sorts its 19 cases into `offline / readonly / llm` and **skips —
  rather than fails —** a case whose resources aren't available. So `--no-net`
  gives CI a clean offline signal, and the exit code only goes non-zero on a real
  failure.
- Every case is tagged `[G]` (grounding) and/or `[C]` (fail-closed), and the
  summary prints `grounding 6/6 | fail-closed 8/8`. It measures *coverage of the
  safety properties*, not just "the tests are green." That tagging idea is worth
  stealing.
- The write-path cases run through `WriteTools` (propose only) against a
  `dry_run=True` client, so we test the safety logic **without touching the real
  environment.**
- Observability is baked in: `telemetry` emits one JSON event per action and
  accumulates token/cost per session (`estimate_cost` reads the gateway's usage
  block), and the CLI prints `tokens / cost / llm_calls` on the way out.

The supporting self-tests, all offline:

- `sec_selftest.py` — the Phase 8 security invariants (28 checks, encryption
  included).
- `ux_selftest.py` — the Phase 10 UX (13 checks, feedback persistence included).
- `smoke.py` — the read-only tool surface against the live environment.

---

## The one-liner

The value here isn't "we connected an LLM." It's that we **turned two soft rules
— *safe* and *don't fabricate* — into hard, code-level ones:**

- Grounding → pre-execution gates in the validator and resolver.
- Read-only → a transport-layer method allow-list plus physically separate
  read/write objects.
- No cross-org → a transport-layer org assertion.
- No leakage → dual-channel redaction plus a structured injection fence.
- No accidental writes → dry-run by default, a propose-only state machine, and an
  elevated confirmation that names the resource.
- No regression → a tiered golden set scored by safety-property coverage.

Pair that with a "probe first, admit the limits, extend without breaking anyone"
habit, and what you've got is a genuinely solid piece of **defensive
engineering.**

## Where I'd go next

- Swap the regex-based SQL validation for a lightweight AST parser, so it holds
  up on complex joins and subqueries.
- Wire the scan budget into `EXPLAIN` (if the platform exposes it) to estimate
  cost *before* the query runs.
- Add the router's LLM fallback for the genuinely ambiguous intents.
