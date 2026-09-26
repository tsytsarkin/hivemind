"""Durable project-local human instruction queue; no instruction executes on the server."""
from __future__ import annotations

import re
import time
from typing import Optional

from . import teams
from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid, NotFound, Tx
from .ids import ulid

MAX_BODY = 64 * 1024
MAX_PENDING = 10_000
MAX_TOTAL = 100_000
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TRANSITIONS = {
    "queued": {"acknowledged", "cancelled"},
    "acknowledged": {"in_progress", "completed", "failed"},
    "in_progress": {"completed", "failed"},
    "completed": set(), "failed": set(), "cancelled": set(),
}


def _public(row, *, now: Optional[float] = None,
            last_seen: Optional[float] = None) -> dict:
    t = time.time() if now is None else now
    return {"id": row["id"], "author": row["author_user"],
            "recipient": (row["recipient_user"], row["recipient_device"],
                          row["recipient_client"]), "room_id": row["room_id"],
            "to_manager": bool(row["to_manager"]), "body": row["body"],
            "state": row["state"], "result": row["result"],
            "retry_of": row["retry_of"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "stalled": row["state"] in ("queued", "acknowledged", "in_progress") and
                       t - row["updated_at"] >= 86400 and
                       (last_seen is None or t - last_seen >= 86400)}


def _last_seen(cur, row) -> Optional[float]:
    result = cur.execute("SELECT MAX(last_activity_at) AS last_seen FROM chat_session "
                         "WHERE user=? AND device=? AND client=?",
                         (row["recipient_user"], row["recipient_device"],
                          row["recipient_client"])).fetchone()
    return result["last_seen"]


def _event(tx: Tx, item_id: str, action: str, actor: str, old: Optional[str],
           new: str, prior: Optional[str], t: float) -> None:
    tx.cur.execute("INSERT INTO agent_instruction_event VALUES(?,?,?,?,?,?,?,?,?)",
                   (ulid(), item_id, action, actor, old, new, prior, t, tx.tx_id))


def enqueue(db: Database, author: str, recipient: StableAddress, body: str,
            retry_key: str, *, room: Optional[str] = None,
            to_manager: bool = False, retry_of: Optional[str] = None) -> dict:
    who = _address(recipient)
    if not isinstance(author, str) or not author or len(author) > 64:
        raise Invalid("author must be a known project user")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY:
        raise Invalid("instruction must have a nonempty body of at most 64 KiB")
    if not isinstance(retry_key, str) or not _KEY.fullmatch(retry_key):
        raise Invalid("retry_key must be 1–64 ASCII letters, digits, dots, colons, underscores or dashes")
    if to_manager and not room:
        raise Invalid("manager-directed instructions require an existing room")
    if type(to_manager) is not bool:
        raise Invalid("to_manager must be a boolean")
    t = time.time()
    with db.write(author, "enqueue instruction") as tx:
        room_id = teams._room(tx.cur, db, room) if room else None
        if to_manager:
            current, _ = teams._current(tx.cur, room_id)
            if current is None:
                raise Conflict("room has no manager; promote one before enqueuing")
            who = current
        previous = tx.cur.execute("SELECT * FROM agent_instruction WHERE author_user=? "
                                  "AND retry_key=?", (author, retry_key)).fetchone()
        if previous is not None:
            if (previous["body"] != body or previous["room_id"] != room_id or
                    bool(previous["to_manager"]) != to_manager or
                    previous["retry_of"] != retry_of or
                    (not to_manager and (previous["recipient_user"], previous["recipient_device"],
                     previous["recipient_client"]) != who)):
                raise Conflict("idempotency key was used for a different instruction")
            return _public(previous, now=t)
        if retry_of is not None:
            old = tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                 (retry_of,)).fetchone()
            if old is None or (old["state"] != "failed" and not _public(
                    old, now=t, last_seen=_last_seen(tx.cur, old))["stalled"]):
                raise Conflict("retry requires an existing failed or stalled instruction")
        pending = tx.cur.execute("SELECT COUNT(*) AS n FROM agent_instruction WHERE state IN "
                                 "('queued','acknowledged','in_progress')").fetchone()["n"]
        total = tx.cur.execute("SELECT COUNT(*) AS n FROM agent_instruction").fetchone()["n"]
        if pending >= MAX_PENDING or total >= MAX_TOTAL:
            raise Conflict("instruction queue quota reached; resolve or archive old work")
        item_id = ulid()
        tx.cur.execute("INSERT INTO agent_instruction VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (item_id, author, *who, room_id, int(to_manager), body, "queued",
                        None, retry_key, retry_of, t, t, tx.tx_id))
        _event(tx, item_id, "enqueue", author, None, "queued", None, t)
        return _public(tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                      (item_id,)).fetchone(), now=t)


def inbox(db: Database, recipient: StableAddress, *, after_id: Optional[str] = None,
          limit: int = 100) -> dict:
    who = _address(recipient)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if after_id is not None and (not isinstance(after_id, str) or len(after_id) > 64):
        raise Invalid("invalid cursor")
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM agent_instruction WHERE recipient_user=? "
                           "AND recipient_device=? AND recipient_client=? AND id>? "
                           "ORDER BY id LIMIT ?", (*who, after_id or "", limit)).fetchall()
        items = [_public(row, last_seen=_last_seen(cur, row)) for row in rows]
    return {"instructions": items, "count": len(items),
            "next_cursor": items[-1]["id"] if items else None}


def transition(db: Database, recipient: StableAddress, instruction_id: str,
               expected_state: str, new_state: str, result: Optional[str] = None) -> dict:
    who = _address(recipient)
    if new_state not in TRANSITIONS.get(expected_state, set()) or new_state == "cancelled":
        raise Invalid("invalid agent instruction state transition")
    if result is not None and (not isinstance(result, str) or
                               len(result.encode("utf-8")) > MAX_BODY):
        raise Invalid("result exceeds 64 KiB")
    t = time.time()
    with db.write(who[0], "update instruction") as tx:
        row = tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                             (instruction_id,)).fetchone()
        if row is None:
            raise NotFound("instruction not found")
        if (row["recipient_user"], row["recipient_device"], row["recipient_client"]) != who:
            raise Conflict("instruction is addressed to another agent")
        if row["state"] != expected_state:
            raise Conflict("instruction state changed; fetch it before retrying")
        tx.cur.execute("UPDATE agent_instruction SET state=?,result=?,updated_at=? WHERE id=?",
                       (new_state, result, t, instruction_id))
        _event(tx, instruction_id, new_state, "-".join(who), expected_state, new_state,
               None, t)
        return _public(tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                      (instruction_id,)).fetchone(), now=t)


def cancel(db: Database, author: str, instruction_id: str) -> dict:
    t = time.time()
    with db.write(author, "cancel instruction") as tx:
        row = tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                             (instruction_id,)).fetchone()
        if row is None:
            raise NotFound("instruction not found")
        if row["state"] != "queued":
            raise Conflict("only queued instructions may be cancelled")
        tx.cur.execute("UPDATE agent_instruction SET state='cancelled',updated_at=? WHERE id=?",
                       (t, instruction_id))
        _event(tx, instruction_id, "cancel", author, "queued", "cancelled", None, t)
        return _public(tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                      (instruction_id,)).fetchone(), now=t)


def handoff_queued(tx: Tx, room_id: str, new_manager: StableAddress) -> int:
    target = _address(new_manager)
    rows = tx.cur.execute("SELECT * FROM agent_instruction WHERE room_id=? AND to_manager=1 "
                           "AND state='queued'", (room_id,)).fetchall()
    t = time.time()
    for row in rows:
        prior = (row["recipient_user"], row["recipient_device"], row["recipient_client"])
        if prior == target:
            continue
        tx.cur.execute("UPDATE agent_instruction SET recipient_user=?,recipient_device=?,"
                       "recipient_client=?,updated_at=? WHERE id=?", (*target, t, row["id"]))
        _event(tx, row["id"], "manager_handoff", "-".join(target), "queued", "queued",
               "-".join(prior), t)
    return len(rows)


def list_project(db: Database, *, after_id: Optional[str] = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM agent_instruction WHERE id>? ORDER BY id LIMIT ?",
                           (after_id or "", limit)).fetchall()
        items = [_public(row, last_seen=_last_seen(cur, row)) for row in rows]
    return {"instructions": items, "count": len(items),
            "next_cursor": items[-1]["id"] if items else None}
