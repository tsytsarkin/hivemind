"""Audited memberships and one fenced manager per project room."""
from __future__ import annotations

import time

from .chat import ChatStore, StableAddress, _address
from .db import Conflict, Database, Invalid, now_iso
from .ids import ulid


def _room(cur, db: Database, name: str) -> str:
    return ChatStore(db)._lookup_room(cur, name)


def _member(cur, room_id: str, who: StableAddress) -> bool:
    return cur.execute("SELECT 1 FROM chat_subscription WHERE room_id=? AND user=? "
                       "AND device=? AND client=?", (room_id, *_address(who))).fetchone() is not None


def _current(cur, room_id: str) -> tuple[StableAddress | None, int]:
    row = cur.execute("SELECT user,device,client,revision FROM room_manager "
                      "WHERE room_id=?", (room_id,)).fetchone()
    if row is None:
        return None, 0
    return ((row["user"], row["device"], row["client"]) if row["user"] is not None
            else None), row["revision"]


def _event(tx, room_id: str, action: str, actor: StableAddress, target: StableAddress,
           previous: StableAddress | None, revision: int) -> None:
    tx.cur.execute("INSERT INTO room_team_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ulid(), room_id, action, *_address(actor), *_address(target),
                    *(previous or (None, None, None)), revision, tx.tx_id))


def add_member(db: Database, room: str, target: StableAddress,
               actor: StableAddress) -> dict:
    stable = _address(target)
    with db.write("room-membership", "add room member") as tx:
        room_id = _room(tx.cur, db, room)
        tx.cur.execute("INSERT INTO chat_subscription(room_id,user,device,client,joined_at) "
                       "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                       (room_id, *stable, now_iso()))
        if tx.cur.rowcount:
            _event(tx, room_id, "add_member", actor, stable, None, _current(tx.cur, room_id)[1])
    return {"room": room, "member": stable, "subscribed": True}


def remove_member(db: Database, room: str, target: StableAddress,
                  actor: StableAddress) -> dict:
    stable = _address(target)
    with db.write("room-membership", "remove room member") as tx:
        room_id = _room(tx.cur, db, room)
        held = tx.cur.execute(
            "SELECT t.status,c.token_digest,c.last_beat_at,c.expires_after_seconds "
            "FROM graph_task_assignment a JOIN graph_task t ON t.node_id=a.node_id "
            "LEFT JOIN graph_task_claim c ON c.node_id=a.node_id "
            "WHERE a.room_id=? AND a.assignee_user=? AND a.assignee_device=? "
            "AND a.assignee_client=?", (room_id, *stable)).fetchall()
        if any(row["status"] == "unclaimed" or
               (row["token_digest"] is not None and row["last_beat_at"] +
                row["expires_after_seconds"] > time.time()) for row in held):
            raise Conflict("member has a waiting or active assignment; reassign it first")
        previous, revision = _current(tx.cur, room_id)
        tx.cur.execute("DELETE FROM chat_subscription WHERE room_id=? AND user=? AND device=? "
                       "AND client=?", (room_id, *stable))
        if tx.cur.rowcount:
            if previous == stable:
                revision += 1
                tx.cur.execute("UPDATE room_manager SET user=NULL,device=NULL,client=NULL,"
                               "revision=? WHERE room_id=?", (revision, room_id))
            _event(tx, room_id, "remove_member", actor, stable, previous, revision)
    return {"room": room, "member": stable, "subscribed": False}


def manager(db: Database, room: str) -> dict:
    with db.read() as cur:
        room_id = _room(cur, db, room)
        who, revision = _current(cur, room_id)
    return {"room": room, "manager": who, "revision": revision}


def list_members(db: Database, room: str) -> list[StableAddress]:
    return ChatStore(db).subscribers(room)


def promote(db: Database, room: str, target: StableAddress, actor: StableAddress,
            *, expected_revision: int) -> dict:
    stable = _address(target)
    if type(expected_revision) is not int or expected_revision < 0:
        raise Invalid("expected_revision must be a nonnegative integer")
    with db.write("room-manager", "promote room manager") as tx:
        room_id = _room(tx.cur, db, room)
        if not _member(tx.cur, room_id, stable):
            raise Invalid("manager must be a member of this room")
        previous, revision = _current(tx.cur, room_id)
        if revision != expected_revision:
            raise Conflict("room manager changed; fetch its revision and retry")
        revision += 1
        tx.cur.execute("INSERT INTO room_manager(room_id,user,device,client,revision) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET "
                       "user=excluded.user,device=excluded.device,client=excluded.client,"
                       "revision=excluded.revision", (room_id, *stable, revision))
        from . import instructions
        instructions.handoff_queued(tx, room_id, stable)
        _event(tx, room_id, "promote", actor, stable, previous, revision)
    return {"room": room, "manager": stable, "previous_manager": previous,
            "revision": revision}
