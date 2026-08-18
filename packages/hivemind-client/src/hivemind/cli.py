"""`hivemind` CLI — talk to a hivemind project from the shell or from an agent.

Config from env (HIVEMIND_SERVER_URL, HIVEMIND_TOKEN, HIVEMIND_AGENT) or --url/--token/--agent.
All output is JSON on stdout; errors go to stderr with a non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    skg = sk.add_parser("get"); skg.add_argument("id"); skg.add_argument("--constraint", default="")
    skp = sk.add_parser("publish"); skp.add_argument("id"); skp.add_argument("--version", required=True)
    skp.add_argument("--title", required=True); skp.add_argument("--description", required=True)
    skp.add_argument("--body-file", required=True); skp.add_argument("--when-to-use")
    skp.add_argument("--tag", action="append", dest="tags"); skp.add_argument("--verified-how")
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
            res = c.artifacts.put(args.file, media_type=args.media_type)
            if args.attach_to:
                c.call("artifact_attach", {"digest": res["digest"], "version_id": args.attach_to,
                                           "role": args.role, "filename": os.path.basename(args.file)})
            _out(res)
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
        elif args.cmd == "skill" and args.skill_cmd == "get":
            _out(c.call("skill_get", {"id": args.id, "constraint": args.constraint}))
        elif args.cmd == "skill" and args.skill_cmd == "publish":
            _out(c.call("skill_publish", {"id": args.id, "version": args.version,
                 "title": args.title, "description": args.description,
                 "body": open(args.body_file).read(), "when_to_use": args.when_to_use,
                 "tags": args.tags, "verified_how": args.verified_how}))
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
