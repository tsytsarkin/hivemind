"""Bounded task pages and project-wide totals using one effective-state definition."""
from __future__ import annotations

import json
import time

from . import capabilities, graph
from .chat import ChatStore, StableAddress
from .db import Database, Invalid

STATES = ("available", "assigned_waiting", "in_progress", "complete")

_STATES_SQL = """
WITH task_states AS (
  SELECT t.node_id,t.room_id,r.name AS room,t.status_mode,
         t.required_capabilities_json,
         CASE WHEN t.status='complete' THEN 'complete'
              WHEN c.token_digest IS NOT NULL AND
                   c.last_beat_at+c.expires_after_seconds>? THEN 'in_progress'
              WHEN t.status='in_progress' AND c.node_id IS NOT NULL THEN 'available'
              WHEN a.assignee_user IS NOT NULL THEN 'assigned_waiting'
              ELSE 'available' END AS state
  FROM graph_task t
  LEFT JOIN graph_task_claim c ON c.node_id=t.node_id
  LEFT JOIN graph_task_assignment a ON a.node_id=t.node_id
  LEFT JOIN chat_room r ON r.room_id=t.room_id
)
"""


def page(db: Database, *, status: str = "all", before_id: str | None = None,
         limit: int = 100, eligible_for: StableAddress | None = None,
         with_counts: bool = False, room: str | None = None) -> dict:
    if status not in ("all", *STATES):
        raise Invalid("unknown task status")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if before_id is not None and (not isinstance(before_id, str) or len(before_id) > 64):
        raise Invalid("invalid before_id cursor")
    t = time.time()
    with db.read() as cur:
        room_id = ChatStore(db)._lookup_room(cur, room) if room is not None else None
        room_sql = " WHERE room_id=?" if room_id is not None else ""
        counts = {name: 0 for name in STATES}
        if with_counts:
            for row in cur.execute(_STATES_SQL + "SELECT state,COUNT(*) AS n FROM task_states "
                                   + room_sql + " GROUP BY state",
                                   (t, room_id) if room_id is not None else (t,)):
                counts[row["state"]] = row["n"]
        eligible = ""
        values = [t, status, status, before_id or "Z"]
        room_filter = " AND room_id=?" if room_id is not None else ""
        if room_id is not None:
            values.append(room_id)
        if eligible_for is not None:
            tags = capabilities.get_in_transaction(cur, eligible_for)
            eligible = (" AND NOT EXISTS (SELECT 1 FROM json_each(required_capabilities_json) "
                        "AS required WHERE required.value NOT IN "
                        "(SELECT value FROM json_each(?)))")
            values.append(json.dumps(tags))
        rows = cur.execute(_STATES_SQL + "SELECT * FROM task_states WHERE (?='all' OR state=?) "
                           "AND node_id<?" + room_filter + eligible + " ORDER BY node_id DESC LIMIT ?",
                           (*values, limit + 1)).fetchall()
    has_older = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        current = graph.get_node(db, node_id=row["node_id"])["current"] or {}
        props = current.get("props") or {}
        items.append({"node_id": row["node_id"], "room_id": row["room_id"],
                      "room": row["room"], "state": row["state"],
                      "status_mode": row["status_mode"],
                      "required_capabilities": json.loads(row["required_capabilities_json"]),
                      "title": props.get("title"), "summary": props.get("summary")})
    return {"tasks": items, "older_cursor": rows[-1]["node_id"] if has_older else None,
            **({"counts": counts} if with_counts else {})}
