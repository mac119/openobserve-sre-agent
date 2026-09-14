"""Validator (Phase 5, architecture §3.5) — a standalone gate.

Turns grounding from "the model should" into "the code enforces". Before a
generated SQL query is executed, it must pass validation:

  1. Extract the stream from FROM and confirm it exists.
  2. Resolve the stream schema (fail-closed if missing).
  3. Extract referenced field identifiers and confirm each exists in the schema.
  4. Enforce a bounded time range and a row limit.

A query that references a non-existent stream or field is REJECTED here, even if
the model produced it confidently. This is deliberately conservative: on any
ambiguity it errs toward asking rather than executing a possibly-wrong scan.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .client import ReadOnlyClient
from .resolver import ContextResolver, ResolveError, StreamSchema

# SQL keywords / functions that are not field identifiers
_SQL_NOISE = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "as", "and", "or", "not", "in", "is", "null", "like", "between", "asc", "desc",
    "count", "sum", "avg", "min", "max", "distinct", "case", "when", "then", "else",
    "end", "cast", "on", "join", "left", "right", "inner", "outer", "union", "all",
    "true", "false", "histogram", "approx_percentile_cont", "date_trunc",
}

_FROM_RE = re.compile(r'\bfrom\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', re.I)
# identifiers: bare or double-quoted; skip string literals in single quotes
_IDENT_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')
_STRING_LIT_RE = re.compile(r"'[^']*'")


@dataclass
class ValidationResult:
    ok: bool
    stream: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_fields: list[str] = field(default_factory=list)

    def as_message(self) -> str:
        parts = []
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        return " | ".join(parts) or "ok"


class SQLValidator:
    def __init__(self, resolver: ContextResolver, max_rows: int):
        self.r = resolver
        self.max_rows = max_rows

    def validate(
        self,
        sql: str,
        *,
        start_time: int | None,
        end_time: int | None,
        stream_type: str = "logs",
    ) -> ValidationResult:
        res = ValidationResult(ok=True)

        # 1. stream from FROM
        m = _FROM_RE.search(sql)
        if not m:
            res.ok = False
            res.errors.append("no FROM clause / stream could be identified")
            return res
        stream = m.group(1)
        res.stream = stream

        # 2. schema (fail-closed)
        try:
            schema: StreamSchema = self.r.resolve_schema(stream, stream_type)
        except ResolveError as e:
            res.ok = False
            res.errors.append(str(e))
            return res

        # 3. field existence
        used = self._referenced_fields(sql, stream)
        res.used_fields = sorted(used)
        unknown = [f for f in used if not schema.has_field(f)]
        if unknown:
            res.ok = False
            res.errors.append(
                f"unknown field(s) not in schema of '{stream}': {unknown}. "
                f"Known fields: {sorted(schema.fields)}"
            )

        # 4. bounded time range
        if start_time is None or end_time is None:
            res.ok = False
            res.errors.append("query has no bounded time range (start_time/end_time required)")
        elif end_time <= start_time:
            res.ok = False
            res.errors.append("end_time must be greater than start_time")

        # 5. row limit / SELECT *
        if re.search(r"select\s+\*", sql, re.I) and not re.search(r"\blimit\b", sql, re.I):
            res.warnings.append(
                f"SELECT * without LIMIT; results will be clamped to {self.max_rows} rows"
            )

        return res

    def _referenced_fields(self, sql: str, stream: str) -> set[str]:
        # drop string literals so their contents aren't treated as identifiers
        cleaned = _STRING_LIT_RE.sub("''", sql)
        found: set[str] = set()
        for quoted, bare in _IDENT_RE.findall(cleaned):
            tok = quoted or bare
            low = tok.lower()
            if quoted:  # explicitly quoted identifier -> treat as field
                if low != stream.lower():
                    found.add(tok)
                continue
            if low in _SQL_NOISE or low == stream.lower():
                continue
            if tok.isdigit():
                continue
            found.add(tok)
        return found


class VRLValidator:
    """VRL validation via OpenObserve's dry-run endpoint (real compile)."""

    def __init__(self, client: ReadOnlyClient):
        self.c = client

    def validate(self, vrl: str, sample_events: list[dict] | None = None) -> ValidationResult:
        res = ValidationResult(ok=True)
        r = self.c.validate_vrl(vrl, sample_events)
        if not r.ok:
            res.ok = False
            res.errors.append(_clean_err(r.error))
        return res


class PromQLValidator:
    """PromQL syntax validation via the format_query endpoint."""

    def __init__(self, client: ReadOnlyClient):
        self.c = client

    def validate(self, promql: str) -> ValidationResult:
        res = ValidationResult(ok=True)
        r = self.c.validate_promql(promql)
        if not r.ok:
            res.ok = False
            res.errors.append(_clean_err(r.error))
        elif isinstance(r.data, dict) and r.data.get("status") == "error":
            res.ok = False
            res.errors.append(str(r.data.get("error", "promql error")))
        return res


class RegexValidator:
    """Regex validation using Python's engine (local, no network)."""

    @staticmethod
    def validate(pattern: str) -> ValidationResult:
        res = ValidationResult(ok=True)
        try:
            re.compile(pattern)
        except re.error as e:
            res.ok = False
            res.errors.append(f"invalid regex: {e}")
        return res


def _clean_err(err: str | None) -> str:
    if not err:
        return "validation failed"
    # strip HTTP status prefix like "[400] "
    body = re.sub(r"^\[\d+\]\s*", "", err).strip()
    # if the body is JSON with a message/error field, extract it
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            msg = obj.get("message") or obj.get("error")
            if msg:
                return str(msg).strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return body

