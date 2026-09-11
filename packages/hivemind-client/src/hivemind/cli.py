"""`hivemind` CLI — talk to a hivemind project from the shell or from an agent.

Config from env (HIVEMIND_SERVER_URL, HIVEMIND_TOKEN, HIVEMIND_AGENT) or --url/--token/--agent.
All output is JSON on stdout; errors go to stderr with a non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

import httpx

from .client import Client, HivemindError


def _client(args) -> Client:
    url = args.url or os.environ.get("HIVEMIND_SERVER_URL")
    token = args.token or os.environ.get("HIVEMIND_TOKEN")
    if not url or not token:
        _die("set HIVEMIND_SERVER_URL and HIVEMIND_TOKEN (or pass --url/--token). "
             "URL must include the project, e.g. http://host:8787/p/default")
    return Client(url, token, agent=args.agent or os.environ.get("HIVEMIND_AGENT", "cli"))


def _die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _json_arg(s: Optional[str], what: str) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        _die(f"--{what} must be valid JSON: {e}")


# `bus wait` exit codes. A watcher is meant to be run backgrounded and re-armed in a loop, so the
# caller has to be able to tell "drain me" from "nothing yet" without parsing stdout.
WAIT_MESSAGES, WAIT_TIMEOUT = 0, 75          # 75 = EX_TEMPFAIL: try again
# `bus sidecar` adds three more, because re-arming is the wrong answer to all of them: the session
# is gone and must be re-registered, the server cannot be reached at all, or there is no longer an
# agent to wake.
SIDECAR_GONE, SIDECAR_UNREACHABLE, SIDECAR_ORPHANED = 69, 70, 71


def _caps(items) -> dict:
    """--capability NAME  or  --capability NAME={"attr":1}  ->  {name: attrs}"""
    out = {}
    for raw in items or []:
        name, sep, attrs = raw.partition("=")
        if not name.strip():
            _die(f"--capability {raw!r} has an empty name")
        try:
            out[name.strip()] = json.loads(attrs) if sep and attrs.strip() else {}
        except json.JSONDecodeError as e:
            _die(f"--capability {name!r} attrs must be valid JSON: {e}")
    return out


def _refs(items) -> list:
    """--ref accepts a bare node_id or a JSON ref object; anything else is a user error here."""
    out = []
    for raw in items or []:
        s = raw.strip()
        if s.startswith("{"):
            try:
                out.append(json.loads(s))
            except json.JSONDecodeError as e:
                _die(f"--ref {s[:40]!r} is not valid JSON: {e}")
        else:
            out.append(s)
    return out


def _bus(c, args) -> int:
    cmd = args.bus_cmd
    if cmd == "wait":                      # the two with non-zero exits and their own loops
        return _bus_wait(c, args)
    if cmd == "sidecar":
        return _bus_sidecar(c, args)
    a = args
    calls = {
        "hello": ("bus_hello", lambda: {
            "label": a.label, "capabilities": _caps(a.capabilities), "harness": a.harness,
            "interruptible": a.interruptible, "rooms": a.rooms, "ttl": a.ttl}),
        "ping": ("bus_ping", lambda: {
            "session_id": a.session_id, "status": a.status, "ttl": a.ttl,
            "capabilities": _caps(a.capabilities) if a.capabilities else None}),
        "bye": ("bus_bye", lambda: {"session_id": a.session_id}),
        "agents": ("bus_agents", lambda: {"capability": a.capability,
                                          "include_ended": a.include_ended}),
        "capabilities": ("bus_capabilities", dict),
        "rooms": ("bus_rooms", dict),
        "stats": ("bus_stats", dict),
        "reap": ("bus_reap", dict),
        "join": ("bus_join", lambda: {"session_id": a.session_id, "room": a.room}),
        "leave": ("bus_leave", lambda: {"session_id": a.session_id, "room": a.room}),
        "post": ("bus_post", lambda: {
            "session_id": a.session_id, "body": a.body, "room": a.room,
            "to_session": a.to_session, "kind": a.kind, "data": _json_arg(a.data, "data"),
            "refs": _refs(a.refs), "reply_to": a.reply_to}),
        "thread": ("bus_thread", lambda: {"seq": a.seq, "limit": a.limit}),
        "poll": ("bus_poll", lambda: {"session_id": a.session_id, "limit": a.limit,
                                      "after": a.after, "include_self": a.include_self}),
        "peek": ("bus_peek", lambda: {"session_id": a.session_id, "limit": a.limit,
                                      "after": a.after}),
        "ack": ("bus_ack", lambda: {"session_id": a.session_id, "seq": a.seq}),
        "history": ("bus_history", lambda: {"room": a.room, "limit": a.limit,
                                            "before": a.before}),
        "request": ("bus_request", lambda: {
            "session_id": a.session_id, "task": a.task, "needs": a.needs,
            "to_session": a.to_session, "payload": _json_arg(a.payload, "payload"),
            "room": a.room, "refs": _refs(a.refs), "lease_sec": a.lease_sec, "ttl": a.ttl}),
        "claim": ("bus_claim", lambda: {"session_id": a.session_id, "request_id": a.request_id,
                                        "lease_sec": a.lease_sec}),
        "release": ("bus_release", lambda: {"session_id": a.session_id,
                                            "request_id": a.request_id, "reason": a.reason}),
        "respond": ("bus_respond", lambda: {
            "session_id": a.session_id, "request_id": a.request_id,
            "result": _json_arg(a.result, "result") if a.result else None, "error": a.error,
            "refs": _refs(a.refs)}),
        "request-get": ("bus_request_get", lambda: {"request_id": a.request_id}),
        "requests": ("bus_requests", lambda: {"session_id": a.session, "state": a.state,
                                              "claimable_only": a.claimable}),
        "resolve": ("bus_resolve", lambda: {"request_id": a.request_id, "seq": a.seq,
                                            "limit": a.limit}),
        "node-refs": ("bus_node_refs", lambda: {"node_id": a.node_id, "limit": a.limit}),
    }
    if cmd not in calls:
        _die(f"unknown bus command {cmd!r}")
    tool, build_args = calls[cmd]
    _out(c.call(tool, build_args()))
    return 0


def _bus_wait(c, args) -> int:
    """Block until something arrives, print it, exit — that exit is what wakes an agent.

    On a harness that re-invokes an agent when a backgrounded process exits (Claude Code), run
    this with run_in_background and drain with `bus poll` when it returns. Nothing is consumed
    here, so if this process dies the message is still waiting for the agent.
    """
    rooms = [r for r in (args.rooms or "").split(",") if r]
    while True:
        out = c.bus_wait(args.session_id, after=args.after, wait=args.wait, rooms=rooms or None,
                         limit=args.limit, interval=args.interval)
        if out.get("messages"):
            _out(out)
            if not args.follow:
                return WAIT_MESSAGES
            # Advance past what we printed so --follow does not reprint it. This is a display
            # cursor only; the session cursor is still untouched until the agent polls.
            args.after = out["head"]
        elif not args.follow:
            _out(out)
            return WAIT_TIMEOUT


def _harness_pid(explicit: Optional[int]) -> Optional[int]:
    """Whose death means there is no longer anybody to wake.

    NOT our immediate parent. A harness backgrounds us through a shell, and that shell outlives
    the harness — it is simply reparented to init — so watching it would never fire. One level up
    is the process that actually re-invokes the agent. Returns None when there is nothing sensible
    to watch (already reparented, or ps unavailable), in which case the caller skips the check
    rather than watching pid 1, which never dies.
    """
    if explicit is not None:
        return explicit if explicit > 1 else None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(os.getppid())],
                             capture_output=True, text=True, timeout=5)
        pid = int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid if pid > 1 else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)                      # signal 0: existence check, delivers nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                          # exists, just not ours to signal
    return True


def _bus_sidecar(c, args) -> int:
    """Heartbeat while it is quiet, drain and exit only when there is actually news.

    `bus wait` exits on every timeout. Where process exit IS the interrupt, that spends one of the
    agent's turns to tell it nothing happened, and the agent then has to spawn a replacement — so
    the steady state of an idle bus is a stream of empty wake-ups. This absorbs timeouts instead:
    ping and keep blocking. Two consequences worth the extra loop:

      - the ping is no longer something the agent has to remember between wake-ups, so a session
        stops dying of a missed heartbeat while its watcher is healthy;
      - the exit always carries drained messages, so waking up costs one turn and no follow-up
        poll, and the cursor has already moved — nothing to pass as --after next time.

    Runs until there is news or until there is nobody left to tell. The thing it must never do is
    outlive its agent while still pinging, because that holds a session in the directory
    advertising interruptible=true when nothing can wake it — the one lie that flag must not tell.
    That is enforced by watching the harness process, not by a timer: an idle timer taxes exactly
    the long-lived sessions this is for, and "an hour has passed" was never evidence that anyone
    had gone away. --max-idle stays available for harnesses where the pid is not knowable.
    """
    rooms = [r for r in (args.rooms or "").split(",") if r]
    started = time.monotonic()
    next_ping = 0.0
    parent = _harness_pid(args.parent_pid)
    try:
        while True:
            now = time.monotonic()
            if parent is not None and not _alive(parent):
                # Leave properly rather than decaying: bye reopens anything we claimed at once,
                # instead of making requesters wait out a lease nobody is serving.
                try:
                    c.call("bus_bye", {"session_id": args.session_id})
                except HivemindError:
                    pass
                _out({"session_id": args.session_id, "orphaned": True, "watched_pid": parent,
                      "hint": "harness gone; session ended rather than left advertising itself"})
                return SIDECAR_ORPHANED
            if now >= next_ping:
                c.call("bus_ping", {"session_id": args.session_id, "ttl": args.ttl})
                next_ping = now + args.ping_every
            idle = now - started
            if args.max_idle and idle >= args.max_idle:
                _out({"session_id": args.session_id, "messages": [], "count": 0,
                      "idle_exit": True, "idle_seconds": round(idle),
                      "hint": "no traffic within --max-idle; re-arm if the agent is still alive"})
                return WAIT_TIMEOUT
            budget = args.wait
            if args.max_idle:                        # never block past the idle deadline
                budget = min(budget, max(1.0, args.max_idle - idle))
            out = c.bus_wait(args.session_id, wait=budget, rooms=rooms or None,
                             limit=args.limit, interval=args.interval)
            if not out.get("messages"):
                continue
            # Peek said there is something; now consume it for real. Deliberately not reusing the
            # peeked copy: poll is what advances the cursor, and reading it back is what proves
            # the messages are ours rather than something another reader on this session took.
            drained = c.call("bus_poll", {"session_id": args.session_id, "limit": args.limit,
                                          "rooms": rooms or None})
            if not drained.get("messages"):
                continue                             # drained elsewhere; not worth a wake-up
            _out(drained)
            return WAIT_MESSAGES
    except HivemindError as e:
        if e.kind in ("invalid", "not_found"):
            _out({"session_id": args.session_id, "session_gone": str(e),
                  "hint": "call bus_hello for a new session; capabilities must be re-advertised"})
            return SIDECAR_GONE
        raise
    except httpx.TransportError as e:
        # The client already retried with backoff, so reaching here means the server is properly
        # unreachable — a dropped tunnel, not a blip. Say so and stop: pinging into a closed
        # socket cannot keep the session alive, and the agent should be told rather than left
        # with a watcher quietly failing in the background.
        _out({"session_id": args.session_id, "unreachable": f"{type(e).__name__}: {e}",
              "hint": "server unreachable; restore the connection, then bus_hello and re-arm"})
        return SIDECAR_UNREACHABLE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hivemind", description="Hivemind client CLI")
    p.add_argument("--url"); p.add_argument("--token"); p.add_argument("--agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")

    node = sub.add_parser("node").add_subparsers(dest="node_cmd", required=True)
    up = node.add_parser("upsert")
    up.add_argument("--type", required=True); up.add_argument("--props", required=True)
    up.add_argument("--subject-key"); up.add_argument("--subject-version")
    up.add_argument("--subject-order"); up.add_argument("--node-id")
    up.add_argument("--expected-head"); up.add_argument("--reason")
    g = node.add_parser("get")
    g.add_argument("--node-id"); g.add_argument("--subject-key"); g.add_argument("--subject-version")
    g.add_argument("--history", action="store_true"); g.add_argument("--as-of")
    subj = node.add_parser("subjects"); subj.add_argument("key"); subj.add_argument("--as-of-subject")

    edge = sub.add_parser("edge").add_subparsers(dest="edge_cmd", required=True)
    ea = edge.add_parser("add")
    ea.add_argument("--type", required=True); ea.add_argument("--src", required=True)
    ea.add_argument("--dst", required=True); ea.add_argument("--props"); ea.add_argument("--source-tag")

    se = sub.add_parser("search"); se.add_argument("query", nargs="?", default="")
    se.add_argument("--type", action="append", dest="types")

    nb = sub.add_parser("neighbors"); nb.add_argument("node_id")
    nb.add_argument("--edge-type", action="append", dest="edge_types")
    nb.add_argument("--depth", type=int, default=1); nb.add_argument("--direction", default="out")

    schema = sub.add_parser("schema").add_subparsers(dest="schema_cmd", required=True)
    schema.add_parser("get").add_argument("--kind")
    sp = schema.add_parser("propose")
    sp.add_argument("--kind", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--json-schema", required=True); sp.add_argument("--traits"); sp.add_argument("--why", default="")
    schema.add_parser("apply").add_argument("pack_file")

    art = sub.add_parser("artifact").add_subparsers(dest="art_cmd", required=True)
    aor = art.add_parser("orphans"); aor.add_argument("--older-than-hours", type=int, default=0)
    ap = art.add_parser("put"); ap.add_argument("file"); ap.add_argument("--media-type")
    ap.add_argument("--attach-to"); ap.add_argument("--role", default="attachment")
    ag = art.add_parser("get"); ag.add_argument("digest"); ag.add_argument("dest")

    tool = sub.add_parser("tool").add_subparsers(dest="tool_cmd", required=True)
    tp = tool.add_parser("publish"); tp.add_argument("script")
    tp.add_argument("--id", required=True); tp.add_argument("--version", required=True)
    tp.add_argument("--description", default=""); tp.add_argument("--runtime", default="python")
    tg = tool.add_parser("get"); tg.add_argument("id"); tg.add_argument("--constraint", default="")
    tg.add_argument("--dest", default="."); tg.add_argument("--os"); tg.add_argument("--arch")
    ts = tool.add_parser("search"); ts.add_argument("query", nargs="?", default="")

    sk = sub.add_parser("skill").add_subparsers(dest="skill_cmd", required=True)
    sks = sk.add_parser("search"); sks.add_argument("query", nargs="?", default="")
    sks.add_argument("--tag", action="append", dest="tags")
    skc = sk.add_parser("catalog"); skc.add_argument("--topic")
    skl = sk.add_parser("link"); skl.add_argument("skill_id"); skl.add_argument("node_id")
    skl.add_argument("--relation", default="about"); skl.add_argument("--note")
    skg = sk.add_parser("get"); skg.add_argument("id"); skg.add_argument("--constraint", default="")
    skp = sk.add_parser("publish"); skp.add_argument("id"); skp.add_argument("--version", required=True)
    skp.add_argument("--title", required=True); skp.add_argument("--description", required=True)
    skp.add_argument("--body-file", required=True); skp.add_argument("--when-to-use")
    skp.add_argument("--tag", action="append", dest="tags"); skp.add_argument("--verified-how")
    skp.add_argument("--force", action="store_true", help="publish despite a similar existing skill")
    sky = sk.add_parser("yank"); sky.add_argument("id"); sky.add_argument("version")
    sky.add_argument("--reason", default="")

    tr = sub.add_parser("trap").add_subparsers(dest="trap_cmd", required=True)
    trs = tr.add_parser("search"); trs.add_argument("query", nargs="?", default="")
    trs.add_argument("--node-id"); trs.add_argument("--include-retired", action="store_true")
    trg = tr.add_parser("get"); trg.add_argument("trap_id")
    trr = tr.add_parser("record"); trr.add_argument("--title", required=True)
    trr.add_argument("--what-failed", required=True); trr.add_argument("--symptom", required=True)
    trr.add_argument("--root-cause"); trr.add_argument("--instead"); trr.add_argument("--node-id")
    trr.add_argument("--subject-key"); trr.add_argument("--subject-version")
    trr.add_argument("--cost-minutes", type=int); trr.add_argument("--evidence")
    trr.add_argument("--verified-how"); trr.add_argument("--confidence", default="medium")
    trt = tr.add_parser("status"); trt.add_argument("trap_id"); trt.add_argument("status")
    trt.add_argument("--reason", default="")

    # ── agent bus ────────────────────────────────────────────────────────────────
    bs = sub.add_parser("bus", help="live coordination between agent sessions").add_subparsers(
        dest="bus_cmd", required=True)
    bh = bs.add_parser("hello", help="register this session and advertise capabilities")
    bh.add_argument("--label", required=True)
    bh.add_argument("--capability", action="append", dest="capabilities", default=[],
                    metavar="NAME[=JSON]",
                    help="repeatable, e.g. --capability browser.cdp "
                         "--capability device.handset.attached='{\"serial\":\"x\"}'")
    bh.add_argument("--harness"); bh.add_argument("--room", action="append", dest="rooms")
    bh.add_argument("--interruptible", action="store_true",
                    help="a watcher can wake this session (true with a backgrounded `bus wait`)")
    bh.add_argument("--ttl", type=int, default=0)
    bp = bs.add_parser("ping"); bp.add_argument("session_id")
    bp.add_argument("--capability", action="append", dest="capabilities")
    bp.add_argument("--status"); bp.add_argument("--ttl", type=int, default=0)
    bs.add_parser("bye").add_argument("session_id")

    ba = bs.add_parser("agents"); ba.add_argument("--capability")
    ba.add_argument("--include-ended", action="store_true")
    bs.add_parser("capabilities")
    bs.add_parser("rooms")
    bs.add_parser("stats")
    bs.add_parser("reap")
    bj = bs.add_parser("join"); bj.add_argument("session_id"); bj.add_argument("room")
    bl = bs.add_parser("leave"); bl.add_argument("session_id"); bl.add_argument("room")

    ref_help = ("repeatable; a bare node_id, or JSON like "
                "'{\"kind\":\"traversal\",\"id\":\"01M2…\",\"edge_types\":[\"calls\"],\"depth\":2}'")
    bpo = bs.add_parser("post"); bpo.add_argument("session_id"); bpo.add_argument("body")
    bpo.add_argument("--room"); bpo.add_argument("--to", dest="to_session")
    bpo.add_argument("--kind", default="chat", help="chat|question|system")
    bpo.add_argument("--data")
    bpo.add_argument("--reply-to", type=int, metavar="SEQ",
                     help="answer that message; inherits its room")
    bpo.add_argument("--ref", action="append", dest="refs", metavar="NODE_ID|JSON",
                     help=ref_help)
    bpl = bs.add_parser("poll"); bpl.add_argument("session_id")
    bpl.add_argument("--limit", type=int, default=50); bpl.add_argument("--after", type=int)
    bpl.add_argument("--include-self", action="store_true")
    bpk = bs.add_parser("peek"); bpk.add_argument("session_id")
    bpk.add_argument("--limit", type=int, default=50); bpk.add_argument("--after", type=int)
    bak = bs.add_parser("ack"); bak.add_argument("session_id"); bak.add_argument("seq", type=int)
    bhi = bs.add_parser("history"); bhi.add_argument("room")
    bhi.add_argument("--limit", type=int, default=50); bhi.add_argument("--before", type=int)
    bth = bs.add_parser("thread", help="a message and every reply to it")
    bth.add_argument("seq", type=int); bth.add_argument("--limit", type=int, default=200)

    bw = bs.add_parser("wait", help="block until a message arrives, then exit (the interrupt)")
    bw.add_argument("session_id")
    bw.add_argument("--wait", type=float, default=25.0, help="seconds to block per call")
    bw.add_argument("--rooms", help="comma-separated filter")
    bw.add_argument("--after", type=int); bw.add_argument("--limit", type=int, default=50)
    bw.add_argument("--interval", type=float, default=1.0)
    bw.add_argument("--follow", action="store_true",
                    help="keep waiting and printing instead of exiting on the first batch")

    bsc = bs.add_parser("sidecar",
                        help="heartbeat + block + drain: wake the agent only for real traffic")
    bsc.add_argument("session_id")
    bsc.add_argument("--wait", type=float, default=60.0, help="seconds to block per call")
    bsc.add_argument("--ping-every", type=float, default=300.0, dest="ping_every",
                     help="heartbeat interval; keep it comfortably under --ttl")
    bsc.add_argument("--ttl", type=int, default=900,
                     help="session lifetime each heartbeat refreshes")
    bsc.add_argument("--max-idle", type=float, default=0.0, dest="max_idle",
                     help="exit 75 after this long with no traffic. Default 0 (never): a quiet "
                          "bus should cost an agent nothing, and outliving the agent is caught by "
                          "--parent-pid instead. Set it only where no pid can be watched")
    bsc.add_argument("--parent-pid", type=int, default=None, dest="parent_pid",
                     help="exit 71 (and bus_bye) when this pid dies — the process that re-invokes "
                          "the agent. Default: auto-detected as our shell's parent. 0 disables, "
                          "which risks a dead session advertising interruptible=true")
    bsc.add_argument("--rooms", help="comma-separated filter")
    bsc.add_argument("--limit", type=int, default=50)
    bsc.add_argument("--interval", type=float, default=1.0)

    brq = bs.add_parser("request"); brq.add_argument("session_id")
    brq.add_argument("--task", required=True)
    brq.add_argument("--needs", action="append", dest="needs")
    brq.add_argument("--to", dest="to_session"); brq.add_argument("--payload")
    brq.add_argument("--room"); brq.add_argument("--lease-sec", type=int, default=0)
    brq.add_argument("--ttl", type=int, default=3600)
    brq.add_argument("--ref", action="append", dest="refs", metavar="NODE_ID|JSON",
                     help="what the work is about; " + ref_help)
    bc = bs.add_parser("claim"); bc.add_argument("session_id"); bc.add_argument("request_id")
    bc.add_argument("--lease-sec", type=int, default=0)
    brl = bs.add_parser("release"); brl.add_argument("session_id"); brl.add_argument("request_id")
    brl.add_argument("--reason", default="")
    brs = bs.add_parser("respond"); brs.add_argument("session_id"); brs.add_argument("request_id")
    brs.add_argument("--result"); brs.add_argument("--error")
    brs.add_argument("--ref", action="append", dest="refs", metavar="NODE_ID|JSON",
                     help="what you PRODUCED (graph_upsert it, then point at it); " + ref_help)
    bs.add_parser("request-get").add_argument("request_id")
    brl2 = bs.add_parser("requests"); brl2.add_argument("--session"); brl2.add_argument("--state")
    brl2.add_argument("--claimable", action="store_true")
    bre = bs.add_parser("resolve", help="follow a message's/request's refs into the graph")
    bre.add_argument("--request-id"); bre.add_argument("--seq", type=int)
    bre.add_argument("--limit", type=int, default=25)
    bnr = bs.add_parser("node-refs", help="what live bus traffic points at this node")
    bnr.add_argument("node_id"); bnr.add_argument("--limit", type=int, default=50)

    guide = sub.add_parser("guide").add_subparsers(dest="guide_cmd", required=True)
    guide.add_parser("get").add_argument("section", nargs="?")
    gpr = guide.add_parser("propose"); gpr.add_argument("section"); gpr.add_argument("--body", required=True)
    gpr.add_argument("--why", default="")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "health":
            c = _client(args); _out(c.health()); return 0
        c = _client(args)
        if args.cmd == "node" and args.node_cmd == "upsert":
            _out(c.upsert(args.type, _json_arg(args.props, "props"),
                          subject_key=args.subject_key, subject_version=args.subject_version,
                          subject_order=args.subject_order, node_id=args.node_id,
                          expected_head=args.expected_head, reason=args.reason))
        elif args.cmd == "node" and args.node_cmd == "get":
            _out(c.get(node_id=args.node_id, subject_key=args.subject_key,
                       subject_version=args.subject_version, history=args.history,
                       as_of=args.as_of))
        elif args.cmd == "node" and args.node_cmd == "subjects":
            _out(c.call("graph_subjects", {"subject_key": args.key,
                                           "as_of_subject": args.as_of_subject}))
        elif args.cmd == "edge" and args.edge_cmd == "add":
            _out(c.link(args.type, args.src, args.dst, _json_arg(args.props, "props"),
                        source_tag=args.source_tag))
        elif args.cmd == "search":
            _out(c.search(args.query, types=args.types))
        elif args.cmd == "neighbors":
            _out(c.call("graph_neighbors", {"node_id": args.node_id, "edge_types": args.edge_types,
                                            "depth": args.depth, "direction": args.direction}))
        elif args.cmd == "schema" and args.schema_cmd == "get":
            _out(c.schema(kind=args.kind))
        elif args.cmd == "schema" and args.schema_cmd == "propose":
            _out(c.call("schema_propose", {"kind": args.kind, "name": args.name,
                        "json_schema": _json_arg(args.json_schema, "json-schema"),
                        "traits": _json_arg(args.traits, "traits") or None, "why": args.why}))
        elif args.cmd == "schema" and args.schema_cmd == "apply":
            pack = json.loads(open(args.pack_file).read())
            _out(c.call("schema_apply", {"pack": pack}))
        elif args.cmd == "artifact" and args.art_cmd == "put":
            res = c.artifacts.put(args.file, media_type=args.media_type,
                                  attach_to=args.attach_to, role=args.role)
            _out(res)
        elif args.cmd == "artifact" and args.art_cmd == "orphans":
            _out(c.call("artifact_orphans", {"older_than_hours": args.older_than_hours}))
        elif args.cmd == "artifact" and args.art_cmd == "get":
            _out(c.artifacts.get(args.digest, args.dest))
        elif args.cmd == "tool" and args.tool_cmd == "publish":
            _out(c.tool_publish(args.script, id=args.id, version=args.version,
                                description=args.description, runtime=args.runtime))
        elif args.cmd == "tool" and args.tool_cmd == "get":
            _out(c.tool_get(args.id, constraint=args.constraint, dest_dir=args.dest,
                            os_=args.os, arch=args.arch))
        elif args.cmd == "tool" and args.tool_cmd == "search":
            _out(c.tool_search(args.query))
        elif args.cmd == "skill" and args.skill_cmd == "search":
            _out(c.call("skill_search", {"query": args.query, "tags": args.tags}))
        elif args.cmd == "skill" and args.skill_cmd == "catalog":
            _out(c.call("skill_catalog", {"topic": args.topic}))
        elif args.cmd == "skill" and args.skill_cmd == "link":
            _out(c.call("skill_link", {"skill_id": args.skill_id, "node_id": args.node_id,
                                       "relation": args.relation, "note": args.note}))
        elif args.cmd == "skill" and args.skill_cmd == "get":
            _out(c.call("skill_get", {"id": args.id, "constraint": args.constraint}))
        elif args.cmd == "skill" and args.skill_cmd == "publish":
            _out(c.call("skill_publish", {"id": args.id, "version": args.version,
                 "title": args.title, "description": args.description,
                 "body": open(args.body_file).read(), "when_to_use": args.when_to_use,
                 "tags": args.tags, "verified_how": args.verified_how, "force": args.force}))
        elif args.cmd == "skill" and args.skill_cmd == "yank":
            _out(c.call("skill_yank", {"id": args.id, "version": args.version,
                                       "reason": args.reason}))
        elif args.cmd == "trap" and args.trap_cmd == "search":
            _out(c.call("trap_search", {"query": args.query, "node_id": args.node_id,
                                        "include_retired": args.include_retired}))
        elif args.cmd == "trap" and args.trap_cmd == "get":
            _out(c.call("trap_get", {"trap_id": args.trap_id}))
        elif args.cmd == "trap" and args.trap_cmd == "record":
            _out(c.call("trap_record", {"title": args.title, "what_failed": args.what_failed,
                 "symptom": args.symptom, "root_cause": args.root_cause, "instead": args.instead,
                 "node_id": args.node_id, "subject_key": args.subject_key,
                 "subject_version": args.subject_version, "cost_minutes": args.cost_minutes,
                 "evidence": args.evidence, "verified_how": args.verified_how,
                 "confidence": args.confidence}))
        elif args.cmd == "trap" and args.trap_cmd == "status":
            _out(c.call("trap_status", {"trap_id": args.trap_id, "status": args.status,
                                        "reason": args.reason}))
        elif args.cmd == "bus":
            return _bus(c, args)
        elif args.cmd == "guide" and args.guide_cmd == "get":
            _out(c.guide(args.section))
        elif args.cmd == "guide" and args.guide_cmd == "propose":
            _out(c.call("guide_propose", {"section": args.section, "body": args.body,
                                          "why": args.why}))
        else:
            _die("unknown command")
        return 0
    except HivemindError as e:
        _die(f"{e} ({e.kind or 'error'})", code=1)
    finally:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
