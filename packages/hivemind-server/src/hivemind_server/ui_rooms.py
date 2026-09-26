"""Bounded room and team pages for the automatically refreshed console."""
from __future__ import annotations

import json

from . import teams
from .chat import ChatStore, _address, _room_name
from .db import Database, Invalid


def member_page(db: Database, name: str, *, after: str | None = None,
                limit: int = 25) -> dict:
    if not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    cursor = None
    if after is not None:
        if len(after) > 256:
            raise Invalid("invalid room member cursor")
        try:
            cursor = _address(json.loads(after))
        except (ValueError, TypeError) as exc:
            raise Invalid("invalid room member cursor") from exc
    with db.read() as cur:
        room_id = ChatStore(db)._lookup_room(cur, name)
        count = cur.execute("SELECT COUNT(*) AS n FROM chat_subscription WHERE room_id=?",
                            (room_id,)).fetchone()["n"]
        rows = cur.execute("SELECT user,device,client FROM chat_subscription "
                           "WHERE room_id=? AND (user,device,client)>(?,?,?) "
                           "ORDER BY user,device,client LIMIT ?",
                           (room_id, *(cursor or ("", "", "")), limit + 1)).fetchall()
    more = len(rows) > limit
    addresses = [(row["user"], row["device"], row["client"])
                 for row in rows[:limit]]
    return {"members": addresses, "member_count": count,
            "members_older_cursor": json.dumps(addresses[-1], separators=(",", ":"))
                                    if more else None}


def room_page(db: Database, *, after: str | None = None, limit: int = 25) -> dict:
    if not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    cursor = _room_name(after) if after is not None else ""
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM chat_room WHERE name>? ORDER BY name LIMIT ?",
                           (cursor, limit + 1)).fetchall()
    more = len(rows) > limit
    rooms = []
    for row in rows[:limit]:
        name = row["name"]
        rooms.append({"room_id": row["room_id"], "name": name,
                      "description": row["description"],
                      "creator": (row["creator_user"], row["creator_device"],
                                  row["creator_client"]), "created_at": row["created_at"],
                      **teams.manager(db, name),
                      **member_page(db, name, limit=limit)})
    return {"rooms": rooms, "older_cursor": rooms[-1]["name"] if more else None}
