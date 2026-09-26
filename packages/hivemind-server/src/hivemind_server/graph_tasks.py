"""Opt-in graph task marker, heartbeat sidecars and fenced claim lifecycle."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Optional

from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid, NotFound, SENTINEL
from .ids import ulid
from . import capabilities, schemas

DEFAULT_INTERVAL = 300
DEFAULT_EXPIRY = 3600
# The three task states. A node whose props carry one of these is describing task status in its
# own versioned props; anything else (or nothing) means the marker owns status on its own.
TASK_STATES = ("unclaimed", "in_progress", "complete")


def _timing(interval_seconds: int, expires_after_seconds: int) -> None:
    if type(interval_seconds) is not int or not 30 <= interval_seconds <= 8 * 3600:
        raise Invalid("heartbeat interval must be 30 seconds to 8 hours")
    if (type(expires_after_seconds) is not int or
            not max(60, 2 * interval_seconds) <= expires_after_seconds <= 24 * 3600):
        raise Invalid("expiry must be 1 minute to 24 hours and at least twice the interval")


def _marker(cur, node_id: str):
    row = cur.execute("SELECT * FROM graph_task WHERE node_id=?", (node_id,)).fetchone()
    if row is None:
        raise NotFound("graph node is not marked as a task")
    return row


def eligible(cur, node_id: str, who: StableAddress) -> None:
    """Require every task tag from this agent's current project-local advertisement."""
    task = _marker(cur, node_id)
    required = json.loads(task["required_capabilities_json"])
    capabilities.require_tags(required, capabilities.get_in_transaction(cur, _address(who)))


def enable(db: Database, agent_id: str, node_id: str, room_id: Optional[str] = None,
           *, required_capabilities: Optional[list[str]] = None) -> dict:
    required = capabilities.normalize([] if required_capabilities is None
                                      else required_capabilities, limit=32)
    with db.write(agent_id, "enable graph task marker") as tx:
        cur = tx.cur
        row = cur.execute("SELECT redirect_to,node_type FROM node WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            raise NotFound("graph node does not exist")
        if row["redirect_to"] is not None:
            raise Invalid("cannot mark a redirected graph node as a task")
        if room_id is not None and not cur.execute(
                "SELECT 1 FROM chat_room WHERE room_id=?", (room_id,)).fetchone():
            raise NotFound("task room does not exist; create it explicitly")
        if cur.execute("SELECT 1 FROM graph_task WHERE node_id=?", (node_id,)).fetchone():
            raise Conflict("node already marked as a task")
        props = cur.execute("SELECT props FROM node_version WHERE node_id=? AND tx_to=?",
                            (node_id, SENTINEL)).fetchone()
        if props is None:
            raise Invalid("task graph node has no current version")
        current_props = json.loads(props["props"])
        # Seed the marker from the node's OWN status; do not assert "unclaimed" over it. Marking a
        # work_item already in_progress or complete used to write a sidecar saying "unclaimed",
        # after which graph_task_get showed the marker and the props disagreeing, and
        # graph_task_claim handed out a claim on a finished task — its guard reads this column, not
        # the props, so only graph_task_complete failed later, with an unrelated message about a
        # versioned status field. Any state is accepted here, not just "unclaimed": a node already
        # under way is still a task, and refusing to mark it would be a second surprise.
        marked = current_props.get("status")
        versioned = marked in TASK_STATES
        if versioned:
            for state in ("in_progress", "complete"):
                try:
                    schemas.validate_props(cur, "node", row["node_type"],
                                           {**current_props, "status": state})
                except Invalid:
                    versioned = False
                    break
        cur.execute("INSERT INTO graph_task(node_id,room_id,status,created_tx,updated_tx,"
                    "status_mode,required_capabilities_json) VALUES(?,?,?,?,?,?,?)",
                    (node_id, room_id, marked if marked in TASK_STATES else "unclaimed",
                     tx.tx_id, tx.tx_id, "versioned" if versioned else "sidecar",
                     json.dumps(required)))
    return read(db, node_id)


def offer(db: Database, agent_id: str, room_name: str,
          title: str, summary: str,
          *, required_capabilities: Optional[list[str]] = None) -> dict:
    """Create a graph task in an existing room, bootstrapping a built-in type if needed."""
    required = capabilities.normalize([] if required_capabilities is None
                                      else required_capabilities, limit=32)
    if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 256 or
            not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2048):
        raise Invalid("task title and summary must be nonempty and bounded")
    from .chat import ChatStore
    from .graph import _create_node_tx, get_node
    with db.write(agent_id, "offer graph task") as tx:
        room_id = ChatStore(db)._lookup_room(tx.cur, room_name)
        props = {"title": title.strip(), "summary": summary.strip(),
                 "status": "unclaimed", "room_id": room_id}
        compatible = tx.cur.execute("SELECT 1 FROM node_type WHERE name='work_item' AND "
                                    "status='active' LIMIT 1").fetchone() is not None
        if compatible:
            try:
                for state in TASK_STATES:
                    schemas.validate_props(tx.cur, "node", "work_item", {**props, "status": state})
            except Invalid:
                compatible = False
        node_type = "work_item" if compatible else "hivemind_collab_task"
        if not compatible and schemas.usable_type(tx.cur, "node", node_type) is None:
            schemas.define_type(tx.cur, tx, "node", node_type,
                                {"type": "object", "additionalProperties": False,
                                 "properties": {"title": {"type": "string"},
                                                "summary": {"type": "string"},
                                                "status": {"enum": list(TASK_STATES)},
                                                "room_id": {"type": "string"}},
                                 "required": ["title", "summary", "status", "room_id"]},
                                status="active")
        if not compatible:
            for state in TASK_STATES:
                schemas.validate_props(tx.cur, "node", node_type, {**props, "status": state})
        node = _create_node_tx(tx, node_type, props)
        tx.cur.execute("INSERT INTO graph_task(node_id,room_id,status,created_tx,updated_tx,"
                       "status_mode,required_capabilities_json) VALUES(?,?,?,?,?,?,?)",
                       (node["node_id"], room_id, "unclaimed", tx.tx_id, tx.tx_id,
                        "versioned", json.dumps(required)))
        _event(tx, node["node_id"], "offer", 0, None, time.time())
    return get_node(db, node_id=node["node_id"])


def _read(cur, node_id: str, t: float) -> dict:
    task = _marker(cur, node_id)
    claim = cur.execute("SELECT * FROM graph_task_claim WHERE node_id=?", (node_id,)).fetchone()
    live = claim is not None and claim["token_digest"] is not None and \
        claim["last_beat_at"] + claim["expires_after_seconds"] > t
    rows = cur.execute("SELECT user,device,client,last_beat_at,interval_seconds,"
                       "expires_after_seconds FROM graph_task_activity WHERE node_id=? "
                       "AND last_beat_at+expires_after_seconds>? ORDER BY user,device,client",
                       (node_id, t)).fetchall()
    progress = None
    if live and task["room_id"] is not None:
        found = cur.execute("SELECT MAX(created_at) AS last_progress FROM chat_message "
                            "WHERE room_id=? AND task_node_id=? AND sender_user=? AND sender_device=? AND "
                            "sender_client=? AND message_kind='progress' AND created_at>=?",
                            (task["room_id"], node_id, claim["holder_user"], claim["holder_device"],
                             claim["holder_client"], claim["claimed_at"])).fetchone()
        progress = found["last_progress"] if found else None
    out = {"node_id": node_id, "room_id": task["room_id"], "status": task["status"],
            "status_mode": task["status_mode"],
            "required_capabilities": json.loads(task["required_capabilities_json"]),
            "effective_status": ("unclaimed" if task["status"] == "in_progress" and not live
                                 else task["status"]),
            "active_agents": [{"address": (r["user"], r["device"], r["client"]),
                               "last_beat_at": r["last_beat_at"],
                               "interval_seconds": r["interval_seconds"],
                               "expires_at": r["last_beat_at"] + r["expires_after_seconds"]}
                              for r in rows]}
    if live:
        out["claim"] = {"holder": (claim["holder_user"], claim["holder_device"],
                                   claim["holder_client"]), "generation": claim["generation"],
                        "last_beat_at": claim["last_beat_at"],
                        "expires_at": claim["last_beat_at"] + claim["expires_after_seconds"],
                        "interval_seconds": claim["interval_seconds"],
                        "last_progress_at": progress,
                        # Only a ROOM task can be overdue. A room-less marker is a supported shape
                        # (graph_task_enable takes room=None) and ChatStore.send refuses a
                        # task-correlated post whose room is not the task's, so such a task can
                        # never receive progress — computing this unconditionally pinned it true
                        # 15 minutes after any claim, forever, and no heartbeat could clear it.
                        # Peers read that as an abandoned claim. The obligation itself is
                        # room-scoped: only agents doing active room work owe status updates.
                        "progress_overdue": task["room_id"] is not None
                        and t >= (progress or claim["claimed_at"]) + 900}
    return out


def read(db: Database, node_id: str, *, now: Optional[float] = None) -> dict:
    with db.read() as cur:
        return _read(cur, node_id, time.time() if now is None else float(now))


def set_requirements(db: Database, agent_id: str, node_id: str,
                     names: list[str]) -> dict:
    required = capabilities.normalize(names, limit=32)
    with db.write(agent_id, "update graph task capability requirements") as tx:
        _marker(tx.cur, node_id)
        tx.cur.execute("UPDATE graph_task SET required_capabilities_json=?,updated_tx=? "
                       "WHERE node_id=?", (json.dumps(required), tx.tx_id, node_id))
        from . import assignments
        assignments.invalidate_for_task(tx, node_id)
        _event(tx, node_id, "requirements", 0, None, time.time())
    return read(db, node_id)


def activity(db: Database, node_id: str, who: StableAddress, *,
             interval_seconds: int = DEFAULT_INTERVAL,
             expires_after_seconds: int = DEFAULT_EXPIRY,
             now: Optional[float] = None) -> dict:
    stable = _address(who)
    _timing(interval_seconds, expires_after_seconds)
    with db.write_light() as cur:
        t = time.time() if now is None else float(now)
        _marker(cur, node_id)
        cur.execute("INSERT INTO graph_task_activity VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(node_id,user,device,client) DO UPDATE SET "
                    "last_beat_at=MAX(graph_task_activity.last_beat_at,excluded.last_beat_at), "
                    "interval_seconds=excluded.interval_seconds, "
                    "expires_after_seconds=excluded.expires_after_seconds",
                    (node_id, *stable, t, interval_seconds, expires_after_seconds))
        accepted = cur.execute("SELECT last_beat_at FROM graph_task_activity WHERE node_id=? "
                               "AND user=? AND device=? AND client=?", (node_id, *stable)).fetchone()[0]
    return {"node_id": node_id, "address": stable, "last_beat_at": accepted,
            "expires_at": accepted + expires_after_seconds}


def _event(tx, node_id: str, kind: str, generation: int,
           who: StableAddress | None, t: float) -> None:
    tx.cur.execute("INSERT INTO graph_task_event VALUES(?,?,?,?,?,?,?,?,?)",
                   (ulid(), node_id, kind, generation, *(who or (None, None, None)), t, tx.tx_id))


def _transition(tx, node_id: str, status: str) -> None:
    from .graph import _task_transition
    _task_transition(tx, node_id, status)
    tx.cur.execute("UPDATE graph_task SET status=?,updated_tx=? WHERE node_id=?",
                   (status, tx.tx_id, node_id))


def claim(db: Database, agent_id: str, node_id: str, who: StableAddress, *,
          interval_seconds: int = DEFAULT_INTERVAL,
          expires_after_seconds: int = DEFAULT_EXPIRY,
          now: Optional[float] = None) -> dict:
    stable = _address(who)
    _timing(interval_seconds, expires_after_seconds)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db.write(agent_id, "claim graph task") as tx:
        t = time.time() if now is None else float(now)
        task = _marker(tx.cur, node_id)
        if task["status"] == "complete":
            raise Conflict("completed task cannot be claimed")
        current = tx.cur.execute("SELECT * FROM graph_task_claim WHERE node_id=?", (node_id,)).fetchone()
        if current and current["token_digest"] and \
                current["last_beat_at"] + current["expires_after_seconds"] > t:
            raise Conflict("task already claimed; wait for expiry or request a release")
        from . import assignments
        if current and current["token_digest"]:
            assignments._clear_assignment_tx(tx, node_id, "expired", t)
        reserved = assignments._assignee(assignments._row(tx.cur, node_id))
        if reserved is not None and reserved != stable:
            raise Conflict("task is assigned to another agent")
        eligible(tx.cur, node_id, stable)
        generation = 1 + (current["generation"] if current else 0)
        tx.cur.execute("INSERT INTO graph_task_claim VALUES(?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(node_id) DO UPDATE SET holder_user=excluded.holder_user,"
                       "holder_device=excluded.holder_device,holder_client=excluded.holder_client,"
                       "token_digest=excluded.token_digest,generation=excluded.generation,"
                       "claimed_at=excluded.claimed_at,last_beat_at=excluded.last_beat_at,"
                       "interval_seconds=excluded.interval_seconds,"
                       "expires_after_seconds=excluded.expires_after_seconds",
                       (node_id, *stable, digest, generation, t, t,
                        interval_seconds, expires_after_seconds))
        _transition(tx, node_id, "in_progress")
        _event(tx, node_id, "claim", generation, stable, t)
    return {**read(db, node_id, now=t), "claim_token": token}


def _valid_claim(cur, node_id: str, token: str, stable: StableAddress, t: float):
    _marker(cur, node_id)
    if not isinstance(token, str) or not token:
        raise Invalid("claim token is required")
    row = cur.execute("SELECT * FROM graph_task_claim WHERE node_id=?", (node_id,)).fetchone()
    digest = hashlib.sha256(token.encode()).hexdigest()
    if (row is None or row["token_digest"] is None or
            not secrets.compare_digest(row["token_digest"], digest) or
            (row["holder_user"], row["holder_device"], row["holder_client"]) != stable or
            row["last_beat_at"] + row["expires_after_seconds"] <= t):
        raise Conflict("claim is not live or is held by another agent")
    return row


def heartbeat(db: Database, node_id: str, claim_token: str, who: StableAddress, *,
              now: Optional[float] = None) -> dict:
    stable = _address(who)
    with db.write_light() as cur:
        t = time.time() if now is None else float(now)
        lease = _valid_claim(cur, node_id, claim_token, stable, t)
        accepted = max(t, lease["last_beat_at"])
        cur.execute("UPDATE graph_task_claim SET last_beat_at=? WHERE node_id=? "
                    "AND generation=?", (accepted, node_id, lease["generation"]))
    return {"node_id": node_id, "generation": lease["generation"],
            "expires_at": accepted + lease["expires_after_seconds"]}


def _end(db: Database, agent_id: str, node_id: str, claim_token: str,
         who: StableAddress, kind: str, now: Optional[float]) -> dict:
    stable = _address(who)
    with db.write(agent_id, kind + " graph task claim") as tx:
        t = time.time() if now is None else float(now)
        lease = _valid_claim(tx.cur, node_id, claim_token, stable, t)
        if kind == "complete" and _marker(tx.cur, node_id)["status_mode"] != "versioned":
            raise Invalid("completion requires a node schema with a versioned status field")
        status = "complete" if kind == "complete" else "unclaimed"
        _transition(tx, node_id, status)
        tx.cur.execute("UPDATE graph_task_claim SET token_digest=NULL,holder_user=NULL,"
                       "holder_device=NULL,holder_client=NULL WHERE node_id=? AND generation=?",
                       (node_id, lease["generation"]))
        _event(tx, node_id, kind, lease["generation"], stable, t)
        if kind == "complete":
            from . import assignments
            assignments._clear_assignment_tx(tx, node_id, kind, t)
    return read(db, node_id, now=t)


def release(db: Database, agent_id: str, node_id: str, claim_token: str,
            who: StableAddress, *, now: Optional[float] = None) -> dict:
    return _end(db, agent_id, node_id, claim_token, who, "release", now)


def complete(db: Database, agent_id: str, node_id: str, claim_token: str,
             who: StableAddress, *, now: Optional[float] = None) -> dict:
    return _end(db, agent_id, node_id, claim_token, who, "complete", now)


def reap_expired(db: Database, *, now: Optional[float] = None) -> int:
    t = time.time() if now is None else float(now)
    with db.read() as cur:
        due = cur.execute("SELECT 1 FROM graph_task_claim WHERE token_digest IS NOT NULL "
                          "AND last_beat_at+expires_after_seconds<=? LIMIT 1", (t,)).fetchone()
    if not due:
        return 0
    with db.write("task-reaper", "reap expired graph task claims") as tx:
        t = time.time() if now is None else float(now)
        expired = tx.cur.execute("SELECT node_id,generation FROM graph_task_claim "
                                 "WHERE token_digest IS NOT NULL AND "
                                 "last_beat_at+expires_after_seconds<=?", (t,)).fetchall()
        for row in expired:
            _transition(tx, row["node_id"], "unclaimed")
            tx.cur.execute("UPDATE graph_task_claim SET token_digest=NULL,holder_user=NULL,"
                           "holder_device=NULL,holder_client=NULL WHERE node_id=? AND generation=?",
                           (row["node_id"], row["generation"]))
            _event(tx, row["node_id"], "expired", row["generation"], None, t)
            from . import assignments
            assignments._clear_assignment_tx(tx, row["node_id"], "expired", t)
    return len(expired)
