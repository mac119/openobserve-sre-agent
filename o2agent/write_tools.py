"""Write tools — build mutating change proposals and route them through the
Confirmation Gate. These tools only ever create a `pending` change; execution
requires an explicit external approve() call (never the model).

Endpoint paths follow api-surface.md (alerts on v2).
"""
from __future__ import annotations

import json

from .gate import ConfirmationGate, PendingChange


def _diff(action: str, resource_kind: str, name: str, body: dict | None) -> str:
    head = f"{action.upper()} {resource_kind}: {name}"
    if body is None:
        return head
    return head + "\n" + json.dumps(body, indent=2, ensure_ascii=False)


class WriteTools:
    """Each method returns a PendingChange for review — nothing is executed."""

    def __init__(self, gate: ConfirmationGate, org: str):
        self.gate = gate
        self.org = org

    def _p(self, suffix: str, *, api: str = "api") -> str:
        return f"/{api}/{self.org}/{suffix}"

    # -- dashboards (Tier 2) ----------------------------------------------

    def create_dashboard(self, dashboard: dict) -> PendingChange:
        name = dashboard.get("title") or dashboard.get("name") or "(unnamed)"
        return self.gate.propose(
            resource_kind="dashboard", action="create",
            method="POST", path=self._p("dashboards"), body=dashboard,
            diff=_diff("create", "dashboard", name, dashboard),
        )

    def update_dashboard(self, dashboard_id: str, dashboard: dict) -> PendingChange:
        return self.gate.propose(
            resource_kind="dashboard", action="update",
            method="PUT", path=self._p(f"dashboards/{dashboard_id}"), body=dashboard,
            diff=_diff("update", "dashboard", dashboard_id, dashboard),
        )

    # -- alerts (Tier 2, v2 path) -----------------------------------------

    def create_alert(self, alert: dict) -> PendingChange:
        name = alert.get("name", "(unnamed)")
        return self.gate.propose(
            resource_kind="alert", action="create",
            method="POST", path=self._p("alerts", api="api/v2"), body=alert,
            diff=_diff("create", "alert", name, alert),
        )

    # -- functions (Tier 2) -----------------------------------------------

    def create_function(self, function: dict) -> PendingChange:
        name = function.get("name", "(unnamed)")
        return self.gate.propose(
            resource_kind="function", action="create",
            method="POST", path=self._p("functions"), body=function,
            diff=_diff("create", "function", name, function),
        )

    # -- delete (Tier 3, elevated) ----------------------------------------

    def delete_resource(self, resource_kind: str, name: str) -> PendingChange:
        """Generic delete. Classified as elevated (Tier 3) by the gate."""
        path_map = {
            "dashboard": self._p(f"dashboards/{name}"),
            "alert": self._p(f"alerts/{name}", api="api/v2"),
            "function": self._p(f"functions/{name}"),
            "stream": self._p(f"streams/{name}"),
        }
        path = path_map.get(resource_kind)
        if not path:
            raise ValueError(f"unsupported delete resource: {resource_kind}")
        return self.gate.propose(
            resource_kind=resource_kind, action="delete",
            method="DELETE", path=path, body=None,
            diff=_diff("delete", resource_kind, name, None),
        )
