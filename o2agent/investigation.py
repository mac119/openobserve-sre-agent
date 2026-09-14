"""Alert investigation workflow (Phase 6, architecture §3.4 investigation).

Answers "Why did my last alert fire?" from EVIDENCE, not guesses:

  resolve last alert -> read definition -> read evaluation history
    -> reconstruct evaluation window -> run the alert query for that window
    -> collect evidence -> conclusion with confidence

Fail-closed: if there are no alerts, or no evaluation history, the workflow
returns a NO_EVIDENCE outcome. The agent must then say it cannot determine the
cause rather than inventing one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .resolver import ContextResolver
from .tools import OpenObserveTools


class Outcome(str, Enum):
    NO_ALERTS = "no_alerts"            # org has no alert definitions
    NO_HISTORY = "no_history"          # alert exists but never evaluated/fired
    EVIDENCE = "evidence"              # real trigger evidence found
    ERROR = "error"                    # could not retrieve required data


@dataclass
class Investigation:
    outcome: Outcome
    alert: dict | None = None
    history: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    window: tuple[int, int] | None = None
    message: str = ""

    def is_grounded(self) -> bool:
        return self.outcome is Outcome.EVIDENCE


class AlertInvestigator:
    def __init__(self, tools: OpenObserveTools, resolver: ContextResolver):
        self.t = tools
        self.r = resolver

    def _pick_last_alert(self, alerts: list[dict], name: str | None) -> dict | None:
        if not alerts:
            return None
        if name:
            for a in alerts:
                if a.get("name") == name:
                    return a
            return None
        # "last" = most recently updated/created if timestamps exist, else first
        def key(a: dict) -> Any:
            return a.get("updated_at") or a.get("last_triggered_at") or a.get("created_at") or 0
        return sorted(alerts, key=key, reverse=True)[0]

    def investigate(self, alert_name: str | None = None) -> Investigation:
        # 1. resolve alert definitions (fail-closed on error)
        r = self.t.list_alerts()
        if not r.ok:
            return Investigation(Outcome.ERROR,
                                 message=f"cannot read alert definitions: {r.error}")
        alerts = (r.data or {}).get("list", []) if isinstance(r.data, dict) else []
        if not alerts:
            return Investigation(
                Outcome.NO_ALERTS,
                message="No alert definitions exist in this organization. "
                        "There is nothing that could have fired; cannot investigate.",
            )

        alert = self._pick_last_alert(alerts, alert_name)
        if alert is None:
            return Investigation(
                Outcome.NO_ALERTS,
                message=f"No alert named '{alert_name}' was found.",
            )

        # 2. evaluation history for that alert
        hr = self.t.alert_history(alert_name=alert.get("name"))
        history = []
        if hr.ok and isinstance(hr.data, dict):
            history = hr.data.get("hits", [])

        if not history:
            return Investigation(
                Outcome.NO_HISTORY,
                alert=alert,
                message=(
                    f"Alert '{alert.get('name')}' exists, but there is NO evaluation "
                    "history for it. Without an actual trigger record, the cause "
                    "cannot be determined. Reporting unknown rather than guessing."
                ),
            )

        # 3. reconstruct window from the most recent history record
        last = history[0]
        window = self._reconstruct_window(alert, last)

        # 4. run the alert query over that window (evidence)
        evidence = self._collect_evidence(alert, window)

        return Investigation(
            Outcome.EVIDENCE,
            alert=alert,
            history=history,
            evidence=evidence,
            window=window,
            message=f"Found {len(history)} evaluation record(s) for '{alert.get('name')}'.",
        )

    def _reconstruct_window(self, alert: dict, last_eval: dict) -> tuple[int, int] | None:
        # try to derive the window from the trigger time and the alert's period
        trig = (last_eval.get("timestamp") or last_eval.get("_timestamp")
                or last_eval.get("triggered_at"))
        if trig is None:
            return None
        trig = int(trig)
        period_min = 0
        tc = alert.get("trigger_condition") or {}
        try:
            period_min = int(tc.get("period", 0))
        except (TypeError, ValueError):
            period_min = 0
        span_us = max(period_min, 5) * 60 * 1_000_000
        return trig - span_us, trig

    def _collect_evidence(self, alert: dict, window: tuple[int, int] | None) -> list[dict]:
        if not window:
            return []
        qc = alert.get("query_condition") or {}
        sql = qc.get("sql") or qc.get("query")
        stream = alert.get("stream_name")
        if not sql and not stream:
            return []
        try:
            if sql:
                res = self.t.search(_search_input(sql, window))
            else:
                res = self.t.search(_search_input(
                    f'SELECT * FROM "{stream}"', window))
        except Exception as e:
            return [{"note": f"evidence query failed: {e}"}]
        if res.ok and isinstance(res.data, dict):
            return res.data.get("hits", [])[:20]
        return [{"note": f"evidence query error: {res.error}"}]


def _search_input(sql: str, window: tuple[int, int]):
    from .tools import SearchInput
    return SearchInput(sql=sql, start_time=window[0], end_time=window[1], size=20)
