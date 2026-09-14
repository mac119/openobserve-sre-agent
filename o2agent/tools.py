"""Read-only OpenObserve tools.

Each tool wraps a verified endpoint (see api-surface.md) and returns a
ToolResult. Endpoint paths reflect Phase-0 findings: alerts use the v2 path,
enrichment tables go through the streams endpoint, usage is served by summary,
and there is no dedicated incidents endpoint in v0.91.0-rc1.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .client import ReadOnlyClient, ToolResult


# --- input schemas --------------------------------------------------------

class ListStreamsInput(BaseModel):
    stream_type: str | None = Field(default=None, description="logs|metrics|traces|enrichment_tables")
    limit: int | None = Field(default=None)
    offset: int | None = Field(default=None)


class GetSchemaInput(BaseModel):
    stream_name: str
    stream_type: str = "logs"


class SearchInput(BaseModel):
    sql: str
    start_time: int = Field(description="epoch microseconds")
    end_time: int = Field(description="epoch microseconds")
    size: int = 100
    from_: int = 0
    stream_type: str = "logs"


# --- tools ----------------------------------------------------------------

class OpenObserveTools:
    """Typed, read-only tool surface over OpenObserve."""

    def __init__(self, client: ReadOnlyClient):
        self.c = client

    def list_streams(self, inp: ListStreamsInput | None = None) -> ToolResult:
        inp = inp or ListStreamsInput()
        params: dict = {}
        if inp.stream_type:
            params["type"] = inp.stream_type
        if inp.limit is not None:
            params["limit"] = inp.limit
        if inp.offset is not None:
            params["offset"] = inp.offset
        return self.c.get(self.c._org_path("streams"), params=params or None)

    def get_schema(self, inp: GetSchemaInput) -> ToolResult:
        path = self.c._org_path(f"streams/{inp.stream_name}/schema")
        return self.c.get(path, params={"type": inp.stream_type})

    def search(self, inp: SearchInput) -> ToolResult:
        return self.c.search(
            inp.sql, inp.start_time, inp.end_time, inp.size,
            stream_type=inp.stream_type, from_=inp.from_,
        )

    def list_alerts(self) -> ToolResult:
        # v2 path (v1 returns 404)
        return self.c.get(self.c._org_path("alerts", api="api/v2"))

    def get_alert(self, alert_id: str) -> ToolResult:
        return self.c.get(self.c._org_path(f"alerts/{alert_id}", api="api/v2"))

    def alert_history(self, alert_name: str | None = None, size: int = 100) -> ToolResult:
        params: dict = {"size": size}
        if alert_name:
            params["alert_name"] = alert_name
        return self.c.get(self.c._org_path("alerts/history", api="api/v2"), params=params)

    def list_dashboards(self) -> ToolResult:
        return self.c.get(self.c._org_path("dashboards"))

    def list_functions(self) -> ToolResult:
        return self.c.get(self.c._org_path("functions"))

    def list_pipelines(self) -> ToolResult:
        return self.c.get(self.c._org_path("pipelines"))

    def list_reports(self) -> ToolResult:
        return self.c.get(self.c._org_path("reports"))

    def list_saved_views(self) -> ToolResult:
        return self.c.get(self.c._org_path("savedviews"))

    def list_enrichment_tables(self) -> ToolResult:
        return self.c.get(self.c._org_path("streams"), params={"type": "enrichment_tables"})

    def summary(self) -> ToolResult:
        """Global counts + health — stands in for the missing usage endpoint."""
        return self.c.get(self.c._org_path("summary"))

    def list_organizations(self) -> ToolResult:
        return self.c.get("/api/organizations")
