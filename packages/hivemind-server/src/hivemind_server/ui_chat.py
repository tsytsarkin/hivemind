"""Deliberate project-wide human console chat view; never mounted on agent MCP."""
from __future__ import annotations

import time
import logging
from pathlib import Path
from typing import Optional

from .chat import ChatStore, StableAddress, _address, MESSAGE_TTL
from . import chat_ws
from .db import Database, Invalid
from .identity import Identity, IdentityStore
from .projects_meta import can_access

log = logging.getLogger(__name__)


def list_transcript(db: Database, *, channel: str, room: Optional[str] = None,
                    after_seq: int = 0, before_seq: Optional[int] = None, limit: int = 100,
                    reader: Optional[StableAddress] = None) -> dict:
    if channel not in ("dm", "room"):
        raise Invalid("channel must be dm or room")
    if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("after_seq must be nonnegative and limit must be 1–100")
    if before_seq is not None and (type(before_seq) is not int or before_seq <= 0):
        raise Invalid("before_seq must be a positive message cursor")
    if channel == "room" and not room:
        raise Invalid("room name is required for room history")
    with db.read() as cur:
        room_id = ChatStore(db)._lookup_room(cur, room) if channel == "room" else None
        where = "channel='room' AND room_id=?" if channel == "room" else "channel='dm'"
        params = (room_id,) if channel == "room" else ()
        cutoff = time.time() - MESSAGE_TTL
        if after_seq > 0 and before_seq is None:
            rows = cur.execute("SELECT * FROM chat_message WHERE " + where +
                               " AND seq>? AND created_at>? ORDER BY seq LIMIT ?",
                               (*params, after_seq, cutoff, limit)).fetchall()
        else:
            rows = list(reversed(cur.execute(
                "SELECT * FROM chat_message WHERE " + where +
                " AND seq<? AND created_at>? ORDER BY seq DESC LIMIT ?",
                (*params, before_seq or 9223372036854775807, cutoff, limit)).fetchall()))
        has_older = bool(rows and cur.execute(
            "SELECT 1 FROM chat_message WHERE " + where + " AND seq<? AND created_at>? LIMIT 1",
            (*params, rows[0]["seq"], cutoff)).fetchone())
        if channel == "room":
            watermark = cur.execute("SELECT expired_through_seq AS seq FROM "
                                    "chat_expiration_watermark WHERE target_key=?",
                                    (ChatStore._target_key("room", room_id),)).fetchone()
        else:
            watermark = cur.execute("SELECT MAX(expired_through_seq) AS seq FROM "
                                    "chat_expiration_watermark WHERE target_key LIKE ?",
                                    ('["dm",%',)).fetchone()
        expired_in_db = cur.execute("SELECT MAX(seq) AS seq FROM chat_message WHERE " + where +
                                    " AND created_at<=? AND seq>?",
                                    (*params, time.time() - MESSAGE_TTL, after_seq)
                                    ).fetchone()["seq"]
        marker = None
        unread_count = 0
        if reader is not None:
            browser = _address(reader)
            if browser[2] != "webui":
                raise Invalid("console cursor requires the authenticated human UI address")
            marker = cur.execute("SELECT seq,message_id FROM console_read_cursor WHERE "
                                 "user=? AND device=? AND channel=? AND conversation=?",
                                 (browser[0], browser[1], channel, room_id or "dm")).fetchone()
            unread_count = cur.execute(
                "SELECT COUNT(*) AS n FROM chat_message WHERE " + where +
                " AND seq>? AND created_at>?",
                (*params, marker["seq"] if marker else 0, cutoff)).fetchone()["n"]
    items = [{**ChatStore._public(row), "recipient":
              (row["target_user"], row["target_device"], row["target_client"])
              if channel == "dm" else None} for row in rows]
    return {"messages": items, "count": len(items), "next_seq": items[-1]["seq"] if items else after_seq,
            "older_cursor": items[0]["seq"] if has_older else None,
            "last_read_seq": marker["seq"] if marker else 0,
            "last_read_message_id": marker["message_id"] if marker else None,
            "unread_count": unread_count,
            "history_gap": bool(expired_in_db is not None or
                                (watermark and watermark["seq"] is not None and
                                 watermark["seq"] > after_seq))}


def mark_console_read(db: Database, reader: StableAddress, *, channel: str,
                      seq: int, room: Optional[str] = None) -> dict:
    who = _address(reader)
    if who[2] != "webui" or channel not in ("dm", "room") or type(seq) is not int or seq < 1:
        raise Invalid("console read marker needs a human address, channel, and message sequence")
    t = time.time()
    with db.write_light() as cur:
        room_id = ChatStore(db)._lookup_room(cur, room) if channel == "room" else None
        if channel == "room" and not room:
            raise Invalid("room name is required")
        clause = "channel='room' AND room_id=?" if channel == "room" else "channel='dm'"
        args = (room_id,) if channel == "room" else ()
        found = cur.execute("SELECT message_id FROM chat_message WHERE " + clause +
                            " AND seq=? AND created_at>?", (*args, seq, t - MESSAGE_TTL)).fetchone()
        if found is None:
            raise Invalid("read cursor must name an unexpired project message in this channel")
        cur.execute("INSERT INTO console_read_cursor VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(user,device,channel,conversation) DO UPDATE SET "
                    "seq=MAX(console_read_cursor.seq,excluded.seq),"
                    "message_id=CASE WHEN excluded.seq>=console_read_cursor.seq "
                    "THEN excluded.message_id ELSE console_read_cursor.message_id END,"
                    "updated_at=excluded.updated_at",
                    (who[0], who[1], channel, room_id or "dm", seq, found["message_id"], t))
        stored = cur.execute("SELECT seq,message_id FROM console_read_cursor WHERE "
                             "user=? AND device=? AND channel=? AND conversation=?",
                             (who[0], who[1], channel, room_id or "dm")).fetchone()
    return {"last_read_seq": stored["seq"], "last_read_message_id": stored["message_id"]}


def send_human_dm(db: Database, who: Identity, identities: IdentityStore,
                  to: StableAddress, body: str, retry_key: str, *, project_meta=None) -> dict:
    recipient = _address(to)
    if not identities.has_device(recipient[0], recipient[1]) or (project_meta is not None and
            not can_access(Identity(recipient[0], recipient[1]), project_meta)):
        raise Invalid("recipient user/device does not exist or lacks project access")
    return ChatStore(db).send("dm", recipient, (who.user, who.device, "webui"), body,
                              retry_key, sender_origin="human_ui")


async def notify_human_dm(project_dir: Path, to: StableAddress, message: dict) -> bool:
    """Best-effort post-commit push; the durable message is the source of truth on failure."""
    frame = {"v": 2, "type": "chat", "id": message["id"], "channel": "dm", "room": None,
             "from": "-".join(message["sender"]),
             "preview": message["body"].encode("utf-8")[:160].decode("utf-8", errors="ignore"),
             "ts": message["created_at"]}
    try:
        return await chat_ws.hub_for(project_dir).notify(_address(to), frame) > 0
    except Exception:
        log.exception("live browser DM notification failed; message remains persisted")
        return False
