"""Tool registry: expose read-only tools as OpenAI function schemas and
dispatch tool calls to the typed OpenObserve tool layer.

Only read-only tools are registered here. Mutating tools (Phase 7) will be
registered on a separate, confirmation-gated surface.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .client import ToolResult
from .investigation import AlertInvestigator, Outcome
from .resolver import ContextResolver
from .security import normalize_permission_error, over_scan_budget
from .tools import GetSchemaInput, ListStreamsInput, OpenObserveTools, SearchInput
from .validator import SQLValidator


def _schema(name: str, desc: str, params: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": params},
    }


# OpenAI-shaped function definitions for the read-only surface.
TOOL_SPECS: list[dict] = [
    _schema("list_streams", "List streams. Optional stream_type (logs|metrics|traces|enrichment_tables).",
            {"type": "object", "properties": {
                "stream_type": {"type": "string"},
            }}),
    _schema("get_schema", "Get the schema (fields + types) of a stream. Required before field-specific queries.",
            {"type": "object", "properties": {
                "stream_name": {"type": "string"},
                "stream_type": {"type": "string", "default": "logs"},
            }, "required": ["stream_name"]}),
    _schema("search", "Run a bounded SQL search. Times are epoch MICROSECONDS. Always include a time range.",
            {"type": "object", "properties": {
                "sql": {"type": "string"},
                "start_time": {"type": "integer", "description": "epoch microseconds"},
                "end_time": {"type": "integer", "description": "epoch microseconds"},
                "size": {"type": "integer", "default": 100},
                "stream_type": {"type": "string", "default": "logs"},
            }, "required": ["sql", "start_time", "end_time"]}),
    _schema("list_alerts", "List alert definitions (v2).", {"type": "object", "properties": {}}),
    _schema("alert_history", "Read alert evaluation history (v2).", {"type": "object", "properties": {}}),
    _schema("list_dashboards", "List dashboards.", {"type": "object", "properties": {}}),
    _schema("list_functions", "List reusable VRL functions.", {"type": "object", "properties": {}}),
    _schema("list_pipelines", "List pipelines.", {"type": "object", "properties": {}}),
    _schema("summary", "Org summary: counts + health for streams/pipelines/alerts/functions/dashboards.",
            {"type": "object", "properties": {}}),
    _schema("list_organizations", "List organizations.", {"type": "object", "properties": {}}),
    _schema("validate_query",
            "Validate (and auto-repair) a generated VRL/PromQL/regex artifact before "
            "you present it. kind is one of vrl|promql|regex. Returns the validated or "
            "repaired artifact, or an unvalidated downgrade. ALWAYS validate generated "
            "VRL/PromQL/regex through this before showing it to the user.",
            {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["vrl", "promql", "regex"]},
                "artifact": {"type": "string"},
            }, "required": ["kind", "artifact"]}),
    _schema("investigate_alert",
            "Investigate why an alert fired, grounded in real alert definition + "
            "evaluation history. Optional alert_name; omit for the most recent alert. "
            "Returns a structured outcome: no_alerts / no_history / evidence. If there "
            "is no evaluation history, you MUST report that the cause cannot be "
            "determined — do NOT guess a reason.",
            {"type": "object", "properties": {
                "alert_name": {"type": "string", "description": "optional; omit for last alert"},
            }}),
    _schema("fetch_result",
            "Fetch more rows from a previously offloaded large tool result. Use the "
            "result_ref returned in a trimmed tool summary to page through the full "
            "data on demand, instead of re-running the query.",
            {"type": "object", "properties": {
                "result_ref": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 20},
            }, "required": ["result_ref"]}),
    _schema("remember",
            "Persist a durable note. scope='session' (default) survives context "
            "compaction within THIS conversation. scope='long_term' persists an "
            "org-level environment fact or user preference ACROSS sessions (e.g. "
            "'prod errors are in stream app_prod', 'user prefers concise answers'); "
            "for long_term always pass a short stable 'key'. kind is fact|task|key_id "
            "(session) or fact|preference|env (long_term). Long-term notes are "
            "ASSUMPTIONS, not verified facts — still re-verify identifiers via read tools.",
            {"type": "object", "properties": {
                "kind": {"type": "string"},
                "note": {"type": "string"},
                "key": {"type": "string", "description": "identifier; required for long_term"},
                "scope": {"type": "string", "enum": ["session", "long_term"],
                          "default": "session"},
                "importance": {"type": "integer", "description": "1..5, long_term only"},
                "ttl_days": {"type": "integer", "description": "expiry in days, long_term only"},
            }, "required": ["note"]}),
    _schema("propose_change",
            "Propose a mutating change (create/update/delete a dashboard/alert/function, "
            "or delete a resource). This ONLY creates a pending change for the user to "
            "review and approve — it NEVER executes. Returns the change id, tier, and a "
            "diff. Deletes and admin resources are elevated (Tier 3) and need extra "
            "confirmation. Use this instead of claiming you created/deleted anything.",
            {"type": "object", "properties": {
                "resource_kind": {"type": "string",
                                  "enum": ["dashboard", "alert", "function", "stream"]},
                "action": {"type": "string", "enum": ["create", "update", "delete"]},
                "name": {"type": "string", "description": "resource name/id (for update/delete)"},
                "body": {"type": "object", "description": "resource definition (for create/update)"},
            }, "required": ["resource_kind", "action"]}),
    _schema("build_dashboard",
            "Build a COMPLETE, schema-valid OpenObserve dashboard from a small "
            "high-level spec and propose it for approval (goes through the "
            "confirmation gate — does NOT auto-create). STRONGLY PREFER this over "
            "propose_change for creating dashboards: it fills in all the required "
            "OpenObserve panel boilerplate for you, so you only provide the title "
            "and a list of panels (each with a chart type, a chart SQL using "
            "x_axis/y_axis/z_axis aliases, and the axis field/aggregation). Build "
            "ALL panels in ONE call. After the user approves, call approve_change ONCE.",
            {"type": "object", "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "stream": {"type": "string", "description": "default stream for panels"},
                "panels": {"type": "array", "description": "one entry per panel", "items": {
                    "type": "object", "properties": {
                        "title": {"type": "string"},
                        "type": {"type": "string",
                                 "description": "line|bar|area|pie|table|scatter"},
                        "query": {"type": "string",
                                  "description": "chart SQL with x_axis_1/y_axis_1[/z_axis_1] aliases"},
                        "stream": {"type": "string"},
                        "x": {"type": "object",
                              "description": "{field, alias, function} e.g. field=_timestamp function=histogram"},
                        "y": {"type": "object",
                              "description": "{field, alias, function} e.g. function=count"},
                        "breakdown": {"type": "object",
                                      "description": "optional {field, alias} for the z-axis series"},
                    }, "required": ["query"]}},
            }, "required": ["title", "panels"]}),
    _schema("approve_change",
            "Approve AND execute a change that you previously created with "
            "propose_change, when — and ONLY when — the user has clearly approved it "
            "(e.g. they replied 'approve' / 'yes' / 'go ahead'). This actually runs "
            "the change through the confirmation gate and returns the REAL result. "
            "You MUST NOT claim a change was created/executed unless this tool "
            "returned ok=true. For an elevated (Tier 3) change, pass 'confirmation' "
            "that names the resource. If the result has dry_run=true, tell the user it "
            "was only simulated (not actually written) because the server runs in "
            "dry-run mode.",
            {"type": "object", "properties": {
                "change_id": {"type": "string", "description": "the id returned by propose_change"},
                "confirmation": {"type": "string",
                                 "description": "for elevated changes: a phrase naming the resource"},
            }, "required": ["change_id"]}),
]


class ToolRegistry:
    def __init__(self, tools: OpenObserveTools, resolver: ContextResolver,
                 validator: SQLValidator, generation=None, scan_budget: int = 0):
        self.t = tools
        self.resolver = resolver
        self.validator = validator
        self.generation = generation  # optional GenerationWorkflow
        self.scan_budget = scan_budget  # scan_records budget (0 = disabled)
        self.investigator = AlertInvestigator(tools, resolver)
        self._dispatch: dict[str, Callable[[dict], ToolResult]] = {
            "list_streams": lambda a: self.t.list_streams(ListStreamsInput(**a)),
            "get_schema": lambda a: self.t.get_schema(GetSchemaInput(**a)),
            "search": self._gated_search,
            "validate_query": self._validate_query,
            "investigate_alert": self._investigate_alert,
            "list_alerts": lambda a: self.t.list_alerts(),
            "alert_history": lambda a: self.t.alert_history(),
            "list_dashboards": lambda a: self.t.list_dashboards(),
            "list_functions": lambda a: self.t.list_functions(),
            "list_pipelines": lambda a: self.t.list_pipelines(),
            "summary": lambda a: self.t.summary(),
            "list_organizations": lambda a: self.t.list_organizations(),
        }
        self.gate = None
        self.write_tools = None
        self._memory = None
        self._session_id = None
        self._org = "default"
        self._settings = None
        self._max_rows = getattr(validator, "max_rows", 1000)

    def attach_memory(self, memory, session_id: str, org: str = "default",
                      settings=None) -> None:
        """Enable memory-backed tools (fetch_result + remember) for a session."""
        self._memory = memory
        self._session_id = session_id
        self._org = org
        self._settings = settings
        self._dispatch["fetch_result"] = self._fetch_result
        self._dispatch["remember"] = self._remember

    def _remember(self, a: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, error="memory not configured")
        note = a.get("note") or ""
        if not note:
            return ToolResult(ok=False, error="note is required")
        scope = (a.get("scope") or "session").lower()
        if scope == "long_term":
            s = self._settings
            if s is not None and not getattr(s, "ltm_enabled", True):
                return ToolResult(ok=False, error="long-term memory is disabled")
            rec = self._memory.remember_long_term(
                self._org, a.get("key", ""), note,
                kind=a.get("kind", "fact"), source="assistant",
                importance=int(a.get("importance", 1) or 1),
                ttl_days=int(a.get("ttl_days", getattr(s, "ltm_default_ttl_days", 0)) or 0),
                max_per_org=getattr(s, "ltm_max_per_org", 100),
            )
            return ToolResult(ok=True, data={
                "remembered": True, "scope": "long_term",
                "key": rec["key"], "org": rec["org"],
                "note": "Stored as an org-level ASSUMPTION for future sessions; "
                        "re-verify identifiers via read tools before relying on them.",
            })
        pad = self._memory.remember(
            self._session_id, a.get("kind", "fact"), note, a.get("key", ""),
        )
        return ToolResult(ok=True, data={
            "remembered": True, "scope": "session",
            "facts": len(pad["facts"]), "tasks": len(pad["tasks"]),
            "key_ids": len(pad["key_ids"]),
        })

    def _fetch_result(self, a: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, error="result store not configured")
        ref = a.get("result_ref") or ""
        rec = self._memory.fetch_result(ref)
        if not rec:
            return ToolResult(ok=False, error=f"unknown result_ref: {ref!r}")
        import json as _json
        try:
            rows = _json.loads(rec["full_json"])
        except (ValueError, TypeError):
            rows = rec["full_json"]
        offset = max(0, int(a.get("offset", 0) or 0))
        limit = max(1, min(int(a.get("limit", 20) or 20), self._max_rows))
        if isinstance(rows, list):
            page = rows[offset:offset + limit]
        else:
            page = rows  # non-list payload: return as-is
        return ToolResult(ok=True, data={
            "tool": rec["tool"], "row_count": rec["row_count"],
            "offset": offset, "limit": limit, "rows": page,
        })

    def attach_write(self, gate, write_tools) -> None:
        """Enable the propose_change tool (write path stays confirmation-gated)."""
        self.gate = gate
        self.write_tools = write_tools
        self._dispatch["propose_change"] = self._propose_change
        self._dispatch["approve_change"] = self._approve_change
        self._dispatch["build_dashboard"] = self._build_dashboard

    def _build_dashboard(self, a: dict) -> ToolResult:
        """Assemble a full, schema-valid dashboard body from a small spec and
        propose it through the gate (single change for the whole dashboard)."""
        if self.write_tools is None:
            return ToolResult(ok=False, error="write path not configured")
        from .dashboard_builder import build_dashboard_body
        try:
            body = build_dashboard_body(a)
        except ValueError as e:
            return ToolResult(ok=False, error=f"invalid dashboard spec: {e}")
        # reuse propose_change's de-dup + note handling
        return self._propose_change({
            "resource_kind": "dashboard", "action": "create", "body": body,
        })

    def _approve_change(self, a: dict) -> ToolResult:
        """Approve + execute pending change(s) through the gate, returning the REAL
        result. Robust against a propose/approve loop: when the user just says
        'approve' (no id), this approves & executes ALL currently-pending
        standard-tier changes in one shot, so there is nothing left to re-approve.
        Elevated (Tier 3) changes still require an explicit id + confirmation."""
        if self.gate is None:
            return ToolResult(ok=False, error="write path not configured")
        cid = a.get("change_id") or ""
        phrase = a.get("confirmation")
        changes = getattr(self.gate, "_changes", {})

        # Resolve the set of changes to act on.
        if cid and cid in changes:
            targets = [changes[cid]]
        else:
            targets = [c for c in changes.values() if c.state.value == "pending"]
            targets.sort(key=lambda c: c.created_at)

        if not targets:
            # nothing pending — report the most recent finished change, if any
            done = sorted((c for c in changes.values()
                           if c.state.value in ("done", "failed")),
                          key=lambda c: (c.resolved_at or 0))
            if done:
                last = done[-1]
                r0 = last.result
                ok0 = bool(r0 and r0.ok)
                return ToolResult(ok=ok0, data={
                    "change_id": last.id, "state": last.state.value,
                    "executed": ok0, "already_done": True,
                    "note": "The change was already executed. Report this and stop.",
                }, error=(None if ok0 else "previously failed"))
            return ToolResult(ok=False, error="no pending change to approve")

        executed, failed, skipped = [], [], []
        for c in targets:
            if c.state.value in ("done", "failed"):
                continue  # idempotent: already handled
            if c.state.value != "pending":
                continue
            if c.tier.value == "elevated" and (
                    not phrase or c.resource_kind not in phrase):
                skipped.append({"change_id": c.id, "resource_kind": c.resource_kind,
                                "reason": "elevated: needs confirmation naming the resource"})
                continue
            try:
                self.gate.approve(c.id, elevated_confirmation=phrase)
                done_c = self.gate.execute(c.id)
            except Exception as e:
                failed.append({"change_id": c.id, "error": f"{e}"})
                continue
            r = done_c.result
            (executed if (r and r.ok) else failed).append({
                "change_id": done_c.id,
                "state": done_c.state.value,
                "dry_run": bool(r and r.dry_run),
                "result": (r.data if r else None),
                "error": (None if (r and r.ok) else (r.error if r else "execution failed")),
            })

        any_ok = bool(executed)
        any_dry = any(e.get("dry_run") for e in executed)
        note = ("Change(s) executed successfully — STOP now; do NOT propose or "
                "approve again." if any_ok and not any_dry else
                "SIMULATED ONLY (dry_run) — nothing was actually written; set "
                "O2_WRITE_DRY_RUN=0 to enable real writes. STOP now." if any_dry else
                "No change executed. Report the failure honestly; do NOT retry by "
                "re-proposing.")
        return ToolResult(
            ok=any_ok,
            data={"executed": executed, "failed": failed, "skipped": skipped,
                  "note": note},
            error=(None if any_ok else
                   (failed[0]["error"] if failed else
                    (skipped[0]["reason"] if skipped else "nothing executed"))),
        )

    def _propose_change(self, a: dict) -> ToolResult:
        if self.write_tools is None:
            return ToolResult(ok=False, error="write path not configured")
        kind = (a.get("resource_kind") or "").lower()
        action = (a.get("action") or "").lower()
        name = a.get("name") or ""
        body = a.get("body") or {}
        # De-dup: if an equivalent change is already pending, reuse it instead of
        # stacking duplicates (guards against a propose/approve loop on retries).
        try:
            for c in getattr(self.gate, "_changes", {}).values():
                if (c.state.value == "pending" and c.resource_kind == kind
                        and c.action == action and (c.body or {}) == body):
                    return ToolResult(ok=True, data={
                        "change_id": c.id, "tier": c.tier.value,
                        "state": c.state.value, "diff": c.diff,
                        "note": ("An identical proposal already exists — reusing it. "
                                 "Do NOT create another. When the user approves, call "
                                 "approve_change ONCE with this change_id."),
                    })
        except Exception:
            pass
        try:
            if action == "delete":
                change = self.write_tools.delete_resource(kind, name)
            elif kind == "dashboard" and action == "create":
                change = self.write_tools.create_dashboard(body)
            elif kind == "dashboard" and action == "update":
                change = self.write_tools.update_dashboard(name, body)
            elif kind == "alert" and action == "create":
                change = self.write_tools.create_alert(body)
            elif kind == "function" and action == "create":
                change = self.write_tools.create_function(body)
            else:
                return ToolResult(ok=False, error=f"unsupported change: {action} {kind}")
        except Exception as e:
            return ToolResult(ok=False, error=f"propose error: {e}")
        return ToolResult(
            ok=True,
            data={
                "change_id": change.id,
                "tier": change.tier.value,
                "state": change.state.value,
                "diff": change.diff,
                "note": ("This is a PROPOSAL only. It has NOT run. Do NOT tell the "
                         "user it was created. When the user approves (e.g. replies "
                         "'approve'/'yes'), you MUST call the approve_change tool with "
                         "this change_id to actually execute it, then report the tool's "
                         "REAL result. " + ("Elevated (Tier 3): approve_change needs a "
                         "'confirmation' that names the resource."
                         if change.tier.value == "elevated" else "")),
            },
        )

    def _investigate_alert(self, a: dict) -> ToolResult:
        """Run the grounded alert investigation. Fail-closed on no evidence."""
        inv = self.investigator.investigate(alert_name=a.get("alert_name"))
        return ToolResult(
            ok=(inv.outcome is not Outcome.ERROR),
            data={
                "outcome": inv.outcome.value,
                "grounded": inv.is_grounded(),
                "message": inv.message,
                "alert": inv.alert,
                "history_count": len(inv.history),
                "window": inv.window,
                "evidence": inv.evidence,
            },
            error=inv.message if inv.outcome is Outcome.ERROR else None,
        )

    def _gated_search(self, a: dict) -> ToolResult:
        """Enforce the Validator gate before any search executes."""
        inp = SearchInput(**a)
        vr = self.validator.validate(
            inp.sql, start_time=inp.start_time, end_time=inp.end_time,
            stream_type=inp.stream_type,
        )
        if not vr.ok:
            return ToolResult(
                ok=False,
                error=f"query rejected by validator: {vr.as_message()}",
                meta={"validation": True, "stream": vr.stream,
                      "used_fields": vr.used_fields},
            )
        result = self.t.search(inp)
        if not result.ok:
            return result
        warnings = list(vr.warnings or [])
        # Scan-budget control: the scan cost is only known after execution, so
        # flag over-budget queries prominently (warning + meta) so the model and
        # user see the cost and can narrow the range instead of silently paying it.
        if over_scan_budget(result.scan_records, self.scan_budget):
            result.meta["scan_budget_exceeded"] = True
            warnings.append(
                f"scan budget exceeded: scanned {result.scan_records} records "
                f"(budget {self.scan_budget}). Narrow the time range or add a "
                f"filter/index to reduce scan cost."
            )
        if warnings:
            result.meta["warnings"] = warnings
        return result

    def _validate_query(self, a: dict) -> ToolResult:
        """Validate + auto-repair a VRL/PromQL/regex artifact."""
        if self.generation is None:
            return ToolResult(ok=False, error="generation workflow not configured")
        from .generation import ArtifactKind
        kind_str = (a.get("kind") or "").lower()
        try:
            kind = ArtifactKind(kind_str)
        except ValueError:
            return ToolResult(ok=False, error=f"unsupported kind: {kind_str!r} (use vrl|promql|regex)")
        gr = self.generation.run(kind, a.get("artifact", ""))
        return ToolResult(
            ok=(gr.status.value != "unvalidated"),
            data={
                "kind": gr.kind.value,
                "artifact": gr.artifact,
                "status": gr.status.value,
                "attempts": gr.attempts,
                "last_error": gr.last_error,
            },
            error=None if gr.status.value != "unvalidated" else f"unvalidated: {gr.last_error}",
        )

    def specs(self) -> list[dict]:
        return TOOL_SPECS

    def call(self, name: str, arguments: dict) -> ToolResult:
        fn = self._dispatch.get(name)
        if fn is None:
            return ToolResult(ok=False, error=f"unknown tool: {name}")
        try:
            result = fn(arguments)
        except Exception as e:  # normalize tool-input errors
            return ToolResult(ok=False, error=f"tool input error: {e}")
        # Normalize permission failures (401/403) to a stable, user-safe message
        # and ensure any error string is redacted before it can reach the model.
        if not result.ok and result.error:
            result.error = normalize_permission_error(result.error)
        return result
