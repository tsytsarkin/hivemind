"""Durable project-local human instruction queue; no instruction executes on the server."""
from __future__ import annotations

import hashlib
import json
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
TERMINAL_RETENTION = 30 * 86400
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


def _request_digest(body: str, room_id: Optional[str], to_manager: bool,
                    recipient: StableAddress | None, retry_of: Optional[str]) -> str:
    payload = [body, room_id, to_manager, recipient if not to_manager else None, retry_of]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _archive_terminal(tx: Tx, now: float, *, limit: int = 1000) -> int:
    """Compact old terminal bodies but retain durable author/recipient/status audit metadata."""
    rows = tx.cur.execute(
        "SELECT * FROM agent_instruction i WHERE i.state IN ('completed','failed','cancelled') "
        "AND i.updated_at<=? AND NOT EXISTS (SELECT 1 FROM agent_instruction c "
        "WHERE c.retry_of=i.id) ORDER BY i.updated_at,i.id LIMIT ?",
        (now - TERMINAL_RETENTION, limit)).fetchall()
    for row in rows:
        recipient = (row["recipient_user"], row["recipient_device"], row["recipient_client"])
        digest = _request_digest(row["body"], row["room_id"], bool(row["to_manager"]),
                                 recipient, row["retry_of"])
        count = tx.cur.execute("SELECT COUNT(*) AS n FROM agent_instruction_event WHERE "
                               "instruction_id=?", (row["id"],)).fetchone()["n"]
        tx.cur.execute("INSERT INTO agent_instruction_archive VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (row["id"], row["author_user"], *recipient, row["room_id"],
                        row["to_manager"], row["state"], row["retry_key"], row["retry_of"],
                        row["created_at"], row["updated_at"], now, digest, count))
        tx.cur.execute(
            "INSERT INTO agent_instruction_archive_event "
            "SELECT id,instruction_id,action,actor,old_state,new_state,prior_recipient,"
            "created_at,tx_id,? FROM agent_instruction_event WHERE instruction_id=?",
            (now, row["id"]))
        tx.cur.execute("DELETE FROM agent_instruction_event WHERE instruction_id=?", (row["id"],))
        tx.cur.execute("DELETE FROM agent_instruction_delivery WHERE instruction_id=?",
                       (row["id"],))
        tx.cur.execute("DELETE FROM agent_instruction WHERE id=?", (row["id"],))
    return len(rows)


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
        archived = tx.cur.execute("SELECT * FROM agent_instruction_archive WHERE "
                                  "author_user=? AND retry_key=?", (author, retry_key)).fetchone()
        if archived is not None:
            digest = _request_digest(body, room_id, to_manager, who, retry_of)
            if archived["request_digest"] != digest:
                raise Conflict("idempotency key was used for a different instruction")
            return {"id": archived["id"], "state": archived["state"], "archived": True}
        if retry_of is not None:
            old = tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                 (retry_of,)).fetchone()
            if old is None or (old["state"] != "failed" and not _public(
                    old, now=t, last_seen=_last_seen(tx.cur, old))["stalled"]):
                raise Conflict("retry requires an existing failed or stalled instruction")
        pending = tx.cur.execute("SELECT COUNT(*) AS n FROM agent_instruction WHERE state IN "
                                 "('queued','acknowledged','in_progress')").fetchone()["n"]
        total = tx.cur.execute("SELECT COUNT(*) AS n FROM agent_instruction").fetchone()["n"]
        if total >= MAX_TOTAL:
            total -= _archive_terminal(tx, t)
        if pending >= MAX_PENDING or total >= MAX_TOTAL:
            raise Conflict("instruction queue quota reached; resolve old work or archive terminal work older than 30 days")
        item_id = ulid()
        tx.cur.execute("INSERT INTO agent_instruction VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (item_id, author, *who, room_id, int(to_manager), body, "queued",
                        None, retry_key, retry_of, t, t, tx.tx_id))
        tx.cur.execute("INSERT INTO agent_instruction_delivery(instruction_id,recipient_user,"
                       "recipient_device,recipient_client,created_at,tx_id) VALUES(?,?,?,?,?,?)",
                       (item_id, *who, t, tx.tx_id))
        _event(tx, item_id, "enqueue", author, None, "queued", None, t)
        return _public(tx.cur.execute("SELECT * FROM agent_instruction WHERE id=?",
                                      (item_id,)).fetchone(), now=t)


def inbox(db: Database, recipient: StableAddress, *, after_id: Optional[str] = None,
          limit: int = 100) -> dict:
    who = _address(recipient)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if after_id is not None and (not isinstance(after_id, str) or not after_id.isascii() or
                                 not after_id.isdecimal() or len(after_id) > 19):
        raise Invalid("after_id must be the decimal delivery cursor from next_cursor")
    with db.read() as cur:
        rows = cur.execute("SELECT i.*, d.seq AS delivery_seq FROM agent_instruction_delivery d "
                           "JOIN agent_instruction i ON i.id=d.instruction_id "
                           "WHERE d.recipient_user=? AND d.recipient_device=? AND "
                           "d.recipient_client=? AND i.recipient_user=? AND "
                           "i.recipient_device=? AND i.recipient_client=? AND d.seq>? AND "
                           "d.seq=(SELECT MAX(x.seq) FROM agent_instruction_delivery x "
                           "WHERE x.instruction_id=i.id) ORDER BY d.seq LIMIT ?",
                           (*who, *who, int(after_id or 0), limit)).fetchall()
        items = [_public(row, last_seen=_last_seen(cur, row)) for row in rows]
    return {"instructions": items, "count": len(items),
            "next_cursor": str(rows[-1]["delivery_seq"]) if rows else None}


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
        if (row["to_manager"] and row["state"] == "queued" and
                teams._current(tx.cur, row["room_id"])[0] != who):
            raise Conflict("queued manager instruction awaits the current room manager")
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
        tx.cur.execute("INSERT INTO agent_instruction_delivery(instruction_id,recipient_user,"
                       "recipient_device,recipient_client,created_at,tx_id) VALUES(?,?,?,?,?,?)",
                       (row["id"], *target, t, tx.tx_id))
        _event(tx, row["id"], "manager_handoff", "-".join(target), "queued", "queued",
               "-".join(prior), t)
    return len(rows)


def list_project(db: Database, *, after_id: Optional[str] = None,
                 before_id: Optional[str] = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if before_id is not None and (not isinstance(before_id, str) or len(before_id) > 64):
        raise Invalid("invalid before_id cursor")
    with db.read() as cur:
        if after_id is not None and before_id is None:
            rows = cur.execute("SELECT * FROM agent_instruction WHERE id>? ORDER BY id LIMIT ?",
                               (after_id, limit)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM agent_instruction WHERE id<? ORDER BY id DESC LIMIT ?",
                               (before_id or "Z", limit)).fetchall()
        more = bool(rows and cur.execute(
            "SELECT 1 FROM agent_instruction WHERE id<? LIMIT 1",
            (rows[-1]["id"],)).fetchone())
        items = [_public(row, last_seen=_last_seen(cur, row)) for row in rows]
    return {"instructions": items, "count": len(items),
            "next_cursor": items[-1]["id"] if items else None,
            "older_cursor": items[-1]["id"] if more else None}
