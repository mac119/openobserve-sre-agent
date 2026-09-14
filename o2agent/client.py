"""OpenObserve HTTP client — read-only by construction.

Only GET and the bounded `_search` POST are permitted. Any attempt to issue a
mutating method raises immediately, enforcing the Tier-1 (read-only) guarantee
at the transport layer. Mutating tools (Phase 7) will use a separate, explicitly
gated path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .security import SecurityError, assert_same_org, check_endpoint


class OpenObserveError(Exception):
    """Normalized OpenObserve API error."""

    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"[{status}] {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


@dataclass
class ToolResult:
    """Normalized result envelope for every tool/client call (architecture §6)."""

    ok: bool
    data: Any = None
    error: str | None = None
    duration_ms: int = 0
    scan_records: int | None = None
    scan_size: float | None = None
    meta: dict = field(default_factory=dict)


class ReadOnlyClient:
    """Read-only OpenObserve client. Refuses mutating HTTP methods."""

    _READ_METHODS = {"GET"}

    def __init__(self, settings: Settings, auth: str | None = None):
        self._s = settings
        if not check_endpoint(settings.base_url, settings.endpoint_allowlist):
            raise SecurityError(
                f"base_url host is not in the endpoint allowlist: {settings.base_url}"
            )
        # auth override enables per-user credential passthrough (P3): a multi-user
        # host can pass the requesting user's token instead of the shared service
        # account, so OpenObserve enforces that user's real permissions.
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": auth or settings.auth},
            timeout=settings.http_timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ReadOnlyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retries: int = 2,
    ) -> ToolResult:
        method = method.upper()
        # Transport-level read-only guard. A small allow-list of side-effect-free
        # POSTs is permitted: bounded `_search`, and the VRL dry-run test endpoint
        # (`functions/test`) — neither creates/modifies persistent resources.
        safe_post = method == "POST" and (
            path.endswith("/_search") or path.endswith("/functions/test")
        )
        if method not in self._READ_METHODS and not safe_post:
            raise OpenObserveError(0, f"mutating method {method} not allowed on read client", path)

        # Cross-organization defense: never touch another org's data.
        assert_same_org(path, self._s.org)

        start = time.monotonic()
        last_err: str | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.HTTPError as e:
                last_err = f"transport error: {e}"
                time.sleep(min(0.5 * (2 ** attempt), 4))
                continue

            dur = int((time.monotonic() - start) * 1000)
            if resp.status_code == 429 and attempt < retries:
                time.sleep(min(0.5 * (2 ** attempt), 4))
                continue
            if resp.status_code >= 400:
                return ToolResult(
                    ok=False,
                    error=f"[{resp.status_code}] {resp.text[:300]}",
                    duration_ms=dur,
                    meta={"status": resp.status_code, "path": path},
                )
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            scan_records = body.get("scan_records") if isinstance(body, dict) else None
            scan_size = body.get("scan_size") if isinstance(body, dict) else None
            return ToolResult(
                ok=True,
                data=body,
                duration_ms=dur,
                scan_records=scan_records,
                scan_size=scan_size,
                meta={"status": resp.status_code, "path": path, "retries": attempt},
            )

        dur = int((time.monotonic() - start) * 1000)
        return ToolResult(ok=False, error=last_err or "unknown error", duration_ms=dur,
                          meta={"path": path})

    # -- convenience -------------------------------------------------------

    def _org_path(self, suffix: str, *, api: str = "api") -> str:
        return f"/{api}/{self._s.org}/{suffix}"

    def get(self, path: str, params: dict | None = None) -> ToolResult:
        return self._request("GET", path, params=params)

    def search(self, sql: str, start_time: int, end_time: int, size: int,
               *, stream_type: str = "logs", from_: int = 0) -> ToolResult:
        """Bounded SQL search. Row size is clamped to the configured max."""
        size = max(0, min(size, self._s.max_rows))
        body = {
            "query": {
                "sql": sql,
                "start_time": start_time,
                "end_time": end_time,
                "from": from_,
                "size": size,
            }
        }
        path = self._org_path("_search")
        return self._request("POST", path,
                             params={"type": stream_type}, json=body)

    def validate_vrl(self, function: str, events: list[dict] | None = None) -> ToolResult:
        """VRL dry-run via functions/test. ok=True means it compiled & ran.
        Side-effect-free (does not persist a function)."""
        body = {"function": function, "events": events or [{"_dummy": 1}]}
        path = self._org_path("functions/test")
        return self._request("POST", path, json=body)

    def validate_promql(self, query: str) -> ToolResult:
        """PromQL syntax check via format_query (returns normalized query, or 400)."""
        path = self._org_path("prometheus/api/v1/format_query")
        return self._request("GET", path, params={"query": query})
