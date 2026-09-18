"""Hivemind-resident work pipeline: queues, atomic claims, stage records.

Queues are QUERIES over graph nodes, never files, so any machine that can reach
the server can enumerate, claim, advance and resume work.

Mutual exclusion comes from graph_upsert's `expected_head` compare-and-swap:
concurrent claimants all send the same head, the server accepts exactly one and
rejects the rest with error_kind="conflict". Measured: 5 racers, 1 winner.

Config: HIVEMIND_SERVER_URL, HIVEMIND_TOKEN, FLEET_AGENT, FLEET_HOST.
"""
from __future__ import annotations
import json, os, socket, time, urllib.request
from datetime import datetime, timezone

BASE  = os.environ.get("HIVEMIND_SERVER_URL", "").rstrip("/")
TOKEN = os.environ.get("HIVEMIND_TOKEN", "")
AGENT = os.environ.get("FLEET_AGENT", "fleet-agent")
HOST  = os.environ.get("FLEET_HOST", socket.gethostname())
PROTO = "2026-07-28"

STAGES = ["cut", "analyze", "dedupe", "verify-muse", "verify-model", "poc-build", "device-test"]
TERMINAL = ["done", "rejected"]


class Refused(RuntimeError):
    """Server refused the write. .kind is 'conflict' for a lost claim race."""
    def __init__(self, payload):
        super().__init__(json.dumps(payload)[:300])
        self.payload = payload
        self.kind = payload.get("error_kind")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def call(tool, arguments=None, agent=None):
    args = dict(arguments or {}); args.setdefault("agent", agent or AGENT)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args,
                       "_meta": {"io.modelcontextprotocol/protocolVersion": PROTO,
                                 "io.modelcontextprotocol/clientInfo": {"name": "fleet", "version": "1"},
                                 "io.modelcontextprotocol/clientCapabilities": {}}}}
    req = urllib.request.Request(
        BASE + "/mcp", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer " + TOKEN,
                 "MCP-Protocol-Version": PROTO,
                 "Mcp-Method": "tools/call", "Mcp-Name": tool})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read().decode()
    if raw.lstrip().startswith(("event:", "data:")):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip(); break
    doc = json.loads(raw)
    if "error" in doc:
        raise RuntimeError(doc["error"])
    res = doc["result"]
    if isinstance(res, dict) and res.get("content"):
        txt = res["content"][0].get("text", "")
        try:
            out = json.loads(txt)
        except Exception:
            return txt
        # A refused write is NOT a transport error: it is ok:false inside a 200.
        if isinstance(out, dict) and out.get("ok") is False:
            raise Refused(out)
        return out
    return res


def _get(subject_key):
    return call("graph_get", {"subject_key": subject_key, "subject_version": "-"})


# ── pipeline ────────────────────────────────────────────────────────────────
def ensure_pipeline(name, goal="", build="", stages=None):
    sk = "pipeline:" + name
    try:
        return _get(sk)["node_id"]
    except Exception:
        return call("graph_upsert", {
            "type": "pipeline", "subject_key": sk, "subject_version": "-", "subject_order": "-",
            "props": {"name": name, "goal": goal, "build": build,
                      "stages": stages or STAGES, "status": "open", "owner": AGENT}})["node_id"]


# ── enqueue ─────────────────────────────────────────────────────────────────
def enqueue(pipeline, item_id, title, goal, output_schema, target="", build="",
            stage="analyze", priority="P2"):
    """Create a work item at `stage` with status pending. Idempotent on item_id."""
    pid = ensure_pipeline(pipeline)
    sk = "work:%s:%s" % (pipeline, item_id)
    try:
        return _get(sk)["node_id"]          # already queued
    except Exception:
        pass
    n = call("graph_upsert", {
        "type": "work_item", "subject_key": sk, "subject_version": "-", "subject_order": "-",
        "props": {"title": title, "pipeline": pipeline, "stage": stage, "status": "pending",
                  "priority": priority, "target": target, "build": build, "goal": goal,
                  "output_schema": output_schema, "attempt": 0, "created_by": AGENT},
        "reason": "enqueue"})
    call("graph_link", {"edge_type": "belongs_to", "src": n["node_id"], "dst": pid,
                        "props": {"notes": "queued"}})
    return n["node_id"]


# ── queue (a QUERY, not a file) ─────────────────────────────────────────────
def type_total(node_type):
    """Population of a node type, for use as a completeness denominator.

    Verified 2026-09-15: empty query + types filter paginates correctly on the
    'recent' backend and reports `total_of_type`. Do NOT reuse this shape with a
    TEXT query -- on the fts5 backend the types filter is applied after a row cap
    and `has_more:false` is not a completeness claim (tool
    hivemind.s7-safe-enumerate measured it 80% short on a rare type)."""
    r = call("graph_search", {"query": "", "types": [node_type], "limit": 1})
    return r.get("total_of_type")


def queue(pipeline, stage=None, status="pending", stale_heartbeat_s=None,
          verify=True):
    """Enumerate work, sorted by priority.

    Traverses `belongs_to` from the pipeline anchor, because graph_neighbors
    returns node PROPS in one call. graph_search does NOT: its rows carry only
    node_id / node_type / subject_key / version_id / score / snippet, so a queue
    built on it silently matches nothing. Use search only to COUNT (type_total).
    """
    pid = ensure_pipeline(pipeline)
    nb = call("graph_neighbors", {"node_id": pid, "edge_types": ["belongs_to"],
                                  "depth": 1, "direction": "in"})
    items = [n for n in nb.get("neighbors", []) if n.get("node_type") == "work_item"]

    if verify:
        total = type_total("work_item")
        if total is not None and len(items) > total:
            raise RuntimeError("anchor traversal saw %d work_items but the type "
                               "population is %d -- stale edges?" % (len(items), total))

    out = []
    for n in items:
        p = n.get("props") or {}
        if p.get("pipeline") and p["pipeline"] != pipeline:
            continue
        if stage and p.get("stage") != stage:
            continue
        if status and p.get("status") != status:
            continue
        if stale_heartbeat_s is not None:
            hb = p.get("heartbeat_at")
            if hb:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(hb)).total_seconds()
                except ValueError:
                    age = None
                if age is not None and age < stale_heartbeat_s:
                    continue
        out.append({"node_id": n["node_id"], **p})
    order = {"P1": 0, "P2": 1, "P3": 2}
    out.sort(key=lambda x: order.get(x.get("priority", "P2"), 1))
    return out


def prior_run(work_node_id, stage):
    """What did an earlier stage produce? This is how a resuming machine on a
    DIFFERENT host picks up the previous stage's output without touching the
    host that produced it."""
    nb = call("graph_neighbors", {"node_id": work_node_id, "edge_types": ["stage_of"],
                                  "depth": 1, "direction": "in"})
    runs = [n for n in nb.get("neighbors", [])
            if n.get("node_type") == "stage_run"
            and (n.get("props") or {}).get("stage") == stage]
    runs.sort(key=lambda n: (n.get("props") or {}).get("ended_at") or "")
    return (runs[-1].get("props") if runs else None)


# ── claim / heartbeat / advance ─────────────────────────────────────────────
def claim(node_id, holder=None, host=None):
    """Atomically take an item. Returns the new head, or raises Refused(kind=
    'conflict') if another machine won. NEVER claim without expected_head."""
    holder = holder or AGENT; host = host or HOST
    g = call("graph_get", {"node_id": node_id})
    cur = g["current"]; props = dict(cur["props"])
    if props.get("status") not in ("pending", "failed"):
        raise Refused({"error_kind": "conflict",
                       "error": "item is %s, not claimable" % props.get("status")})
    props.update(status="claimed", claim_holder=holder, claim_host=host,
                 claimed_at=now(), heartbeat_at=now(),
                 attempt=int(props.get("attempt", 0)) + 1)
    r = call("graph_upsert", {"type": "work_item", "node_id": node_id, "props": props,
                              "expected_head": cur["version_id"],
                              "reason": "claim by %s@%s" % (holder, host)}, agent=holder)
    return r["version_id"]


def heartbeat(node_id, holder=None):
    """Prove the CLAIMER is alive. This is liveness on the worker, NOT a deadline
    on the work: a muse task may legitimately run for hours."""
    holder = holder or AGENT
    g = call("graph_get", {"node_id": node_id})
    cur = g["current"]; props = dict(cur["props"])
    if props.get("claim_holder") != holder:
        raise Refused({"error_kind": "conflict", "error": "claim held by %s"
                       % props.get("claim_holder")})
    props["heartbeat_at"] = now()
    props["status"] = "running"
    return call("graph_upsert", {"type": "work_item", "node_id": node_id, "props": props,
                                 "expected_head": cur["version_id"],
                                 "reason": "heartbeat"}, agent=holder)["version_id"]


def steal(node_id, stale_s=3600, holder=None, host=None):
    """Take over an item whose CLAIMER is dead. Only permitted on a stale
    heartbeat, and the steal is recorded on the item."""
    holder = holder or AGENT; host = host or HOST
    g = call("graph_get", {"node_id": node_id})
    cur = g["current"]; props = dict(cur["props"])
    hb = props.get("heartbeat_at")
    if hb:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
        if age < stale_s:
            raise Refused({"error_kind": "conflict",
                           "error": "heartbeat only %ds old; not stale" % int(age)})
    prev = props.get("claim_holder", "?")
    props.update(status="claimed", claim_holder=holder, claim_host=host,
                 claimed_at=now(), heartbeat_at=now(),
                 notes=("stolen from %s (stale heartbeat %s); " % (prev, hb)) + (props.get("notes") or ""))
    return call("graph_upsert", {"type": "work_item", "node_id": node_id, "props": props,
                                 "expected_head": cur["version_id"],
                                 "reason": "steal from %s" % prev}, agent=holder)["version_id"]


def advance(node_id, next_stage, status="pending", finding=None, note=""):
    """Move an item to the next stage and release the claim."""
    g = call("graph_get", {"node_id": node_id})
    cur = g["current"]; props = dict(cur["props"])
    props.update(stage=next_stage, status=status,
                 claim_holder="", claim_host="", heartbeat_at="")
    if finding: props["finding"] = finding
    if note: props["notes"] = (note + " | " + (props.get("notes") or "")).strip(" |")
    return call("graph_upsert", {"type": "work_item", "node_id": node_id, "props": props,
                                 "expected_head": cur["version_id"],
                                 "reason": "advance -> " + next_stage})["version_id"]


def fail(node_id, reason, retryable=True):
    g = call("graph_get", {"node_id": node_id})
    cur = g["current"]; props = dict(cur["props"])
    props.update(status="failed" if retryable else "rejected",
                 blocked_reason=reason, claim_holder="", claim_host="", heartbeat_at="")
    if not retryable: props["stage"] = "rejected"
    return call("graph_upsert", {"type": "work_item", "node_id": node_id, "props": props,
                                 "expected_head": cur["version_id"],
                                 "reason": "fail: " + reason[:80]})["version_id"]


# ── stage runs (the per-stage audit record) ─────────────────────────────────
def start_run(work_sk, pipeline, stage, runner, device=""):
    sk = "run:%s:%s:%d" % (work_sk.replace("work:", ""), stage, int(time.time()))
    n = call("graph_upsert", {
        "type": "stage_run", "subject_key": sk, "subject_version": "-", "subject_order": "-",
        "props": {"work_item": work_sk, "pipeline": pipeline, "stage": stage,
                  "runner": runner, "host": HOST, "started_at": now(), "status": "running",
                  "device": device}})
    try:
        call("graph_link", {"edge_type": "stage_of", "src": n["node_id"],
                            "dst": _get(work_sk)["node_id"], "props": {}})
    except Exception:
        pass
    return sk, n["node_id"]


def finish_run(run_sk, status="complete", verdict="n/a", output_digest="",
               schema_valid=None, injection_sightings=0, notes=""):
    g = _get(run_sk); cur = g["current"]; props = dict(cur["props"])
    props.update(status=status, verdict=verdict, ended_at=now(),
                 output_digest=output_digest, injection_sightings=injection_sightings,
                 notes=notes)
    if schema_valid is not None: props["schema_valid"] = schema_valid
    return call("graph_upsert", {"type": "stage_run", "node_id": g["node_id"], "props": props,
                                 "expected_head": cur["version_id"],
                                 "reason": "finish " + status})["version_id"]


def board(pipeline):
    """One-glance state of every stage."""
    pid = ensure_pipeline(pipeline)
    nb = call("graph_neighbors", {"node_id": pid, "edge_types": ["belongs_to"],
                                  "depth": 1, "direction": "in"})
    grid = {}
    for n in nb.get("neighbors", []):
        if n.get("node_type") != "work_item": continue
        p = n.get("props", {})
        grid.setdefault(p.get("stage", "?"), {}).setdefault(p.get("status", "?"), 0)
        grid[p.get("stage", "?")][p.get("status", "?")] += 1
    return grid


# ── CLI ─────────────────────────────────────────────────────────────────────
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="pipeline",
        description="Hivemind-resident work pipeline. Queues, atomic claims, stage records.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("board", help="stage x status counts"); b.add_argument("pipeline")
    q = sub.add_parser("queue", help="list work"); q.add_argument("pipeline")
    q.add_argument("--stage"); q.add_argument("--status", default="pending")
    q.add_argument("--stale", type=int, help="only items whose CLAIMER is stale this many seconds")
    e = sub.add_parser("enqueue"); e.add_argument("pipeline"); e.add_argument("id")
    e.add_argument("title"); e.add_argument("--goal", default=""); e.add_argument("--schema", default="")
    e.add_argument("--target", default=""); e.add_argument("--build", default="")
    e.add_argument("--stage", default="analyze"); e.add_argument("--priority", default="P2")
    c = sub.add_parser("claim"); c.add_argument("node_id"); c.add_argument("--holder")
    hb = sub.add_parser("heartbeat"); hb.add_argument("node_id"); hb.add_argument("--holder")
    s = sub.add_parser("steal"); s.add_argument("node_id"); s.add_argument("--stale", type=int, default=3600)
    ad = sub.add_parser("advance"); ad.add_argument("node_id"); ad.add_argument("stage")
    sw = sub.add_parser("sweep", help="report work whose claimer is dead"); sw.add_argument("pipeline")
    sw.add_argument("--stale", type=int, default=3600)
    a = ap.parse_args(argv)

    if not BASE or not TOKEN:
        raise SystemExit("set HIVEMIND_SERVER_URL and HIVEMIND_TOKEN")

    if a.cmd == "board":
        print(json.dumps(board(a.pipeline), indent=1))
    elif a.cmd == "queue":
        for w in queue(a.pipeline, stage=a.stage, status=a.status, stale_heartbeat_s=a.stale):
            print("%-10s %-12s %-9s %-24s %s" % (w.get("priority"), w.get("stage"),
                  w.get("status"), (w.get("claim_holder") or "-")[:24], w.get("title", "")[:58]))
    elif a.cmd == "enqueue":
        print(enqueue(a.pipeline, a.id, a.title, a.goal, a.schema,
                      target=a.target, build=a.build, stage=a.stage, priority=a.priority))
    elif a.cmd == "claim":
        try:
            print(claim(a.node_id, holder=a.holder))
        except Refused as ex:
            raise SystemExit("not claimed: %s" % ex.payload.get("error"))
    elif a.cmd == "heartbeat":
        print(heartbeat(a.node_id, holder=a.holder))
    elif a.cmd == "steal":
        try:
            print(steal(a.node_id, stale_s=a.stale))
        except Refused as ex:
            raise SystemExit("not stolen: %s" % ex.payload.get("error"))
    elif a.cmd == "advance":
        print(advance(a.node_id, a.stage))
    elif a.cmd == "sweep":
        rows = queue(a.pipeline, stage=None, status="claimed", stale_heartbeat_s=a.stale)
        if not rows:
            print("no abandoned work")
        for w in rows:
            print("RESUMABLE %-12s held by %-18s since %s  %s" % (
                w.get("stage"), w.get("claim_holder"), w.get("heartbeat_at"), w.get("title", "")[:40]))


if __name__ == "__main__":
    import sys as _s
    _main(_s.argv[1:])
