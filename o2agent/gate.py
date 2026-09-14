"""Confirmation Gate (Phase 7, architecture §3.6).

A state machine that guards every mutating operation:

    pending --approve--> approved --execute--> executing --success--> done
       |                    |                      |
       +--reject--> rejected|                      +--failure--> failed

Two tiers:
- standard  (Tier 2): create/update observability resources.
- elevated  (Tier 3): delete, users/roles/permissions, service accounts, org
  settings, quota. Requires an elevated approval that names the resource+impact.

The gate NEVER auto-approves. `approve()` must be called by an external actor
(the human/UI), not by the model. The agent can only create `pending` changes
and render their diffs.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .write_client import WriteClient, WriteResult
from .telemetry import Telemetry


class Tier(str, Enum):
    STANDARD = "standard"
    ELEVATED = "elevated"


class GateState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"


class GateError(Exception):
    pass


@dataclass
class PendingChange:
    id: str
    tier: Tier
    resource_kind: str
    action: str  # create | update | delete
    method: str
    path: str
    body: dict | None
    diff: str
    state: GateState = GateState.PENDING
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    result: WriteResult | None = None


# actions/resources that force the elevated tier
_ELEVATED_ACTIONS = {"delete"}
_ELEVATED_RESOURCES = {"user", "role", "permission", "service_account",
                       "org_settings", "quota"}


def classify_tier(resource_kind: str, action: str) -> Tier:
    if action.lower() in _ELEVATED_ACTIONS:
        return Tier.ELEVATED
    if resource_kind.lower() in _ELEVATED_RESOURCES:
        return Tier.ELEVATED
    return Tier.STANDARD


class ConfirmationGate:
    def __init__(self, write_client: WriteClient, telemetry: Telemetry | None = None):
        self.wc = write_client
        self.tel = telemetry
        self._changes: dict[str, PendingChange] = {}

    def propose(
        self,
        *,
        resource_kind: str,
        action: str,
        method: str,
        path: str,
        body: dict | None,
        diff: str,
    ) -> PendingChange:
        """Create a pending change. Never executes. Returns the change for review."""
        tier = classify_tier(resource_kind, action)
        change = PendingChange(
            id=uuid.uuid4().hex[:12], tier=tier, resource_kind=resource_kind,
            action=action.lower(), method=method.upper(), path=path, body=body,
            diff=diff,
        )
        self._changes[change.id] = change
        if self.tel:
            self.tel.emit("change_proposed", change_id=change.id, tier=tier.value,
                          resource=resource_kind, action=change.action, path=path)
        return change

    def get(self, change_id: str) -> PendingChange:
        c = self._changes.get(change_id)
        if not c:
            raise GateError(f"unknown change id: {change_id}")
        return c

    def approve(self, change_id: str, *, elevated_confirmation: str | None = None) -> PendingChange:
        """Move pending -> approved. For elevated tier, the caller must pass an
        elevated_confirmation string that names the resource (proof of intent)."""
        c = self.get(change_id)
        if c.state is not GateState.PENDING:
            raise GateError(f"change {change_id} is {c.state.value}, cannot approve")
        if c.tier is Tier.ELEVATED:
            if not elevated_confirmation or c.resource_kind not in elevated_confirmation:
                raise GateError(
                    "elevated change requires an elevated_confirmation naming the "
                    f"resource ('{c.resource_kind}')"
                )
        c.state = GateState.APPROVED
        if self.tel:
            self.tel.emit("change_approved", change_id=change_id, tier=c.tier.value)
        return c

    def reject(self, change_id: str) -> PendingChange:
        c = self.get(change_id)
        if c.state not in (GateState.PENDING, GateState.APPROVED):
            raise GateError(f"change {change_id} is {c.state.value}, cannot reject")
        c.state = GateState.REJECTED
        c.resolved_at = time.time()
        if self.tel:
            self.tel.emit("change_rejected", change_id=change_id)
        return c

    def execute(self, change_id: str) -> PendingChange:
        """Execute an approved change. Refuses any non-approved state."""
        c = self.get(change_id)
        if c.state is not GateState.APPROVED:
            raise GateError(
                f"change {change_id} is {c.state.value}; only 'approved' changes "
                "can execute (no auto-execution)"
            )
        c.state = GateState.EXECUTING
        res = self.wc.execute(c.method, c.path, json=c.body, change_id=c.id)
        c.result = res
        c.state = GateState.DONE if res.ok else GateState.FAILED
        c.resolved_at = time.time()
        if self.tel:
            self.tel.emit("change_result", change_id=change_id, ok=res.ok,
                          state=c.state.value, dry_run=res.dry_run)
        return c
