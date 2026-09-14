"""Phase 10: user-experience helpers for the CLI.

Pure presentation logic — suggested prompts (grouped by intent), a compact
turn-context footer (org / stream / time range), streamed-progress lines, and
feedback prompts. Kept out of `agent.py` so the runtime stays UI-agnostic and
these strings are easy to test/tweak.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Suggested prompts, grouped to mirror the Intent Router categories. These give a
# new user runnable starting points (checklist: "suggested prompts available").
SUGGESTED_PROMPTS: dict[str, list[str]] = {
    "query": [
        "Write SQL to count errors by status over the last 15 minutes.",
        "Convert this Datadog query to OpenObserve SQL: sum:nginx.requests{status:500}.as_count()",
        "Write a regex to redact email addresses.",
    ],
    "investigation": [
        "Why was my last alert fired?",
        "Explain why this SQL query is slow.",
    ],
    "resource": [
        "Create an alert for a 5% error rate over 10 minutes.",
        "Create a reusable VRL function to redact emails.",
        "Build a dashboard panel from my default stream.",
    ],
    "schema_vrl_promql": [
        "Map my default stream schema.",
        "Write VRL to parse JSON from my nginx logs.",
        "Write PromQL for pods using more than 80% CPU.",
    ],
}


def render_suggestions() -> str:
    lines = ["Suggested prompts:"]
    labels = {
        "query": "Queries",
        "investigation": "Investigation",
        "resource": "Create (gated)",
        "schema_vrl_promql": "Schema / VRL / PromQL",
    }
    for key, items in SUGGESTED_PROMPTS.items():
        lines.append(f"  {labels.get(key, key)}:")
        for it in items:
            lines.append(f"    - {it}")
    return "\n".join(lines)


def _fmt_us(ts_us: int) -> str:
    """Format an epoch-microseconds timestamp as a short UTC string."""
    try:
        dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        return str(ts_us)


def render_time_range(rng: tuple[int, int] | None) -> str | None:
    if not rng:
        return None
    start, end = rng
    return f"{_fmt_us(start)} → {_fmt_us(end)}"


def render_context_footer(*, org: str, streams: list[str],
                          time_range: tuple[int, int] | None,
                          scan_records: int | None,
                          scan_budget: int | None) -> str:
    """One-line, verifiable summary of what the turn actually touched.

    Shows org, stream(s), and time range used (checklist: "Show organization,
    stream, and time range used"). Only rendered when a tool actually ran.
    """
    parts = [f"org={org}"]
    if streams:
        parts.append("stream=" + ",".join(streams))
    tr = render_time_range(time_range)
    if tr:
        parts.append(f"range={tr}")
    if scan_records is not None:
        over = scan_budget and scan_records > scan_budget
        flag = " !OVER-BUDGET" if over else ""
        parts.append(f"scanned={scan_records}{flag}")
    return "· context: " + "  ".join(parts)


def render_progress(event: str, **info) -> str | None:
    """Turn an agent progress event into a short status line for the CLI.

    Enables streamed intermediate progress for longer investigations
    (checklist: "Stream intermediate progress").
    """
    if event == "tool_start":
        args = info.get("args") or {}
        hint = ""
        if info.get("tool") == "search" and "sql" in args:
            hint = f": {str(args['sql'])[:70]}"
        return f"  … {info.get('tool')}{hint}"
    if event == "tool_end":
        ok = info.get("ok")
        tag = "ok" if ok else "error"
        extra = ""
        if info.get("scan_records") is not None:
            extra = f", scanned={info.get('scan_records')}"
        return f"  ↳ {info.get('tool')} {tag} ({info.get('duration_ms', 0)}ms{extra})"
    return None


HELP_TEXT = """\
Ask a question in natural language, or use a command:

  /suggest                 show suggested prompts
  /sql <query>             run a bounded SQL query (validated, editable before run)
  /copy [n]                print the n-th generated artifact (SQL/VRL/PromQL/regex)
                           from the last turn as raw text for copying
  /context                 show org/stream/time range used by the last turn
  /feedback up|down [note] rate the last answer (optionally with a correction)
  /changes                 list pending changes (with diffs)
  /approve <id> [phrase]   approve a change (elevated needs a phrase naming it)
  /reject  <id>            reject a change
  /run     <id>            execute an approved change (dry-run unless configured)
  /help                    show this help
  exit                     quit
"""
