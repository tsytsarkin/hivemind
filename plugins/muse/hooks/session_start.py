#!/usr/bin/env python3
"""Restore a validated session-scoped pin as Muse SessionStart context."""
import json
import os
import pathlib
import re
import subprocess
import sys

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

SESSION_VARS = ("MUSE_SESSION_ID", "MUSE_CODE_SESSION_ID", "HIVEMIND_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")

def _find_helper():
    # Try plugin roots in order
    for env in ("MUSE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
        root = os.environ.get(env)
        if root:
            p = pathlib.Path(root) / "skills/hivemind/scripts/hivemind-project.py"
            if p.is_file():
                return p
    # Relative to this file (plugin tree)
    p = pathlib.Path(__file__).resolve().parents[1] / "skills/hivemind/scripts/hivemind-project.py"
    if p.is_file():
        return p
    # Installed copy
    p = pathlib.Path.home() / ".hivemind/hivemind-project.py"
    return p

def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        # Try muse-specific env, then generic chain
        for var in SESSION_VARS:
            val = os.environ.get(var, "")
            if val:
                session_id = val
                break
        else:
            session_id = ""
    # Also write MUSE_ENV_FILE if present (muse equivalent of CLAUDE_ENV_FILE)
    project = ""
    helper = _find_helper()
    if session_id and helper.is_file():
        env = dict(os.environ, HIVEMIND_SESSION_ID=session_id, MUSE_SESSION_ID=session_id)
        try:
            result = subprocess.run([sys.executable, str(helper), "--show"], env=env,
                                    capture_output=True, text=True, timeout=3, check=True)
            value = json.loads(result.stdout).get("project")
            if isinstance(value, str) and NAME.fullmatch(value):
                project = value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    # Publish to session shell if env file present (muse/claude compat)
    for env_file_var in ("MUSE_ENV_FILE", "CLAUDE_ENV_FILE"):
        env_file = os.environ.get(env_file_var)
        if env_file:
            try:
                pathlib.Path(env_file).chmod(0o600)
            except OSError:
                pass
            try:
                with open(env_file, "a") as f:
                    if not os.environ.get("HIVEMIND_SERVER_URL"):
                        # Try plugin config options
                        for opt in ("MUSE_PLUGIN_OPTION_SERVER_URL", "CLAUDE_PLUGIN_OPTION_SERVER_URL"):
                            val = os.environ.get(opt)
                            if val:
                                f.write(f'export HIVEMIND_SERVER_URL="{val}"\n')
                                break
                    if not os.environ.get("HIVEMIND_TOKEN"):
                        for opt in ("MUSE_PLUGIN_OPTION_API_TOKEN", "CLAUDE_PLUGIN_OPTION_API_TOKEN"):
                            val = os.environ.get(opt)
                            if val:
                                f.write(f'export HIVEMIND_TOKEN="{val}"\n')
                                break
                    if not os.environ.get("HIVEMIND_PROJECT") and project:
                        f.write(f'export HIVEMIND_PROJECT="{project}"\n')
            except OSError:
                pass
    if project:
        message = ("Hivemind project for this session: %s. Pass project=%s on every "
                   "Hivemind MCP call. The project echoed in each tool response is authoritative."
                   % (project, project))
    else:
        message = ("Hivemind has no project pinned for this session. Before using its MCP tools, "
                   "use the hivemind-project skill to list projects, ask the user which to use, "
                   "and pin their choice. Always pass project=<name> explicitly on MCP calls.")
    # Muse native hook output format, compatible with claude
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": message}}))

if __name__ == "__main__":
    main()
