"""Structured logging & telemetry (Phase 1).

Emits one JSON object per event to a log file (and optionally stderr). Events
cover agent requests, LLM calls, and tool calls, with latency, errors, token
usage, and cost.

Redaction: values are scrubbed for anything resembling credentials before being
logged. Telemetry/log *content* is never logged verbatim (only summaries/counts)
to avoid leaking secrets or re-injecting untrusted data.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

_DEFAULT_LOG = Path.home() / ".o2agent" / "events.log"

# patterns that must never appear in logs
_REDACT_PATTERNS = [
    re.compile(r"(?i)\b(basic|bearer)\s+[A-Za-z0-9+/=._-]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bo2oi_[A-Za-z0-9]+"),
    re.compile(r"(?i)(authorization|api[_-]?key|password|token)\"?\s*[:=]\s*\"?[^\s\",}]+"),
]


def redact(value: Any) -> Any:
    """Recursively scrub credential-like substrings from a value."""
    if isinstance(value, str):
        out = value
        for pat in _REDACT_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Telemetry:
    def __init__(self, log_path: str | Path | None = None, echo_stderr: bool = False):
        self.path = Path(log_path) if log_path else _DEFAULT_LOG
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._echo = echo_stderr
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "event": event}
        record.update(redact(fields))
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        if self._echo:
            print(line, file=sys.stderr)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


# --- cost model -----------------------------------------------------------

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  price_in: float, price_out: float) -> float:
    """Cost in USD. price_* are USD per 1M tokens."""
    return round(
        (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6
    )
