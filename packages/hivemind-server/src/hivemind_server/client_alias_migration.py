"""One-time, collision-aware migration of historical Claude Code addresses."""
from __future__ import annotations

import json
import sqlite3

from .db import Invalid

MARKER = "client_alias_migrated_v1"
OLD = "claude-code"
NEW = "claude"


def _key(value: str) -> str:
    try:
        parts = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(parts, list) and len(parts) == 4 and parts[0] == "dm" and parts[3] == OLD:
        return json.dumps([*parts[:3], NEW], separators=(",", ":"))
    return value


def _preflight(cur: sqlite3.Cursor) -> None:
    for table, fields in (("agent_config", ("max_parallel_tasks", "auto_claim_enabled")),
                          ("agent_capability", ("tags_json", "approved_tags_json"))):
        rows = cur.execute(f"SELECT old.*,new.* FROM {table} old JOIN {table} new "
                           "ON old.user=new.user AND old.device=new.device "
                           "WHERE old.client=? AND new.client=? AND "
                           "old.human_managed=1 AND new.human_managed=1", (OLD, NEW)).fetchall()
        # Select the two sides separately: duplicate column names in sqlite3.Row otherwise
        # silently resolve to the first side, hiding conflicting human decisions.
        for row in rows:
            who = (row["user"], row["device"])
            old = cur.execute(f"SELECT * FROM {table} WHERE user=? AND device=? AND client=?",
                              (*who, OLD)).fetchone()
            new = cur.execute(f"SELECT * FROM {table} WHERE user=? AND device=? AND client=?",
                              (*who, NEW)).fetchone()
            if any(old[field] != new[field] for field in fields):
                raise Invalid(f"claude-code migration conflict in {table} for {who[0]}/{who[1]}")

    # Updating a legacy DM's target key can merge two idempotency namespaces. Neither
    # original message nor its retry may be discarded or redirected to the other id.
    seen = {}
    for row in cur.execute("SELECT message_id,sender_user,sender_device,sender_client,"
                           "target_key,retry_key FROM chat_message").fetchall():
        key = (row["sender_user"], row["sender_device"],
               NEW if row["sender_client"] == OLD else row["sender_client"],
               _key(row["target_key"]), row["retry_key"])
        prior = seen.setdefault(key, row["message_id"])
        if prior != row["message_id"]:
            raise Invalid("claude-code migration conflict in chat_message retry keys: "
                          f"{prior}/{row['message_id']}")


def preflight(con: sqlite3.Connection) -> None:
    """Reject known identity collisions before other startup data migrations write rows."""
    if con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone() is None:
        _preflight(con.cursor())


def _merge_simple(cur: sqlite3.Cursor, table: str, client_column: str) -> None:
    cur.execute(f"UPDATE {table} SET {client_column}=? WHERE {client_column}=?", (NEW, OLD))


def _subscriptions(cur: sqlite3.Cursor) -> None:
    rows = cur.execute("SELECT * FROM chat_subscription WHERE client=?", (OLD,)).fetchall()
    for row in rows:
        cur.execute("INSERT INTO chat_subscription(room_id,user,device,client,joined_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(room_id,user,device,client) DO UPDATE "
                    "SET joined_at=MIN(chat_subscription.joined_at,excluded.joined_at)",
                    (row["room_id"], row["user"], row["device"], NEW, row["joined_at"]))
    cur.execute("DELETE FROM chat_subscription WHERE client=?", (OLD,))


def _sessions(cur: sqlite3.Cursor) -> None:
    rows = cur.execute("SELECT * FROM chat_session WHERE client=?", (OLD,)).fetchall()
    for row in rows:
        sid = row["session_id"]
        collision = cur.execute("SELECT 1 FROM chat_session WHERE user=? AND device=? "
                                "AND client=? AND session_id=?",
                                (row["user"], row["device"], NEW, sid)).fetchone()
        if collision:
            import hashlib
            suffix = hashlib.sha256(f"{row['user']}/{row['device']}/{sid}".encode()).hexdigest()[:12]
            sid = f"legacy-{suffix}-{sid}"[:64]
            if cur.execute("SELECT 1 FROM chat_session WHERE user=? AND device=? AND "
                           "client=? AND session_id=?", (row["user"], row["device"], NEW,
                                                      sid)).fetchone():
                raise Invalid("claude-code migration conflict in chat_session session_id")
            cur.execute("UPDATE chat_message SET sender_session=? WHERE sender_user=? "
                        "AND sender_device=? AND sender_client=? AND sender_session=?",
                        (sid, row["user"], row["device"], OLD, row["session_id"]))
        cur.execute("UPDATE chat_session SET client=?,session_id=? WHERE user=? AND "
                    "device=? AND client=? AND session_id=?", (NEW, sid, row["user"],
                        row["device"], OLD, row["session_id"]))


def _cursors(cur: sqlite3.Cursor) -> None:
    for row in cur.execute("SELECT rowid,* FROM chat_cursor").fetchall():
        target = _key(row["target_key"])
        client = NEW if row["client"] == OLD else row["client"]
        if (target, client) == (row["target_key"], row["client"]):
            continue
        cur.execute("INSERT INTO chat_cursor(user,device,client,target_key,seq,updated_at,"
                    "message_id) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user,device,client,target_key) "
                    "DO UPDATE SET seq=MAX(chat_cursor.seq,excluded.seq),"
                    "updated_at=MAX(chat_cursor.updated_at,excluded.updated_at),"
                    "message_id=CASE WHEN excluded.seq>chat_cursor.seq "
                    "THEN excluded.message_id ELSE chat_cursor.message_id END",
                    (row["user"], row["device"], client, target, row["seq"],
                     row["updated_at"], row["message_id"]))
        cur.execute("DELETE FROM chat_cursor WHERE rowid=?", (row["rowid"],))
    for row in cur.execute("SELECT target_key,expired_through_seq FROM "
                           "chat_expiration_watermark").fetchall():
        target = _key(row["target_key"])
        if target == row["target_key"]:
            continue
        cur.execute("INSERT INTO chat_expiration_watermark VALUES(?,?) ON CONFLICT(target_key) "
                    "DO UPDATE SET expired_through_seq=MAX(expired_through_seq,"
                    "excluded.expired_through_seq)", (target, row["expired_through_seq"]))
        cur.execute("DELETE FROM chat_expiration_watermark WHERE target_key=?",
                    (row["target_key"],))


def _agent_rows(cur: sqlite3.Cursor, table: str) -> None:
    rows = cur.execute(f"SELECT * FROM {table} WHERE client=?", (OLD,)).fetchall()
    for old in rows:
        who = (old["user"], old["device"])
        current = cur.execute(f"SELECT * FROM {table} WHERE user=? AND device=? "
                              "AND client=?", (*who, NEW)).fetchone()
        if current is None:
            cur.execute(f"UPDATE {table} SET client=? WHERE user=? AND device=? AND client=?",
                        (NEW, *who, OLD))
            continue
        if table == "agent_config":
            winner = old if old["human_managed"] and not current["human_managed"] else \
                current if current["human_managed"] and not old["human_managed"] else \
                old if old["updated_at"] > current["updated_at"] else current
            cur.execute("UPDATE agent_config SET max_parallel_tasks=?,auto_claim_enabled=?,"
                        "updated_at=?,human_managed=? WHERE user=? AND device=? AND client=?",
                        (winner["max_parallel_tasks"], winner["auto_claim_enabled"],
                         max(old["updated_at"], current["updated_at"]),
                         max(old["human_managed"], current["human_managed"]), *who, NEW))
        else:
            tags = sorted(set(json.loads(old["tags_json"])) |
                          set(json.loads(current["tags_json"])))
            approved = sorted(set(json.loads(old["approved_tags_json"])) |
                              set(json.loads(current["approved_tags_json"])))
            cur.execute("UPDATE agent_capability SET tags_json=?,approved_tags_json=?,"
                        "updated_at=?,human_managed=? WHERE user=? AND device=? AND client=?",
                        (json.dumps(tags), json.dumps(approved),
                         max(old["updated_at"], current["updated_at"]),
                         max(old["human_managed"], current["human_managed"]), *who, NEW))
        cur.execute(f"DELETE FROM {table} WHERE user=? AND device=? AND client=?",
                    (*who, OLD))


def _activity(cur: sqlite3.Cursor) -> None:
    for row in cur.execute("SELECT * FROM graph_task_activity WHERE client=?", (OLD,)).fetchall():
        cur.execute("INSERT INTO graph_task_activity VALUES(?,?,?,?,?,?,?) ON CONFLICT(node_id,"
                    "user,device,client) DO UPDATE SET last_beat_at=MAX(last_beat_at,"
                    "excluded.last_beat_at),interval_seconds=CASE WHEN excluded.last_beat_at>="
                    "last_beat_at THEN excluded.interval_seconds ELSE interval_seconds END,"
                    "expires_after_seconds=CASE WHEN excluded.last_beat_at>=last_beat_at "
                    "THEN excluded.expires_after_seconds ELSE expires_after_seconds END",
                    (row["node_id"], row["user"], row["device"], NEW,
                     row["last_beat_at"], row["interval_seconds"], row["expires_after_seconds"]))
    cur.execute("DELETE FROM graph_task_activity WHERE client=?", (OLD,))


def migrate(con: sqlite3.Connection) -> None:
    """Merge legacy stable addresses or leave all data rows unchanged on conflict."""
    if con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        cur = con.cursor()
        if cur.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            con.execute("COMMIT")
            return
        _preflight(cur)
        _subscriptions(cur)
        _sessions(cur)
        _cursors(cur)
        _agent_rows(cur, "agent_capability")
        _agent_rows(cur, "agent_config")
        _activity(cur)
        for table, column in (
            ("chat_room", "creator_client"), ("room_manager", "client"),
            ("room_team_event", "actor_client"), ("room_team_event", "target_client"),
            ("room_team_event", "prior_client"),
            ("agent_instruction", "recipient_client"),
            ("agent_instruction_delivery", "recipient_client"),
            # Archived instruction digests include the original recipient address and the
            # body is no longer available to rehash. Preserve its raw recipient for retries.
            ("graph_task_claim", "holder_client"),
            ("graph_task_assignment", "assignee_client"),
            ("graph_task_assignment_event", "assignee_client"),
            ("graph_task_assignment_event", "previous_client"),
            ("graph_task_event", "client"),
        ):
            _merge_simple(cur, table, column)
        for row in cur.execute("SELECT message_id,target_key FROM chat_message WHERE "
                               "channel='dm' AND target_client=?", (OLD,)).fetchall():
            cur.execute("UPDATE chat_message SET target_client=?,target_key=? WHERE message_id=?",
                        (NEW, _key(row["target_key"]), row["message_id"]))
        cur.execute("INSERT INTO meta(key,value) VALUES(?,?)", (MARKER, "1"))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
