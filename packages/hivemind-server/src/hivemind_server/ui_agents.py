"""Bounded, project-wide roster pages including agents with durable advertisements."""
from __future__ import annotations

import json
import time

from . import capabilities
from .chat import MESSAGE_TTL, _address
from .db import Database, Invalid


def page(db: Database, hub, *, after: str | None = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    cursor = None
    if after is not None:
        if len(after) > 256:
            raise Invalid("invalid agent cursor")
        try:
            cursor = _address(json.loads(after))
        except (ValueError, TypeError) as exc:
            raise Invalid("invalid agent cursor") from exc
    # Bound each indexed source before merging: a LIMIT outside a UNION would scan,
    # deduplicate and sort every self-advertisement on every console refresh.
    with db.read() as cur:
        candidates = set()
        for table in ("chat_session", "chat_subscription", "agent_capability"):
            active = "AND last_activity_at>?" if table == "chat_session" else ""
            params = (* (cursor or ("", "", "")),
                      *((time.time() - MESSAGE_TTL,) if active else ()), limit + 1)
            rows = cur.execute(
                f"SELECT user,device,client FROM {table} "
                f"WHERE (user,device,client)>(?,?,?) {active} "
                "GROUP BY user,device,client ORDER BY user,device,client LIMIT ?",
                params).fetchall()
            candidates.update((r["user"], r["device"], r["client"]) for r in rows)
        sorted_addresses = sorted(candidates)
        has_more = len(sorted_addresses) > limit
        addresses = sorted_addresses[:limit]
        agents = []
        for address in addresses:
            sessions = cur.execute(
                "SELECT session_id,last_activity_at FROM chat_session WHERE user=? AND device=? "
                "AND client=? AND last_activity_at>? ORDER BY last_activity_at DESC LIMIT 10",
                (*address, time.time() - MESSAGE_TTL)).fetchall()
            sessions = [{"session_id": s["session_id"],
                         "last_activity_at": s["last_activity_at"],
                         "online": hub.online(address, s["session_id"])} for s in sessions]
            agents.append({"address": address, "last_activity_at":
                           sessions[0]["last_activity_at"] if sessions else None,
                           "online": hub.online(address), "sessions": sessions})
    return {"agents": agents,
            "capabilities": capabilities.list_addresses(db, set(addresses)),
            "older_cursor": json.dumps(addresses[-1], separators=(",", ":"))
                            if has_more else None}
