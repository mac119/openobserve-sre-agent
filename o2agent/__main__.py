"""Unified entry point: `python -m o2agent <command>`.

Sub-commands (the CLI is retained as one of them):

    chat     interactive CLI chat (default)      -> o2agent.cli
    serve    run the HTTP/SSE API server         -> o2agent.server
    smoke    read-only smoke test                -> o2agent.smoke
    golden   golden-set evaluation               -> o2agent.golden

Any extra args are forwarded to the sub-command. Existing direct entry points
(`python -m o2agent.cli`, `python -m o2agent.server`, …) still work.
"""
from __future__ import annotations

import sys

_COMMANDS = {"chat", "serve", "smoke", "golden"}


def main() -> None:
    argv = sys.argv[1:]
    cmd = argv[0] if argv and argv[0] in _COMMANDS else "chat"
    # forward remaining args to the sub-command
    sys.argv = [sys.argv[0]] + (argv[1:] if argv and argv[0] in _COMMANDS else argv)

    if cmd == "serve":
        from .server import main as run
    elif cmd == "smoke":
        from .smoke import main as run
    elif cmd == "golden":
        from .golden import main as run
    else:  # chat
        from .cli import main as run
    run()


if __name__ == "__main__":
    main()
