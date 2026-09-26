"""Opt-in graph task marker, heartbeat sidecars and fenced claim lifecycle."""
from __future__ import annotations

import time
from typing import Optional

from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid, NotFound

DEFAULT_INTERVAL = 300
DEFAULT_EXPIRY = 3600


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


def enable(db: Database, agent_id: str, node_id: str, room_id: Optional[str] = None) -> dict:
    with db.write(agent_id, "enable graph task marker") as tx:
        cur = tx.cur
        row = cur.execute("SELECT redirect_to FROM node WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            raise NotFound("graph node does not exist")
        if row["redirect_to"] is not None:
            raise Invalid("cannot mark a redirected graph node as a task")
        if room_id is not None and not cur.execute(
                "SELECT 1 FROM chat_room WHERE room_id=?", (room_id,)).fetchone():
            raise NotFound("task room does not exist; create it explicitly")
        if cur.execute("SELECT 1 FROM graph_task WHERE node_id=?", (node_id,)).fetchone():
            raise Conflict("node already marked as a task")
        cur.execute("INSERT INTO graph_task VALUES(?,?,?,?,?)",
                    (node_id, room_id, "unclaimed", tx.tx_id, tx.tx_id))
    return read(db, node_id)


def _read(cur, node_id: str, t: float) -> dict:
    task = _marker(cur, node_id)
    rows = cur.execute("SELECT user,device,client,last_beat_at,interval_seconds,"
                       "expires_after_seconds FROM graph_task_activity WHERE node_id=? "
                       "AND last_beat_at+expires_after_seconds>? ORDER BY user,device,client",
                       (node_id, t)).fetchall()
    return {"node_id": node_id, "room_id": task["room_id"], "status": task["status"],
            "effective_status": task["status"],
            "active_agents": [{"address": (r["user"], r["device"], r["client"]),
                               "last_beat_at": r["last_beat_at"],
                               "interval_seconds": r["interval_seconds"],
                               "expires_at": r["last_beat_at"] + r["expires_after_seconds"]}
                              for r in rows]}


def read(db: Database, node_id: str, *, now: Optional[float] = None) -> dict:
    with db.read() as cur:
        return _read(cur, node_id, time.time() if now is None else float(now))


def activity(db: Database, node_id: str, who: StableAddress, *,
             interval_seconds: int = DEFAULT_INTERVAL,
             expires_after_seconds: int = DEFAULT_EXPIRY,
             now: Optional[float] = None) -> dict:
    stable = _address(who)
    _timing(interval_seconds, expires_after_seconds)
    t = time.time() if now is None else float(now)
    with db.write_light() as cur:
        _marker(cur, node_id)
        cur.execute("INSERT INTO graph_task_activity VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(node_id,user,device,client) DO UPDATE SET "
                    "last_beat_at=excluded.last_beat_at, "
                    "interval_seconds=excluded.interval_seconds, "
                    "expires_after_seconds=excluded.expires_after_seconds",
                    (node_id, *stable, t, interval_seconds, expires_after_seconds))
    return {"node_id": node_id, "address": stable, "last_beat_at": t,
            "expires_at": t + expires_after_seconds}
