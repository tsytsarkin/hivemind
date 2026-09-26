"""Project-local durable chat metadata, separate from the legacy ephemeral bus."""
from __future__ import annotations

import re
import sqlite3
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
    def __init__(self, db: Database):
        self.db = db

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
