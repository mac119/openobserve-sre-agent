"""Context Resolver (Phase 3, architecture §3.2).

Resolves the concrete context a query needs (org, stream, schema) and **fails
closed**: if the stream or its schema cannot be retrieved, callers must NOT
fabricate — they get a `ResolveError` describing what is missing.

Schemas are cached briefly to avoid repeated lookups within a conversation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .client import ReadOnlyClient
from .tools import GetSchemaInput, OpenObserveTools

# spec.md §5 default time ranges (seconds)
DEFAULT_RANGES = {
    "incident": 30 * 60,
    "interactive": 15 * 60,
    "trend": 24 * 60 * 60,
}


class ResolveError(Exception):
    """Raised when required context cannot be grounded (fail-closed)."""


@dataclass
class StreamSchema:
    stream_name: str
    stream_type: str
    fields: dict[str, str]  # name -> type
    settings: dict = field(default_factory=dict)

    def has_field(self, name: str) -> bool:
        return name in self.fields


class ContextResolver:
    def __init__(self, tools: OpenObserveTools, cache_ttl: float = 60.0):
        self.t = tools
        self._ttl = cache_ttl
        self._streams_cache: tuple[float, list[dict]] | None = None
        self._schema_cache: dict[str, tuple[float, StreamSchema]] = {}

    # -- streams -----------------------------------------------------------

    def list_stream_names(self, stream_type: str | None = None) -> list[str]:
        now = time.monotonic()
        if not self._streams_cache or now - self._streams_cache[0] > self._ttl:
            r = self.t.list_streams()
            if not r.ok:
                raise ResolveError(f"cannot list streams: {r.error}")
            self._streams_cache = (now, r.data.get("list", []))
        items = self._streams_cache[1]
        if stream_type:
            items = [s for s in items if s.get("stream_type") == stream_type]
        return [s["name"] for s in items]

    def resolve_stream(self, stream_name: str, stream_type: str | None = None) -> str:
        names = self.list_stream_names(stream_type)
        if stream_name not in names:
            raise ResolveError(
                f"stream '{stream_name}' not found. Available: {names or '(none)'}. "
                "Refusing to query a non-existent stream."
            )
        return stream_name

    # -- schema ------------------------------------------------------------

    def resolve_schema(self, stream_name: str, stream_type: str = "logs") -> StreamSchema:
        """Fail-closed: returns a real schema or raises. Never fabricates."""
        now = time.monotonic()
        cached = self._schema_cache.get(stream_name)
        if cached and now - cached[0] <= self._ttl:
            return cached[1]

        # ensure the stream actually exists first
        self.resolve_stream(stream_name, stream_type)

        r = self.t.get_schema(GetSchemaInput(stream_name=stream_name, stream_type=stream_type))
        if not r.ok:
            raise ResolveError(f"cannot retrieve schema for '{stream_name}': {r.error}")
        raw_fields = r.data.get("schema", [])
        if not raw_fields:
            raise ResolveError(
                f"schema for '{stream_name}' is empty; refusing to generate "
                "field-specific queries without a known schema."
            )
        schema = StreamSchema(
            stream_name=stream_name,
            stream_type=r.data.get("stream_type", stream_type),
            fields={f["name"]: f.get("type", "") for f in raw_fields},
            settings=r.data.get("settings", {}),
        )
        self._schema_cache[stream_name] = (now, schema)
        return schema

    def invalidate(self, stream_name: str | None = None) -> None:
        if stream_name:
            self._schema_cache.pop(stream_name, None)
        else:
            self._schema_cache.clear()
            self._streams_cache = None

    # -- time range --------------------------------------------------------

    @staticmethod
    def default_range_us(kind: str = "interactive") -> tuple[int, int]:
        """Return (start_us, end_us) for a default bounded window."""
        span = DEFAULT_RANGES.get(kind, DEFAULT_RANGES["interactive"])
        end = int(time.time() * 1_000_000)
        return end - span * 1_000_000, end
