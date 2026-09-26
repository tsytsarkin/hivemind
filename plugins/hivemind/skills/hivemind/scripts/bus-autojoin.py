#!/usr/bin/env python3
"""Best-effort, session-scoped bus listener for hooks and harnesses without Monitor.

No third-party modules. Only a previously pinned project may be joined. Listener credentials
stay in the child's environment, never in argv or the state file. Messages are stored locally;
hook context contains only a count/path, not message bodies.
"""
import argparse
import fcntl
import hashlib
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
# What survives a listener being stopped or replaced: where this session had read up to. Losing it
# is not cosmetic — an empty cursor makes _notice announce every id in both inbox generations as
# new, so a resumed session opens by telling the agent to read and act on hundreds of old messages.
CURSOR_KEYS = ("seen_message", "seen_inbox", "seen_offset")


# The pin helper's session_id() chain, in its order. Duplicated rather than imported: both scripts
# are standalone stdlib files installed side by side in $HOME/.hivemind, and importing one from the
# other through a hyphenated filename would make a join depend on the pin helper being present.
# test_the_pin_key_is_resolved_the_same_way_as_the_helper keeps the two lists in step.
SESSION_VARS = ("HIVEMIND_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID")


def _session(event):
    """This conversation's id: the hook's own event first, then the pin helper's variable chain.

    The id is the KEY of the pin file this script then reads, so resolving it by a different rule
    than the helper used to WRITE that file makes a real pin look like no pin — and the session
    silently never joins the bus. That is why the platform does not select a variable here, though
    it once did: `platform` names the bus label and the state directory, not the key.
    """
    value = event.get("session_id")
    if isinstance(value, str) and value:
        return value
    for name in SESSION_VARS:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _chat_session(host_session):
    """Bound long host IDs without ever conflating two IDs at the 64-byte boundary."""
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", host_session):
        return host_session
    # Strip `_` as well as `-`: the server requires ^[a-z0-9] for the FIRST character, so a host id
    # beginning with an underscore produced a slug it rejected. chat_connect then answered ok:false,
    # _rpc turned that into {}, and the session degraded silently to the ephemeral bus with no
    # durable mailbox — the one failure here that costs offline messages rather than announcing it.
    prefix = re.sub(r"[^a-z0-9_-]", "-", host_session.lower()).strip("-_")[:47] or "sid"
    suffix = hashlib.sha256(host_session.encode()).hexdigest()[:16]
    return prefix + "-" + suffix


def _paths(session, platform):
    cleaned = SLUG.sub("-", session)
    slug = (cleaned if cleaned == session and len(cleaned) <= 100 else
            cleaned[:83] + "-" + hashlib.sha256(session.encode()).hexdigest()[:16])
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
            # parents[3] only exists for the copy inside the plugin tree. The manual install lives
            # at $HOME/.hivemind/bus-autojoin.py, which has exactly three parents when $HOME is one
            # level below root (/root in a container or CI) — indexing there raised IndexError, the
            # except below swallowed it as "no endpoint", and the ~/.hivemind/codex-server.json
            # fallback this branch exists for was never reached. Sliced, so a short path is simply
            # not a manifest.
            parents = Path(__file__).resolve().parents
            manifest = next(iter(parents[3:4]), None)
            manifest = manifest / ".mcp.json" if manifest is not None else None
            if manifest is not None and manifest.is_file():
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
                                                             "version": "1.4.0"},
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
    """Is the listener this state file describes still running?

    Matched on the BASENAME plus the instance marker, never the resolved path. There are two copies
    of bus-listen.py — the plugin tree's and the one the skill installs in $HOME/.hivemind — and the
    hooks run one while the documented agent command runs the other. Matching the full path made
    each copy blind to the other's listener: `_stop` became a no-op, a second listener spawned onto
    the same inbox (so every message was recorded and counted twice), and the first was orphaned
    with its pid overwritten in listener.json, beyond the reach of even `--mode stop` at SessionEnd.
    It then repeated every RETRY_SECONDS for the rest of the session.

    The marker is 24 random hex characters minted by the spawn that wrote this state file, so it is
    the identity; the basename only rejects a pid that has since been recycled onto something else.
    """
    pid, marker = state.get("pid"), state.get("marker")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(marker, str) or not marker:
        return False
    try:
        cmd = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=1, check=True).stdout
        return listener.name in cmd and "--instance-id " + marker in cmd
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


def _frames(path, start=0):
    """Message ids in one inbox generation from a byte offset, with the offset after the last one.

    A trailing partial line is a frame the listener is still writing: stop before it and leave the
    offset where it began, so the next call reads it whole rather than discarding it.
    """
    ids, offset = [], start
    try:
        with path.open("rb") as source:
            source.seek(start)
            for line in source:
                if not line.endswith(b"\n"):
                    break
                offset += len(line)
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    kind = frame.get("type")
                    mid = frame.get("ts") if kind == "hello" and frame.get("v") == 2 else frame.get("id")
                    if isinstance(mid, (str, int, float)) and mid:
                        if kind in ("message", "broadcast"):
                            ids.append(str(mid))
                        elif kind == "chat":
                            ids.append("chat:" + str(mid))
                        elif kind == "hello" and frame.get("v") == 2:
                            ids.append("resume:" + str(mid))
    except OSError:
        return [], start
    return ids, offset


def _notice(inbox, state_path, state):
    """Notify only for unread messages, including those in the rotated inbox.

    Resumes from a stored byte offset. This runs on every Bash tool call and every prompt under an
    8 s hook timeout, while the inbox is bounded at INBOX_MAX_BYTES with one rotated generation
    kept — so re-reading both generations meant up to ~8 MiB of json.loads per invocation to
    recompute a count that only changes when a frame arrives.

    The offset is only trusted while it still addresses the same file: a rotation replaces the
    inbox with a shorter one, and then the id-based scan below is the exact behaviour this
    fast path replaces, so the count stays right across the rotation rather than re-announcing it.
    """
    start = state.get("seen_offset")
    resumable = (state.get("seen_inbox") == inbox.name and isinstance(start, int)
                 and not isinstance(start, bool) and start >= 0)
    if resumable:
        try:
            resumable = inbox.stat().st_size >= start
        except OSError:
            resumable = False
    if resumable:
        fresh, offset = _frames(inbox, start)
        pending, last = len(fresh), fresh[-1] if fresh else None
    else:
        rotated, _ = _frames(inbox.with_name(inbox.name + ".1"))
        current, offset = _frames(inbox)
        ids = rotated + current
        if not ids:
            return ""
        seen = state.get("seen_message") if state.get("seen_inbox") == inbox.name else None
        # If the cursor has fallen off both bounded inbox files, announce what remains instead of
        # silently dropping the new messages. The bus is not a durable archive.
        index = 0
        if seen:
            for i in range(len(ids) - 1, -1, -1):
                if ids[i] == seen:
                    index = i + 1
                    break
        pending, last = len(ids) - index, ids[-1]
    if not pending:
        return ""
    durable = any(str(value).startswith(("chat:", "resume:"))
                  for value in (fresh if resumable else ids[index:]))
    if last:
        state["seen_message"] = last
    state["seen_inbox"] = inbox.name
    state["seen_offset"] = offset
    try:
        _save(state_path, state)
    except OSError:
        pass
    if durable:
        return ("Hivemind durable chat: %d new notification(s) or reconnect at %s. Call "
                "MCP chat_inbox and chat_room_history for relevant rooms now; fetch complete "
                "messages, then chat_mark_read / chat_room_mark_read only after processing. "
                "Server history, not this local file, is authoritative for 24 hours."
                % (pending, inbox))
    return ("Hivemind legacy bus: %d new message(s) from peer agents saved at %s. Read every "
            "full message now, work on its request, and reply via MCP bus_send. "
            "This legacy inbox is ephemeral."
            % (pending, inbox))


def run(event, platform="codex", mode="ensure"):
    session = _session(event)
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
            # The pid and the endpoint go; the read cursor stays. SessionEnd stops the listener but
            # does not delete the inbox, and a --resume reuses this same session id and state
            # directory — with the cursor discarded the next `ensure` announced the entire backlog.
            _save(state_path, {k: state[k] for k in ("project",) + CURSOR_KEYS if k in state})
            return ""
        if mode == "check":
            return notice()
        endpoint = _endpoint(platform)
        token = _token(platform, endpoint)
        if alive and state.get("project") != project:
            _stop(state, listener)
            alive = False
        if (alive and state.get("project") == project
                and (not endpoint or state.get("endpoint") == endpoint)):
            # A running listener already has its restricted key. Do not claim it has left the
            # bus when the bearer token is temporarily unavailable on a later prompt — nor when
            # the ENDPOINT is unresolvable in this particular shell, which is the same situation
            # wearing a different hat: the plugin publishes HIVEMIND_SERVER_URL through
            # CLAUDE_ENV_FILE and CLAUDE_PLUGIN_OPTION_SERVER_URL is hook-only, so the agent
            # running this command by hand can have neither while the listener is happily online.
            # Reporting "not joined" there tells the user they are offline when they are not.
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
        cursor = ({k: state[k] for k in CURSOR_KEYS if k in state}
                  if state.get("project") == project else {})
        state = {"project": project, "endpoint": endpoint, "attempted_at": time.time(), **cursor}
        _save(state_path, state)
        label = "%s-%s" % (platform, SLUG.sub("-", session)[:48])
        chat_session = _chat_session(session)
        step = "chat_connect"
        try:
            reply = {}
            if chat_session:
                try:
                    reply = _rpc(endpoint, token, "chat_connect",
                                 {"project": project, "client": platform,
                                  "session_id": chat_session})
                except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError):
                    # A server without this tool is the WHOLE REASON the fallback below exists, and
                    # it does not answer by returning a reply that lacks a listen_key: it answers
                    # `{"isError": true, "content": [{"text": "Unknown tool: chat_connect"}]}`, so
                    # _rpc's json.loads of that text raises ValueError (a JSON-RPC error object
                    # raises KeyError, and an older proxy may answer by status). Letting any of
                    # those reach the outer handler skipped bus_connect entirely and left the
                    # session with NO listener — measured against a live 1.1.0 server, which
                    # reported only "cannot reach the MCP bus or start its listener".
                    reply = {}
            durable = bool(reply.get("listen_key"))
            if not durable:
                step = "bus_connect"
                # Older deployments or legacy project-only tokens keep their original, explicitly
                # ephemeral bus. Never describe this fallback as offline message delivery.
                reply = _rpc(endpoint, token, "bus_connect", {"project": project, "label": label})
            ws = reply.get("ws_url", "")
            key = reply.get("listen_key", "")
            url = urlsplit(ws)
            dest = urlsplit(endpoint)
            if (not isinstance(key, str) or not key or url.scheme not in ("ws", "wss")
                    or url.hostname != dest.hostname or _port(url) != _port(dest)
                    or url.path != "/p/%s/%s/ws" % (project, "chat" if durable else "bus")
                    or reply.get("project", project) != project):
                return ("Hivemind bus not joined for project=%s: bus_connect returned no usable "
                        "WebSocket/listen key. %s" % (project, notice())).strip()
            step = "start the listener"
            marker = secrets.token_hex(12)
            child_env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "HIVEMIND_LISTEN_KEY": key}
            proc = subprocess.Popen([sys.executable, str(listener), "--url", ws,
                                     "--key-env", "HIVEMIND_LISTEN_KEY", "--inbox", str(inbox),
                                     "--instance-id", marker], env=child_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True, start_new_session=True)
            state.update({"pid": proc.pid, "marker": marker,
                          "peer": reply.get("peer", label) if durable else label,
                          "protocol": "chat" if durable else "legacy-bus"})
            _save(state_path, state)
            if durable:
                return ("Hivemind durable chat joined as %s for project=%s. Call chat_inbox and "
                        "chat_room_history for subscribed rooms NOW to catch up on 24-hour "
                        "server history; local notifications at %s do not wake an idle chat."
                        % (reply.get("peer", label), project, inbox))
            return ("Hivemind legacy ephemeral bus joined as %s for project=%s. Offline "
                    "messages are NOT preserved; local inbox at %s is not server history."
                    % (label, project, inbox))
        except HTTPError as error:
            return ("Hivemind bus not joined for project=%s: %s answered HTTP %d. Check your "
                    "server/token and project access. %s"
                    % (project, step, error.code, notice())).strip()
        except (URLError, OSError, ValueError, KeyError, TypeError) as error:
            # Name the STEP and the exception. One sentence covering three operations and five
            # exception types is why a `chat_connect` that a 1.1.0 server had simply never heard of
            # read as an unreachable bus, and sent the next reader hunting for a routing fault.
            # Type and message only — no traceback and no argument values: this string is injected
            # into an agent's context, and the token lives in the frame that would be dumped.
            return ("Hivemind bus not joined for project=%s: could not %s (%s: %s). %s"
                    % (project, step, type(error).__name__,
                       str(error)[:120], notice())).strip()


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
