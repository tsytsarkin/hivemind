#!/usr/bin/env python3
"""Best-effort, session-scoped bus listener for hooks and harnesses without Monitor.

No third-party modules. Only a previously pinned project may be joined. Listener credentials
stay in the child's environment, never in argv or the state file. Messages are stored locally;
hook context contains only a count/path, not message bodies.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLUG = re.compile(r"[^A-Za-z0-9_-]")
PROTO = "2026-07-28"
RETRY_SECONDS = 30


def _session(event, platform):
    value = event.get("session_id") or os.environ.get(
        "CODEX_THREAD_ID" if platform == "codex" else "CLAUDE_CODE_SESSION_ID", "")
    return value if isinstance(value, str) and value else ""


def _paths(session, platform):
    slug = SLUG.sub("-", session)[:100]
    base = Path.home() / ".hivemind"
    return base / ("session-%s.json" % slug), base / ("%s-bus" % platform) / slug


def _project(pin_path):
    try:
        value = json.loads(pin_path.read_text()).get("project")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) and NAME.fullmatch(value) else ""


def _endpoint(platform):
    if platform == "claude":
        base = (os.environ.get("HIVEMIND_SERVER_URL")
                or os.environ.get("CLAUDE_PLUGIN_OPTION_SERVER_URL", "")).rstrip("/")
        url = base + "/mcp"
    else:
        try:
            configured = os.environ.get("HIVEMIND_SERVER_URL", "").rstrip("/")
            manifest = Path(__file__).resolve().parents[3] / ".mcp.json"
            if manifest.is_file():
                config = json.loads(manifest.read_text())
                url = config["mcpServers"]["hivemind"]["url"]
            else:
                # The manual fallback is installed under ~/.hivemind, outside the plugin tree.
                # Use the root URL saved by `hivemind-codex configure` or an explicit shell URL.
                saved = Path.home() / ".hivemind/codex-server.json"
                base = json.loads(saved.read_text())["server_url"] if saved.is_file() else configured
                url = base.rstrip("/") + "/mcp" if isinstance(base, str) and base else ""
            if configured and url != configured + "/mcp":
                return ""
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or not parts.path.endswith("/mcp"):
        return ""
    return url


def _token(platform, endpoint):
    """Use the host credential, or Codex's private setup token for hook-only bus access."""
    token = os.environ.get("HIVEMIND_TOKEN") or (os.environ.get("CLAUDE_PLUGIN_OPTION_API_TOKEN", "")
                                              if platform == "claude" else "")
    if token or platform != "codex" or not endpoint:
        return token
    state = Path.home() / ".hivemind"
    try:
        server = json.loads((state / "codex-server.json").read_text())["server_url"]
        path = state / "codex-token"
        info = path.lstat()
        if (not isinstance(server, str) or server.rstrip("/") + "/mcp" != endpoint
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return ""
        return path.read_text().strip()
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _rpc(url, token, name, arguments):
    params = {"name": name, "arguments": arguments,
              "_meta": {"io.modelcontextprotocol/protocolVersion": PROTO,
                        "io.modelcontextprotocol/clientInfo": {"name": "hivemind-bus-autojoin",
                                                             "version": "1.3.0"},
                        "io.modelcontextprotocol/clientCapabilities": {}}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": params}).encode()
    req = Request(url, data=body, headers={"Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/call",
                    "Mcp-Name": name}, method="POST")
    with urlopen(req, timeout=3) as response:
        data = response.read(1024 * 1024).decode("utf-8")
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            rows = [s[5:].strip() for s in data.splitlines() if s.startswith("data:")]
            data = rows[-1] if rows else "{}"
    result = json.loads(data).get("result", {})
    value = result.get("structuredContent")
    if value is None:
        value = json.loads(result["content"][0]["text"])
    return value if isinstance(value, dict) and value.get("ok") is not False else {}


def _port(parts):
    return parts.port or (443 if parts.scheme in ("https", "wss") else 80)


def _owned_process(state, listener):
    pid, marker = state.get("pid"), state.get("marker")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(marker, str) or not marker:
        return False
    try:
        cmd = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=1, check=True).stdout
        return str(listener) in cmd and "--instance-id " + marker in cmd
    except (OSError, subprocess.SubprocessError):
        return False


def _read_state(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, state):
    tmp = path.with_name(path.name + ".new-" + secrets.token_hex(4))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(state, output)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def _stop(state, listener):
    if _owned_process(state, listener):
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except OSError:
            pass


def _notice(inbox, state_path, state):
    """Notify only for unread messages, including those in the rotated inbox."""
    ids = []
    for path in (inbox.with_name(inbox.name + ".1"), inbox):
        try:
            with path.open("rb") as source:
                for line in source:
                    try:
                        frame = json.loads(line)
                        if isinstance(frame, dict) and frame.get("type") in ("message", "broadcast"):
                            mid = frame.get("id")
                            if isinstance(mid, (str, int)) and mid:
                                ids.append(str(mid))
                    except ValueError:
                        pass
        except OSError:
            pass
    if not ids:
        return ""
    seen = state.get("seen_message") if state.get("seen_inbox") == inbox.name else None
    # If the cursor has fallen off both bounded inbox files, announce what remains instead of
    # silently dropping the new messages. The bus is not a durable archive.
    start = 0
    if seen:
        for i in range(len(ids) - 1, -1, -1):
            if ids[i] == seen:
                start = i + 1
                break
    pending = len(ids) - start
    if not pending:
        return ""
    state["seen_message"] = ids[-1]
    state["seen_inbox"] = inbox.name
    try:
        _save(state_path, state)
    except OSError:
        pass
    return ("Hivemind bus: %d new message(s) from peer agents saved at %s. Read every full "
            "message now, work on its request, and reply to its sender via the MCP bus_send "
            "tool. Ask the user before deleting files or other destructive actions."
            % (pending, inbox))


def run(event, platform="codex", mode="ensure"):
    session = _session(event, platform)
    if not session:
        return ""
    pin_path, state_dir = _paths(session, platform)
    listener = Path(__file__).resolve().with_name("bus-listen.py")
    project = _project(pin_path)
    if not project and mode != "stop":
        return ""
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(str(state_dir / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return ""
    with os.fdopen(lock, "r") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        state_path = state_dir / "listener.json"
        inbox = state_dir / ("inbox-%s.jsonl" % project)
        state = _read_state(state_path)
        def notice():
            return _notice(inbox, state_path, state)
        alive = _owned_process(state, listener)
        if mode == "stop":
            _stop(state, listener)
            _save(state_path, {})
            return ""
        if mode == "check":
            return notice()
        endpoint = _endpoint(platform)
        token = _token(platform, endpoint)
        if alive and state.get("project") != project:
            _stop(state, listener)
            alive = False
        if alive and endpoint and state.get("project") == project and state.get("endpoint") == endpoint:
            # A running listener already has its restricted key. Do not claim it has left the
            # bus when the bearer token is temporarily unavailable on a later prompt.
            return notice()
        if not endpoint or not token or not listener.is_file():
            reason = ("server URL is missing or differs from configured MCP endpoint" if not endpoint
                      else "no token; configure Codex or set Claude plugin api_token" if not token
                      else "bundled bus-listen.py is missing")
            return ("Hivemind bus not joined for project=%s: %s. %s" %
                    (project, reason, notice())).strip()
        if alive:
            _stop(state, listener)
        if (not alive and state.get("project") == project
                and state.get("endpoint") == endpoint
                and time.time() - state.get("attempted_at", 0) < RETRY_SECONDS):
            return ("Hivemind bus join pending for project=%s; retrying shortly. %s"
                    % (project, notice())).strip()
        cursor = ({k: state[k] for k in ("seen_message", "seen_inbox") if k in state}
                  if state.get("project") == project else {})
        state = {"project": project, "endpoint": endpoint, "attempted_at": time.time(), **cursor}
        _save(state_path, state)
        label = "%s-%s" % (platform, SLUG.sub("-", session)[:48])
        try:
            reply = _rpc(endpoint, token, "bus_connect", {"project": project, "label": label})
            ws = reply.get("ws_url", "")
            key = reply.get("listen_key", "")
            url = urlsplit(ws)
            dest = urlsplit(endpoint)
            if (not isinstance(key, str) or not key or url.scheme not in ("ws", "wss")
                    or url.hostname != dest.hostname or _port(url) != _port(dest)
                    or url.path != "/p/%s/bus/ws" % project
                    or reply.get("project", project) != project):
                return ("Hivemind bus not joined for project=%s: bus_connect returned no usable "
                        "WebSocket/listen key. %s" % (project, notice())).strip()
            marker = secrets.token_hex(12)
            child_env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "HIVEMIND_LISTEN_KEY": key}
            proc = subprocess.Popen([sys.executable, str(listener), "--url", ws,
                                     "--key-env", "HIVEMIND_LISTEN_KEY", "--inbox", str(inbox),
                                     "--instance-id", marker], env=child_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True, start_new_session=True)
            state.update({"pid": proc.pid, "marker": marker, "peer": label})
            _save(state_path, state)
            return ("Hivemind bus joined as %s for project=%s. Messages are saved in %s. "
                    "Codex/Claude without Monitor does not wake an idle chat; read new inbox "
                    "messages and reply via MCP at the next prompt." % (label, project, inbox))
        except HTTPError as error:
            return ("Hivemind bus not joined for project=%s: MCP answered HTTP %d. Check your "
                    "server/token and project access. %s" % (project, error.code, notice())).strip()
        except (URLError, OSError, ValueError, KeyError, TypeError):
            return ("Hivemind bus not joined for project=%s: cannot reach the MCP bus or start "
                    "its listener. %s" % (project, notice())).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("codex", "claude"), default="codex")
    parser.add_argument("--mode", choices=("ensure", "check", "after-pin", "stop"), default="ensure")
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (ValueError, OSError):
        event = {}
    # Any Bash call may have loaded an existing pin, including a shell wrapper or a resumed
    # session. The lock in run() makes registration idempotent, so do not guess from command text.
    message = run(event, args.platform, args.mode)
    if args.mode != "stop":
        name = event.get("hook_event_name", "UserPromptSubmit")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name,
                                                 "additionalContext": message}}))


if __name__ == "__main__":
    main()
