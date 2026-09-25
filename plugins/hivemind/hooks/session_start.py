#!/usr/bin/env python3
"""Restore a validated session-scoped pin as Codex SessionStart context."""
import json
import os
import pathlib
import re
import subprocess
import sys

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = os.environ.get("CODEX_THREAD_ID", "")
    helper = pathlib.Path(__file__).resolve().parents[1] / "skills/hivemind/scripts/hivemind-project.py"
    project = ""
    if session_id:
        env = dict(os.environ, HIVEMIND_SESSION_ID=session_id)
        try:
            result = subprocess.run([sys.executable, str(helper), "--show"], env=env,
                                    capture_output=True, text=True, timeout=3, check=True)
            value = json.loads(result.stdout).get("project")
            if isinstance(value, str) and NAME.fullmatch(value):
                project = value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if project:
        message = ("Hivemind project for this session: %s. Pass project=%s on every "
                   "Hivemind MCP call. The project echoed in each tool response is authoritative."
                   % (project, project))
    else:
        message = ("Hivemind has no project pinned for this session. Before using its MCP tools, "
                   "use the hivemind-project skill to list projects, ask the user which to use, "
                   "and pin their choice. Always pass project=<name> explicitly on MCP calls.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": message}}))


if __name__ == "__main__":
    main()
