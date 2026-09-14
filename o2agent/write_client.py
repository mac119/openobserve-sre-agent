"""Mutating OpenObserve client — physically separate from the read-only client.

This client is the ONLY place that may issue POST/PUT/DELETE for persistent
resources. It is deliberately NOT the read client, so read paths cannot
accidentally mutate.

Safety:
- `dry_run` (default True): `execute()` records the intended request and returns
  a simulated success WITHOUT sending it. Real writes require dry_run=False,
  which the caller must set explicitly.
- Every call — real or simulated — is written to the audit log.
- The client refuses to send unless invoked with an already-approved change id
  (enforced by the ConfirmationGate, not here; this class just carries it).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .security import SecurityError, assert_same_org, check_endpoint
from .telemetry import Telemetry


@dataclass
class WriteResult:
    ok: bool
    status: int | None = None
    data: Any = None
    error: str | None = None
    dry_run: bool = True
    request: dict = field(default_factory=dict)


class WriteClient:
    _MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, settings: Settings, telemetry: Telemetry | None = None,
                 dry_run: bool = True, auth: str | None = None):
        self._s = settings
        self._tel = telemetry
        self.dry_run = dry_run
        if not check_endpoint(settings.base_url, settings.endpoint_allowlist):
            raise SecurityError(
                f"base_url host is not in the endpoint allowlist: {settings.base_url}"
            )
        # auth override enables per-user credential passthrough (P3).
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": auth or settings.auth},
            timeout=settings.http_timeout,
        )

    def close(self) -> None:
        self._http.close()

    def execute(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        change_id: str | None = None,
    ) -> WriteResult:
        method = method.upper()
        if method not in self._MUTATING:
            return WriteResult(ok=False, error=f"{method} is not a mutating method")

        # Cross-organization defense: refuse writes aimed at another org.
        assert_same_org(path, self._s.org)

        req = {"method": method, "path": path, "params": params, "json": json,
               "change_id": change_id}

        if self.dry_run:
            if self._tel:
                self._tel.emit("write_dry_run", change_id=change_id, method=method,
                               path=path, has_body=bool(json))
            return WriteResult(ok=True, status=None, dry_run=True,
                               data={"simulated": True}, request=req)

        # real write
        start = time.monotonic()
        try:
            resp = self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as e:
            if self._tel:
                self._tel.emit("write_error", change_id=change_id, method=method,
                               path=path, error=str(e))
            return WriteResult(ok=False, error=f"transport error: {e}",
                               dry_run=False, request=req)
        dur = int((time.monotonic() - start) * 1000)
        ok = resp.status_code < 400
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if self._tel:
            self._tel.emit("write_executed", change_id=change_id, method=method,
                           path=path, status=resp.status_code, ok=ok, duration_ms=dur)
        return WriteResult(
            ok=ok, status=resp.status_code, dry_run=False,
            data=body if ok else None,
            error=None if ok else f"[{resp.status_code}] {str(body)[:300]}",
            request=req,
        )
