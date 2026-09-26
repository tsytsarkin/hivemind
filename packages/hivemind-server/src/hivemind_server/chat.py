"""Project-local durable chat metadata, separate from the legacy ephemeral bus."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Optional

from .db import Conflict, Database, Invalid, NotFound, now_iso
from .identity import Identity, validate_username
from .ids import ulid


StableAddress = tuple[str, str, str]
_DEVICE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CLIENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SESSION = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ROOM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_ROOMS = 1000
MESSAGE_TTL = 24 * 3600
MAX_BODY_BYTES = 256 * 1024
MAX_CHAT_BYTES = 512 * 1024 * 1024
MAX_CHAT_MESSAGES = 100_000
MAX_PAGE = 100
MESSAGE_OVERHEAD = 512
_RETRY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def stable_identity(who: Optional[Identity], client: str, session_id: str) -> tuple[str, str, str, str]:
    """Bind the durable address to a real token identity, never a supplied username."""
    if who is None or who.legacy:
        raise Invalid("durable chat needs a user/device token, not a legacy project token")
    validate_username(who.user)
    if not isinstance(who.device, str) or not _DEVICE.fullmatch(who.device):
        raise Invalid("durable chat requires a lowercase device name in the user token")
    if not isinstance(client, str) or not _CLIENT.fullmatch(client):
        raise Invalid("client must be a 1–32 character lowercase slug")
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise Invalid("session_id must be a 1–64 character lowercase slug")
    parts = who.user, who.device, client, session_id
    if len("-".join(parts).encode("utf-8")) > 256:
        raise Invalid("session display label exceeds 256 UTF-8 bytes")
    return parts


def _address(stable: StableAddress) -> StableAddress:
    if not isinstance(stable, (tuple, list)) or len(stable) != 3:
        raise Invalid("expected a (user, device, client) identity")
    user, device, client = stable
    validate_username(user)
    if not isinstance(device, str) or not _DEVICE.fullmatch(device):
        raise Invalid("device must be a 1–64 character lowercase slug")
    if not isinstance(client, str) or not _CLIENT.fullmatch(client):
        raise Invalid("client must be a 1–32 character lowercase slug")
    return user, device, client


def _room_name(name: str) -> str:
    if not isinstance(name, str) or not _ROOM.fullmatch(name):
        raise Invalid("room name must be a 1–64 character lowercase slug")
    return name


class ChatStore:
    def __init__(self, db: Database, *, max_bytes: int = MAX_CHAT_BYTES,
                 max_messages: int = MAX_CHAT_MESSAGES):
        self.db = db
        self.max_bytes = max_bytes
        self.max_messages = max_messages

    def create_room(self, name: str, description: str, who: StableAddress) -> dict:
        name = _room_name(name)
        user, device, client = _address(who)
        if not isinstance(description, str):
            raise Invalid("room description must be text")
        description = description.strip()
        if not 1 <= len(description) <= 256 or len(description.encode("utf-8")) > 1024:
            raise Invalid("room description must be 1–256 characters and at most 1024 UTF-8 bytes")
        room = {"room_id": ulid(), "name": name, "description": description,
                "creator": (user, device, client), "created_at": now_iso()}
        with self.db.write_light() as cur:
            if cur.execute("SELECT 1 FROM chat_room WHERE name=?", (name,)).fetchone():
                raise Conflict(f"room {name!r} already exists")
            if cur.execute("SELECT COUNT(*) AS n FROM chat_room").fetchone()["n"] >= MAX_ROOMS:
                raise Invalid("project room limit reached")
            try:
                cur.execute("INSERT INTO chat_room VALUES(?,?,?,?,?,?,?)",
                            (room["room_id"], name, description, user, device, client,
                             room["created_at"]))
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"room {name!r} already exists") from exc
        return room

    def rooms(self) -> list[dict]:
        with self.db.read() as cur:
            rows = cur.execute("SELECT * FROM chat_room ORDER BY name").fetchall()
        return [{"room_id": r["room_id"], "name": r["name"],
                 "description": r["description"],
                 "creator": (r["creator_user"], r["creator_device"], r["creator_client"]),
                 "created_at": r["created_at"]} for r in rows]

    def _lookup_room(self, cur, name: str) -> str:
        row = cur.execute("SELECT room_id FROM chat_room WHERE name=?",
                          (_room_name(name),)).fetchone()
        if row is None:
            raise NotFound(f"room {name!r} does not exist")
        return row["room_id"]

    def join(self, name: str, stable: StableAddress) -> dict:
        who = _address(stable)
        with self.db.write_light() as cur:
            room_id = self._lookup_room(cur, name)
            cur.execute("INSERT INTO chat_subscription(room_id,user,device,client,joined_at) "
                        "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (room_id, *who, now_iso()))
        return {"room_id": room_id, "room": name, "subscribed": True}

    def leave(self, name: str, stable: StableAddress) -> dict:
        who = _address(stable)
        with self.db.write_light() as cur:
            room_id = self._lookup_room(cur, name)
            cur.execute("DELETE FROM chat_subscription WHERE room_id=? AND user=? "
                        "AND device=? AND client=?", (room_id, *who))
        return {"room_id": room_id, "room": name, "subscribed": False}

    def subscribed(self, name: str, stable: StableAddress) -> bool:
        who = _address(stable)
        with self.db.read() as cur:
            room_id = self._lookup_room(cur, name)
            return cur.execute("SELECT 1 FROM chat_subscription WHERE room_id=? AND user=? "
                               "AND device=? AND client=?", (room_id, *who)).fetchone() is not None

    def subscribers(self, name: str) -> list[StableAddress]:
        with self.db.read() as cur:
            room_id = self._lookup_room(cur, name)
            rows = cur.execute("SELECT user,device,client FROM chat_subscription "
                               "WHERE room_id=? ORDER BY user,device,client", (room_id,)).fetchall()
        return [(r["user"], r["device"], r["client"]) for r in rows]

    @staticmethod
    def _target_key(channel: str, target: StableAddress | str) -> str:
        return json.dumps([channel, *target] if channel == "dm" else [channel, target],
                          separators=(",", ":"))

    @staticmethod
    def _public(row) -> dict:
        return {"id": row["message_id"], "seq": row["seq"], "channel": row["channel"],
                "room_id": row["room_id"], "sender": (row["sender_user"],
                row["sender_device"], row["sender_client"]),
                "sender_session": row["sender_session"], "kind": row["message_kind"],
                "body": row["body"], "created_at": row["created_at"],
                "expires_at": row["created_at"] + MESSAGE_TTL}

    def _purge(self, cur, now: float) -> int:
        """Track discarded ranges even when the last message in a conversation is deleted."""
        old = cur.execute("SELECT target_key, MAX(seq) AS last_seq, COUNT(*) AS n, "
                          "SUM(body_bytes + ?) AS bytes FROM chat_message WHERE created_at<=? "
                          "GROUP BY target_key", (MESSAGE_OVERHEAD, now - MESSAGE_TTL)).fetchall()
        if not old:
            return 0
        for row in old:
            cur.execute("INSERT INTO chat_expiration_watermark VALUES(?,?) "
                        "ON CONFLICT(target_key) DO UPDATE SET "
                        "expired_through_seq=MAX(expired_through_seq, excluded.expired_through_seq)",
                        (row["target_key"], row["last_seq"]))
        cur.execute("DELETE FROM chat_message WHERE created_at<=?", (now - MESSAGE_TTL,))
        cur.execute("UPDATE chat_usage SET counted_bytes=counted_bytes-?, "
                    "message_count=message_count-? WHERE id=1",
                    (sum(r["bytes"] for r in old), sum(r["n"] for r in old)))
        return sum(r["n"] for r in old)

    def cleanup(self, *, now: Optional[float] = None) -> int:
        """Bound disk usage and presence, without expiring mailboxes or room subscriptions."""
        t = time.time() if now is None else float(now)
        with self.db.write_light() as cur:
            removed = self._purge(cur, t)
            cur.execute("DELETE FROM chat_session WHERE last_activity_at<=?",
                        (t - MESSAGE_TTL,))
        return removed

    def send(self, channel: str, target: StableAddress | str, sender: StableAddress,
             body: str, idempotency_key: str, *, now: Optional[float] = None,
             kind: str = "text", session_id: Optional[str] = None) -> dict:
        who = _address(sender)
        if channel not in ("dm", "room"):
            raise Invalid("channel must be dm or room")
        if kind not in ("text", "progress"):
            raise Invalid("message kind must be text or progress")
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise Invalid("body exceeds 256 KiB UTF-8")
        if not isinstance(idempotency_key, str) or not _RETRY.fullmatch(idempotency_key):
            raise Invalid("idempotency_key must be a 1–64 character ASCII key")
        if session_id is not None and not _SESSION.fullmatch(session_id):
            raise Invalid("invalid sender session_id")
        t = time.time() if now is None else float(now)
        with self.db.write_light() as cur:
            self._purge(cur, t)
            if channel == "dm":
                addressed = _address(target)
                room_id = None
            else:
                if not isinstance(target, str):
                    raise Invalid("room target must be its name")
                room_id = self._lookup_room(cur, target)
                addressed = None
            target_key = self._target_key(channel, addressed or room_id)
            duplicate = cur.execute(
                "SELECT * FROM chat_message WHERE sender_user=? AND sender_device=? "
                "AND sender_client=? AND target_key=? AND retry_key=?",
                (*who, target_key, idempotency_key)).fetchone()
            if duplicate:
                if duplicate["body"] != body or duplicate["message_kind"] != kind:
                    raise Conflict("idempotency_key already used for different content")
                return {**self._public(duplicate), "duplicate": True}
            byte_count = len(body.encode("utf-8"))
            cur.execute("INSERT INTO chat_usage VALUES(1,0,0) ON CONFLICT(id) DO NOTHING")
            usage = cur.execute("SELECT counted_bytes, message_count FROM chat_usage WHERE id=1").fetchone()
            if (usage["counted_bytes"] + byte_count + MESSAGE_OVERHEAD > self.max_bytes or
                    usage["message_count"] + 1 > self.max_messages):
                raise Invalid("project chat quota exceeded; no unexpired message was discarded")
            message_id = ulid()
            cur.execute("INSERT INTO chat_message(message_id,channel,room_id,target_user,"
                        "target_device,target_client,sender_user,sender_device,sender_client,"
                        "sender_session,target_key,body,body_bytes,message_kind,retry_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (message_id, channel, room_id, *(addressed or (None, None, None)),
                         *who, session_id, target_key, body, byte_count, kind, idempotency_key, t))
            seq = cur.lastrowid
            cur.execute("UPDATE chat_usage SET counted_bytes=counted_bytes+?, "
                        "message_count=message_count+1 WHERE id=1",
                        (byte_count + MESSAGE_OVERHEAD,))
            if session_id is not None:
                self._touch(cur, who, session_id, t)
        return {"id": message_id, "seq": seq, "channel": channel, "room_id": room_id,
                "sender": who, "sender_session": session_id, "kind": kind, "body": body,
                "created_at": t, "expires_at": t + MESSAGE_TTL, "duplicate": False}

    @staticmethod
    def _page(after_seq: int, limit: int) -> None:
        if not isinstance(after_seq, int) or after_seq < 0 or not isinstance(limit, int) \
                or not 1 <= limit <= MAX_PAGE:
            raise Invalid("after_seq must be nonnegative and limit must be 1–100")

    def _history(self, where: str, args: tuple, key: str, after_seq: int,
                 limit: int, now: float) -> dict:
        self._page(after_seq, limit)
        with self.db.read() as cur:
            rows = cur.execute(
                f"SELECT * FROM chat_message WHERE {where} AND seq>? AND created_at>? "
                "ORDER BY seq LIMIT ?", (*args, after_seq, now - MESSAGE_TTL, limit)).fetchall()
            mark = cur.execute("SELECT expired_through_seq FROM chat_expiration_watermark "
                               "WHERE target_key=?", (key,)).fetchone()
            # A cleanup pass may not have run yet. Expose the gap at read time, too.
            old = cur.execute("SELECT MAX(seq) AS last_seq FROM chat_message "
                              f"WHERE {where} AND seq>? AND created_at<=?",
                              (*args, after_seq, now - MESSAGE_TTL)).fetchone()
        expired = max(mark["expired_through_seq"] if mark else 0, old["last_seq"] or 0)
        return {"messages": [self._public(row) for row in rows],
                "next_seq": rows[-1]["seq"] if rows else after_seq,
                "gap": expired > after_seq, "expired_through_seq": expired,
                "oldest_available_id": rows[0]["message_id"] if rows else None}

    def inbox(self, stable: StableAddress, after_seq: int = 0, limit: int = MAX_PAGE,
              *, now: Optional[float] = None) -> dict:
        who = _address(stable)
        return self._history("channel='dm' AND target_user=? AND target_device=? "
                             "AND target_client=?", who, self._target_key("dm", who),
                             after_seq, limit, time.time() if now is None else float(now))

    def history(self, room: str, after_seq: int = 0, limit: int = MAX_PAGE,
                *, now: Optional[float] = None) -> dict:
        with self.db.read() as cur:
            room_id = self._lookup_room(cur, room)
        return self._history("channel='room' AND room_id=?", (room_id,),
                             self._target_key("room", room_id), after_seq, limit,
                             time.time() if now is None else float(now))

    def message(self, message_id: str, reader: StableAddress,
                *, now: Optional[float] = None) -> dict:
        who = _address(reader)
        t = time.time() if now is None else float(now)
        with self.db.read() as cur:
            row = cur.execute("SELECT * FROM chat_message WHERE message_id=? "
                              "AND created_at>?", (message_id, t - MESSAGE_TTL)).fetchone()
        if row is None or (row["channel"] == "dm" and who not in (
                (row["sender_user"], row["sender_device"], row["sender_client"]),
                (row["target_user"], row["target_device"], row["target_client"]))):
            raise NotFound("message not found")
        return self._public(row)

    def _cursor_key(self, cur, target: str, who: StableAddress) -> tuple[str, str, tuple]:
        if target == "dm":
            return "channel='dm' AND target_user=? AND target_device=? AND target_client=?", \
                self._target_key("dm", who), who
        room_id = self._lookup_room(cur, target)
        return "channel='room' AND room_id=?", self._target_key("room", room_id), (room_id,)

    def mark_read(self, stable: StableAddress, target: str, seq: int,
                  *, now: Optional[float] = None) -> dict:
        who = _address(stable)
        if not isinstance(seq, int) or seq <= 0:
            raise Invalid("read cursor must name a fetched message")
        t = time.time() if now is None else float(now)
        with self.db.write_light() as cur:
            clause, key, args = self._cursor_key(cur, target, who)
            row = cur.execute(f"SELECT 1 FROM chat_message WHERE {clause} "
                              "AND seq=? AND created_at>?", (*args, seq, t - MESSAGE_TTL)).fetchone()
            if row is None:
                raise Invalid("read cursor must name an accessible unexpired message")
            cur.execute("INSERT INTO chat_cursor VALUES(?,?,?,?,?,?) ON CONFLICT(user,device,client,target_key) "
                        "DO UPDATE SET seq=MAX(seq,excluded.seq),updated_at=excluded.updated_at",
                        (*who, key, seq, t))
        return {"up_to_seq": seq, "target": target}

    def read_cursor(self, stable: StableAddress, target: str) -> int:
        who = _address(stable)
        with self.db.read() as cur:
            _, key, _ = self._cursor_key(cur, target, who)
            row = cur.execute("SELECT seq FROM chat_cursor WHERE user=? AND device=? "
                              "AND client=? AND target_key=?", (*who, key)).fetchone()
        return row["seq"] if row else 0

    @staticmethod
    def _touch(cur, who: StableAddress, session: str, now: float) -> None:
        cur.execute("DELETE FROM chat_session WHERE last_activity_at<=?", (now - MESSAGE_TTL,))
        cur.execute("INSERT INTO chat_session VALUES(?,?,?,?,?) "
                    "ON CONFLICT(user,device,client,session_id) DO UPDATE SET "
                    "last_activity_at=excluded.last_activity_at", (*who, session, now))

    def touch(self, stable: StableAddress, session: str,
              *, now: Optional[float] = None) -> None:
        who = _address(stable)
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            raise Invalid("invalid session_id")
        with self.db.write_light() as cur:
            self._touch(cur, who, session, time.time() if now is None else float(now))

    def agents(self, *, now: Optional[float] = None) -> list[dict]:
        t = time.time() if now is None else float(now)
        with self.db.read() as cur:
            rows = cur.execute("SELECT * FROM chat_session WHERE last_activity_at>? "
                               "ORDER BY last_activity_at DESC", (t - MESSAGE_TTL,)).fetchall()
        return [{"address": (r["user"], r["device"], r["client"]),
                 "session_id": r["session_id"], "last_activity_at": r["last_activity_at"]}
                for r in rows]
