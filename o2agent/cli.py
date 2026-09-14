"""Interactive CLI for O2 Assistant.

Usage:
    python -m o2agent.cli            # new session
    python -m o2agent.cli <session>  # resume an existing session

Free-text questions are answered by the agent. Commands (see /help) provide the
Phase 10 UX: suggested prompts, streamed progress, a context footer (org/stream/
time range), copyable/editable queries, the confirmation flow, and feedback.

Writes are dry-run by default. The agent can only PROPOSE changes; approval and
execution are your explicit actions here.
"""
from __future__ import annotations

import sys

from .agent import Agent
from .config import Settings
from .gate import GateError
from . import ux


def _print_progress(event: str, **info) -> None:
    line = ux.render_progress(event, **info)
    if line:
        print(line)


def _show_context(agent: Agent) -> None:
    turn = agent.last_turn
    if not turn or not turn.tools_called:
        print("(no tools were used in the last turn)")
        return
    print(ux.render_context_footer(
        org=turn.org, streams=turn.streams, time_range=turn.time_range,
        scan_records=turn.scan_records, scan_budget=agent.s.scan_records_budget,
    ))


def _copy_artifact(agent: Agent, parts: list[str]) -> None:
    turn = agent.last_turn
    arts = turn.artifacts if turn else []
    if not arts:
        print("(no generated queries/artifacts in the last turn)")
        return
    idx = 1
    if len(parts) > 1:
        try:
            idx = int(parts[1])
        except ValueError:
            print("usage: /copy [n]")
            return
    if not (1 <= idx <= len(arts)):
        print(f"(no artifact #{idx}; the last turn produced {len(arts)})")
        return
    art = arts[idx - 1]
    print(f"--- artifact #{idx} [{art.kind}, {art.status}] "
          f"(copy the block below) ---")
    print(art.text)
    print("--- end ---")


def _run_sql(agent: Agent, query: str) -> None:
    """Edit-and-run: execute a user-supplied SQL through the validator gate,
    applying a bounded default time range (checklist: allow users to edit
    queries before execution)."""
    if not query.strip():
        print("usage: /sql <SELECT ... FROM \"stream\" ...>")
        return
    start, end = agent.resolver.default_range_us("interactive")
    result = agent.registry.call("search", {
        "sql": query, "start_time": start, "end_time": end, "size": 20,
    })
    if not result.ok:
        print(f"rejected/error: {result.error}")
        return
    hits = result.data.get("hits", []) if isinstance(result.data, dict) else result.data
    n = len(hits) if isinstance(hits, list) else "?"
    print(f"ok: {n} row(s), scanned={result.scan_records} "
          f"(range {ux.render_time_range((start, end))})")
    for row in (hits or [])[:10]:
        print(f"  {row}")
    if result.meta.get("warnings"):
        for w in result.meta["warnings"]:
            print(f"  ! {w}")


def _feedback(agent: Agent, session_id: str, parts: list[str]) -> None:
    if len(parts) < 2 or parts[1].lower() not in ("up", "down"):
        print("usage: /feedback up|down [note]")
        return
    rating = parts[1].lower()
    note = " ".join(parts[2:]) if len(parts) > 2 else ""
    excerpt = ""
    msgs = agent.mem.load_messages(session_id)
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("content"):
            excerpt = m["content"]
            break
    agent.mem.record_feedback(session_id, rating, note, excerpt)
    print(f"recorded feedback: {rating}" + (f" ({note})" if note else ""))


def _show_changes(agent: Agent) -> None:
    pend = list(agent.gate._changes.values())
    if not pend:
        print("(no changes)")
        return
    for c in pend:
        print(f"  {c.id}  [{c.tier.value}] {c.state.value}  {c.action} {c.resource_kind}")
        for dl in c.diff.splitlines():
            print(f"      {dl}")


def _handle_command(agent: Agent, session_id: str, line: str) -> bool:
    """Return True if the line was a recognized command."""
    parts = line.split()
    cmd = parts[0].lower()
    if cmd in ("/help", "/?"):
        print(ux.HELP_TEXT)
        return True
    if cmd == "/suggest":
        print(ux.render_suggestions())
        return True
    if cmd == "/context":
        _show_context(agent)
        return True
    if cmd == "/copy":
        _copy_artifact(agent, parts)
        return True
    if cmd == "/sql":
        _run_sql(agent, line[len(parts[0]):].strip())
        return True
    if cmd == "/feedback":
        _feedback(agent, session_id, parts)
        return True
    if cmd == "/changes":
        _show_changes(agent)
        return True
    if cmd == "/approve":
        if len(parts) < 2:
            print("usage: /approve <id> [confirming phrase]")
            return True
        phrase = " ".join(parts[2:]) if len(parts) > 2 else None
        try:
            c = agent.gate.approve(parts[1], elevated_confirmation=phrase)
            print(f"approved {c.id} ({c.tier.value}). Use /run {c.id} to execute.")
        except GateError as e:
            print(f"cannot approve: {e}")
        return True
    if cmd == "/reject":
        try:
            c = agent.gate.reject(parts[1])
            print(f"rejected {c.id}")
        except (GateError, IndexError) as e:
            print(f"cannot reject: {e}")
        return True
    if cmd == "/run":
        try:
            c = agent.gate.execute(parts[1])
            r = c.result
            tag = "DRY-RUN" if (r and r.dry_run) else "LIVE"
            print(f"[{tag}] {c.id} -> {c.state.value}"
                  + (f" (error: {r.error})" if r and r.error else ""))
        except (GateError, IndexError) as e:
            print(f"cannot run: {e}")
        return True
    return False


def main() -> None:
    settings = Settings.load()
    agent = Agent(settings)

    if len(sys.argv) > 1:
        session_id = sys.argv[1]
        print(f"# resuming session {session_id}")
    else:
        session_id = agent.start_session()
        print(f"# new session {session_id} (org={settings.org}, model={settings.llm_model})")

    print("# type /help for commands, or just ask a question. 'exit' to quit.\n")
    print(ux.render_suggestions())
    print()
    try:
        while True:
            try:
                user = input("you> ").strip()
            except EOFError:
                break
            if user.lower() in {"exit", "quit", ":q"}:
                break
            if not user:
                continue
            if user.startswith("/"):
                if _handle_command(agent, session_id, user):
                    continue
            answer = agent.chat(session_id, user, on_event=_print_progress)
            print(f"\no2 > {answer}\n")
            _show_context(agent)
            print("# rate this answer with /feedback up|down [note]\n")
    finally:
        usage = agent.mem.session_usage(session_id)
        agent.close()
        print(f"\n# session {session_id} saved. "
              f"tokens={usage['total_tokens']} cost=${usage['cost_usd']} "
              f"llm_calls={usage['llm_calls']}")


if __name__ == "__main__":
    main()
