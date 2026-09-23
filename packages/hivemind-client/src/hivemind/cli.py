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



def _bus(c, args) -> int:
    """Bus subcommands. `listen` is the one Monitor runs; the rest are ordinary tool calls."""
    from . import bus as _bus_mod

    if args.bus_cmd == "listen":
        def mint(label):
            cmd = c.call("bus_connect", {"label": label})["monitor_command"]
            return cmd.split("--label ", 1)[1].strip().strip("'") if "--label " in cmd else cmd

        def mint_url(label):
            # Build the ws URL from the base URL this client already uses, NOT from whatever the
            # server advertises: a server bound to 0.0.0.0 advertises 0.0.0.0, which no client can
            # dial. The address that reached the server is by definition one that works.
            ticket = c.call("bus_connect", {"label": label})["ticket"]
            base = c.base_url
            ws_base = "ws" + base[4:] if base.startswith("http") else base
            return f"{ws_base}/bus/ws?ticket={ticket}"

        if args.url:
            # Explicit URL: single ticket, so this cannot survive a reconnect. Supported for
            # debugging; agents are given a --label command instead.
            return _bus_mod.run_listen(args.url, retry=not args.once, remint=None,
                                       inbox=args.inbox)
        if not args.label:
            _die("give --label (recommended) or --url")
        return _bus_mod.run_listen(mint_url(args.label), retry=not args.once,
                                   remint=lambda: mint_url(args.label), inbox=args.inbox)

    if args.bus_cmd == "connect":
        _out(c.call("bus_connect", {"label": args.label}))
    elif args.bus_cmd == "send":
        _out(c.call("bus_send", {"to": args.to, "body": args.body}))
    elif args.bus_cmd == "broadcast":
        _out(c.call("bus_broadcast", {"body": args.body, "room": args.room}))
    elif args.bus_cmd == "peers":
        _out(c.call("bus_peers", {"online_only": args.online_only}))
    return 0


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

    # ── agent bus (WebSocket push) ───────────────────────────────────────────────
    bus = sub.add_parser("bus", help="live messaging between agent sessions").add_subparsers(
        dest="bus_cmd", required=True)
    bl = bus.add_parser("listen", help="stream messages; one line per message (for Monitor)")
    bl.add_argument("--url", help="ws:// URL from bus_connect (includes the single-use ticket)")
    bl.add_argument("--label", help="connect as this peer and mint the ticket automatically")
    bl.add_argument("--once", action="store_true", help="exit on disconnect instead of retrying")
    bl.add_argument("--inbox", help="append every message here as JSON, one per line "
                                    "(default: ~/.hivemind/bus-inbox.jsonl)")
    bsnd = bus.add_parser("send", help="send a message to one peer")
    bsnd.add_argument("to"); bsnd.add_argument("body")
    bbc = bus.add_parser("broadcast", help="send to everyone in a room")
    bbc.add_argument("body"); bbc.add_argument("--room", default="lobby")
    bp = bus.add_parser("peers", help="who is connected")
    bp.add_argument("--online-only", action="store_true")
    bus.add_parser("connect", help="mint a ticket and print the Monitor command")\
        .add_argument("label")

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
