"""Project-local mandatory task reservations and fenced reassignment."""
from __future__ import annotations

import json
import time
from typing import Optional

from . import graph_tasks, teams
from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid, Tx
from .ids import ulid


def _assignee(row) -> StableAddress | None:
    if row is None or row["assignee_user"] is None:
        return None
    return row["assignee_user"], row["assignee_device"], row["assignee_client"]


def _row(cur, node_id: str):
    return cur.execute("SELECT * FROM graph_task_assignment WHERE node_id=?",
                       (node_id,)).fetchone()


def _event(tx: Tx, node_id: str, reason: str, target: StableAddress | None,
           previous: StableAddress | None, revision: int, now: float) -> None:
    tx.cur.execute("INSERT INTO graph_task_assignment_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ulid(), node_id, reason, *(target or (None, None, None)),
                    *(previous or (None, None, None)), revision, now, tx.tx_id))


def _clear_assignment_tx(tx: Tx, node_id: str, reason: str, now: float) -> bool:
    row = _row(tx.cur, node_id)
    previous = _assignee(row)
    if previous is None:
        return False
    revision = row["revision"] + 1
    tx.cur.execute("UPDATE graph_task_assignment SET assignee_user=NULL,assignee_device=NULL,"
                   "assignee_client=NULL,revision=?,assigned_at=NULL WHERE node_id=?",
                   (revision, node_id))
    _event(tx, node_id, reason, None, previous, revision, now)
    return True


def _clear_tx(tx: Tx, node_id: str, reason: str, now: float) -> None:
    task = graph_tasks._marker(tx.cur, node_id)
    lease = tx.cur.execute("SELECT generation,token_digest,holder_user,holder_device,"
                           "holder_client FROM graph_task_claim WHERE node_id=?",
                           (node_id,)).fetchone()
    if lease is not None and lease["token_digest"] is not None:
        if task["status"] == "in_progress":
            graph_tasks._transition(tx, node_id, "unclaimed")
        tx.cur.execute("UPDATE graph_task_claim SET token_digest=NULL,holder_user=NULL,"
                       "holder_device=NULL,holder_client=NULL WHERE node_id=?",
                       (node_id,))
        graph_tasks._event(tx, node_id, reason, lease["generation"],
                           (lease["holder_user"], lease["holder_device"],
                            lease["holder_client"]), now)
    _clear_assignment_tx(tx, node_id, reason, now)


def assign(db: Database, agent_id: str, node_id: str, target: StableAddress,
           *, expected_revision: Optional[int] = None,
           manager_actor: Optional[StableAddress] = None,
           confirm_displace: bool = True,
           now: Optional[float] = None) -> dict:
    stable = _address(target)
    t = time.time() if now is None else float(now)
    with db.write(agent_id, "assign graph task") as tx:
        task = graph_tasks._marker(tx.cur, node_id)
        room_id = task["room_id"]
        if room_id is None:
            raise Invalid("assignment requires a task linked to an existing room")
        if task["status"] == "complete":
            raise Conflict("completed task cannot be assigned")
        if not teams._member(tx.cur, room_id, stable):
            raise Invalid("assignee must be a member of the task's room")
        if manager_actor is not None and teams._current(tx.cur, room_id)[0] != _address(manager_actor):
            raise Conflict("only the current room manager can assign this task")
        graph_tasks.eligible(tx.cur, node_id, stable)
        row = _row(tx.cur, node_id)
        revision = row["revision"] if row else 0
        if expected_revision is not None and (type(expected_revision) is not int or
                                              revision != expected_revision):
            raise Conflict("task assignment changed; fetch its revision and retry")
        previous = _assignee(row)
        if previous != stable:
            old_claim = tx.cur.execute("SELECT token_digest,last_beat_at,expires_after_seconds "
                                       "FROM graph_task_claim "
                                       "WHERE node_id=?", (node_id,)).fetchone()
            if old_claim and old_claim["token_digest"] is not None:
                if (old_claim["last_beat_at"] + old_claim["expires_after_seconds"] > t and
                        confirm_displace is not True):
                    raise Conflict("active claim would be displaced; confirm the target and retry")
                _clear_tx(tx, node_id, "reassigned", t)
                row = _row(tx.cur, node_id)
                revision = row["revision"] if row else revision
            revision += 1
            tx.cur.execute("INSERT INTO graph_task_assignment(node_id,room_id,assignee_user,"
                           "assignee_device,assignee_client,revision,assigned_at) "
                           "VALUES(?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
                           "assignee_user=excluded.assignee_user,"
                           "assignee_device=excluded.assignee_device,"
                           "assignee_client=excluded.assignee_client,"
                           "revision=excluded.revision,assigned_at=excluded.assigned_at",
                           (node_id, room_id, *stable, revision, t))
            _event(tx, node_id, "assigned", stable, previous, revision, t)
    return view(db, node_id, now=t)


def clear(db: Database, agent_id: str, node_id: str, reason: str,
          *, expected_revision: int, manager_actor: Optional[StableAddress] = None,
          confirm_displace: bool = True) -> dict:
    if type(expected_revision) is not int or expected_revision < 0:
        raise Invalid("expected_revision must be the current assignment revision")
    with db.write(agent_id, reason) as tx:
        row = _row(tx.cur, node_id)
        if (row["revision"] if row else 0) != expected_revision:
            raise Conflict("task assignment revision changed; fetch it before clearing")
        claim = tx.cur.execute("SELECT token_digest,last_beat_at,expires_after_seconds "
                               "FROM graph_task_claim WHERE node_id=?", (node_id,)).fetchone()
        if (claim and claim["token_digest"] is not None and
                claim["last_beat_at"] + claim["expires_after_seconds"] > time.time() and
                confirm_displace is not True):
            raise Conflict("active claim would be revoked; confirm the target and retry")
        if manager_actor is not None:
            task = graph_tasks._marker(tx.cur, node_id)
            if task["room_id"] is None or teams._current(tx.cur, task["room_id"])[0] != \
                    _address(manager_actor):
                raise Conflict("only the current room manager can clear this assignment")
        _clear_tx(tx, node_id, reason, time.time())
    return view(db, node_id)


def view(db: Database, node_id: str, *, now: Optional[float] = None) -> dict:
    t = time.time() if now is None else float(now)
    with db.read() as cur:
        task = graph_tasks._marker(cur, node_id)
        row = _row(cur, node_id)
        claim = cur.execute("SELECT * FROM graph_task_claim WHERE node_id=?",
                            (node_id,)).fetchone()
        live = claim is not None and claim["token_digest"] is not None and \
            claim["last_beat_at"] + claim["expires_after_seconds"] > t
        if task["status"] == "complete":
            state = "complete"
        elif live:
            state = "in_progress"
        elif task["status"] == "in_progress" and claim is not None:
            state = "available"  # effective expiry precedes the reaper's transaction
        elif _assignee(row) is not None:
            state = "assigned_waiting"
        else:
            state = "available"
        holder = ((claim["holder_user"], claim["holder_device"], claim["holder_client"])
                  if live else None)
    return {"node_id": node_id, "room_id": task["room_id"], "state": state,
            "assignee": holder or (_assignee(row) if state == "assigned_waiting" else None),
            "revision": row["revision"] if row else 0,
            "required_capabilities": json.loads(task["required_capabilities_json"])}


def invalidate_for_task(tx: Tx, node_id: str, *, now: Optional[float] = None) -> None:
    t = time.time() if now is None else float(now)
    row = _row(tx.cur, node_id)
    claim = tx.cur.execute("SELECT holder_user,holder_device,holder_client,token_digest "
                           "FROM graph_task_claim WHERE node_id=?", (node_id,)).fetchone()
    holder = ((claim["holder_user"], claim["holder_device"], claim["holder_client"])
              if claim is not None and claim["token_digest"] is not None else None)
    candidates = {who for who in (_assignee(row), holder) if who is not None}
    for who in candidates:
        try:
            graph_tasks.eligible(tx.cur, node_id, who)
        except Invalid:
            _clear_tx(tx, node_id, "capability_lost", t)
            break


def invalidate_for_agent(tx: Tx, who: StableAddress) -> None:
    stable = _address(who)
    rows = tx.cur.execute("SELECT node_id FROM graph_task_assignment WHERE assignee_user=? "
                          "AND assignee_device=? AND assignee_client=? UNION "
                          "SELECT node_id FROM graph_task_claim WHERE holder_user=? AND "
                          "holder_device=? AND holder_client=? AND token_digest IS NOT NULL",
                          (*stable, *stable)).fetchall()
    for row in rows:
        invalidate_for_task(tx, row["node_id"])


def mine(db: Database, who: StableAddress) -> list[dict]:
    stable = _address(who)
    with db.read() as cur:
        nodes = [row["node_id"] for row in cur.execute(
            "SELECT node_id FROM graph_task_assignment WHERE assignee_user=? AND "
            "assignee_device=? AND assignee_client=? ORDER BY assigned_at", stable).fetchall()]
    return [state for node_id in nodes if (state := view(db, node_id))["state"] ==
            "assigned_waiting"]


def list_room(db: Database, room: str, *, limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    with db.read() as cur:
        room_id = teams._room(cur, db, room)
        rows = cur.execute("SELECT node_id FROM graph_task WHERE room_id=? "
                           "ORDER BY node_id DESC LIMIT ?", (room_id, limit)).fetchall()
    return [view(db, row["node_id"]) for row in rows]
