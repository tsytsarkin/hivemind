"""Legacy Claude Code collaboration state survives canonical client migration."""

import json
import time

import pytest

from hivemind_server import assignments, graph_tasks, instructions, teams
from hivemind_server.chat import ChatStore
from hivemind_server.db import Database, Invalid


OLD = ("nik", "mac", "claude-code")
NEW = ("nik", "mac", "claude")
OWNER = ("nik", "mac", "codex")
MARKER = "client_alias_migrated_v1"


def test_restart_merges_legacy_mail_read_marker_room_claim_and_instruction(tmp_path):
    db = Database(tmp_path / "project.db")
    store = ChatStore(db)
    store.create_room("review", "Code review", OWNER)
    teams.add_member(db, "review", NEW, OWNER)
    message = store.send("dm", NEW, OWNER, "Please review", "legacy-mail", now=time.time())
    store.mark_read(NEW, "dm", message["seq"])
    store.touch(NEW, "legacy-session")
    nid = graph_tasks.offer(db, "setup", "review", "Audit", "Check code")["node_id"]
    teams.promote(db, "review", NEW, OWNER, expected_revision=0)
    assignments.assign(db, "setup", nid, NEW)
    claimed = graph_tasks.claim(db, "setup", nid, NEW)
    queued = instructions.enqueue(db, "setup", NEW, "Fix a bug", "legacy-instruction")
    legacy_key = json.dumps(["dm", *OLD], separators=(",", ":"))
    with db.write_light() as cur:
        cur.execute("UPDATE chat_subscription SET client='claude-code' WHERE user=? AND "
                    "device=? AND client='claude'", NEW[:2])
        cur.execute("UPDATE chat_message SET target_client='claude-code',target_key=? "
                    "WHERE message_id=?", (legacy_key, message["id"]))
        cur.execute("UPDATE chat_cursor SET client='claude-code',target_key=? WHERE user=? "
                    "AND device=? AND client='claude'", (legacy_key, *NEW[:2]))
        cur.execute("UPDATE chat_session SET client='claude-code' WHERE user=? AND "
                    "device=? AND client='claude'", NEW[:2])
        cur.execute("UPDATE room_manager SET client='claude-code' WHERE user=? AND "
                    "device=? AND client='claude'", NEW[:2])
        cur.execute("UPDATE graph_task_claim SET holder_client='claude-code' WHERE node_id=?",
                    (nid,))
        cur.execute("UPDATE graph_task_assignment SET assignee_client='claude-code' "
                    "WHERE node_id=?", (nid,))
        cur.execute("UPDATE agent_instruction SET recipient_client='claude-code' WHERE id=?",
                    (queued["id"],))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))

    restarted = Database(db.path)
    inbox = ChatStore(restarted).inbox(NEW)
    assert [entry["id"] for entry in inbox["messages"]] == [message["id"]]
    assert ChatStore(restarted).read_marker(NEW, "dm")["last_read_message_id"] == message["id"]
    assert ChatStore(restarted).subscribed("review", NEW)
    assert ChatStore(restarted).agents()[0]["address"] == NEW
    assert teams.manager(restarted, "review")["manager"] == NEW
    graph_tasks.heartbeat(restarted, nid, claimed["claim_token"], NEW)
    assert graph_tasks.read(restarted, nid)["claim"]["holder"] == NEW
    assert instructions.inbox(restarted, NEW)["instructions"][0]["id"] == queued["id"]
    assert assignments.view(restarted, nid)["assignee"] == NEW
    again = Database(db.path)
    assert ChatStore(again).inbox(NEW)["messages"][0]["id"] == message["id"]


def test_coexisting_alias_merges_membership_cursor_and_distinct_sessions(tmp_path):
    db = Database(tmp_path / "project.db")
    store = ChatStore(db)
    room = store.create_room("review", "Code review", OWNER)
    store.join("review", NEW)
    first = store.send("dm", NEW, OWNER, "First", "one")
    second = store.send("dm", NEW, OWNER, "Second", "two")
    store.mark_read(NEW, "dm", first["seq"])
    store.touch(NEW, "same")
    old_key = json.dumps(["dm", *OLD], separators=(",", ":"))
    with db.write_light() as cur:
        cur.execute("INSERT INTO chat_subscription SELECT room_id,user,device,?,joined_at "
                    "FROM chat_subscription WHERE room_id=? AND client=?",
                    (OLD[2], room["room_id"], NEW[2]))
        cur.execute("INSERT INTO chat_cursor(user,device,client,target_key,seq,updated_at,"
                    "message_id) VALUES(?,?,?,?,?,?,?)",
                    (*OLD, old_key, second["seq"], time.time(), second["id"]))
        for sid in ("same", "separate"):
            cur.execute("INSERT INTO chat_session(user,device,client,session_id,"
                        "last_activity_at,model_name) VALUES(?,?,?,?,?,?)",
                        (*OLD, sid, time.time(), "opus-4"))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))

    restarted = Database(db.path)
    assert ChatStore(restarted).read_marker(NEW, "dm") == {
        "last_read_seq": second["seq"], "last_read_message_id": second["id"]}
    with restarted.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM chat_subscription WHERE room_id=? AND "
                           "user=? AND device=? AND client=?", (room["room_id"], *NEW)).fetchone()[0] == 1
        sessions = cur.execute("SELECT session_id,model_name FROM chat_session WHERE "
                               "user=? AND device=? AND client=? ORDER BY session_id", NEW).fetchall()
        assert len(sessions) == 3
        assert {sid["model_name"] for sid in sessions} == {None, "opus-4"}
        assert cur.execute("SELECT COUNT(*) FROM chat_session WHERE client=?",
                           (OLD[2],)).fetchone()[0] == 0
    assert ChatStore(Database(db.path)).read_marker(NEW, "dm")["last_read_seq"] == second["seq"]


def test_conflicting_human_managed_alias_config_aborts_without_losing_rows(tmp_path):
    from hivemind_server import agent_config, capabilities

    db = Database(tmp_path / "project.db")
    capabilities.define(db, "review", "Review code")
    agent_config.update(db, NEW, max_parallel_tasks=2, auto_claim_enabled=True,
                        managed_by_ui=True)
    with db.write_light() as cur:
        cur.execute("UPDATE project_capability SET approved=0 WHERE name='review'")
        cur.execute("INSERT INTO agent_config(user,device,client,max_parallel_tasks,"
                    "auto_claim_enabled,updated_at,human_managed) VALUES(?,?,?,?,?,?,?)",
                    (*OLD, 8, 1, time.time(), 1))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))
        cur.execute("DELETE FROM meta WHERE key='capability_approval_migrated_v1'")
    with pytest.raises(Invalid, match="conflict.*agent_config"):
        Database(db.path)
    with db.read() as cur:
        rows = cur.execute("SELECT client,max_parallel_tasks FROM agent_config "
                           "ORDER BY client").fetchall()
    assert [(r["client"], r["max_parallel_tasks"]) for r in rows] == \
        [("claude", 2), ("claude-code", 8)]
    assert capabilities.catalog(db)["capabilities"][0]["approved"] is False


def test_alias_retry_key_collision_refuses_migration_without_losing_messages(tmp_path):
    db = Database(tmp_path / "project.db")
    store = ChatStore(db)
    first = store.send("dm", OWNER, NEW, "First request", "shared-key")
    other = store.send("dm", OWNER, ("nik", "mac", "muse"),
                       "Different request", "shared-key")
    with db.write_light() as cur:
        cur.execute("UPDATE chat_message SET sender_client=? WHERE message_id=?",
                    (OLD[2], other["id"]))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))
    with pytest.raises(Invalid, match="chat_message retry keys"):
        Database(db.path)
    with db.read() as cur:
        assert cur.execute("SELECT sender_client FROM chat_message WHERE message_id=?",
                           (other["id"],)).fetchone()[0] == OLD[2]
        assert cur.execute("SELECT COUNT(*) FROM chat_message WHERE message_id IN (?,?)",
                           (first["id"], other["id"])).fetchone()[0] == 2


def test_historical_claude_sender_keeps_message_access_and_idempotent_retry(tmp_path):
    db = Database(tmp_path / "project.db")
    store = ChatStore(db)
    sent = store.send("dm", OWNER, NEW, "Review complete", "legacy-retry",
                      session_id="old", now=time.time())
    with db.write_light() as cur:
        cur.execute("UPDATE chat_message SET sender_client=? WHERE message_id=?",
                    (OLD[2], sent["id"]))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))

    restarted = ChatStore(Database(db.path))
    assert restarted.message(sent["id"], NEW)["sender"] == NEW
    retried = restarted.send("dm", OWNER, NEW, "Review complete", "legacy-retry",
                             session_id="old")
    assert retried["duplicate"] is True and retried["id"] == sent["id"]


def test_archived_legacy_instruction_retries_to_original_id_after_migration(tmp_path):
    db = Database(tmp_path / "project.db")
    original = instructions.enqueue(db, "setup", NEW, "Fix the parser", "archive-retry")
    instructions.transition(db, NEW, original["id"], "queued", "acknowledged")
    instructions.transition(db, NEW, original["id"], "acknowledged", "completed")
    with db.write("test-archive") as tx:
        instructions._archive_terminal(tx, time.time() + 31 * 86400)
    with db.write_light() as cur:
        cur.execute("UPDATE agent_instruction_archive SET recipient_client=?,request_digest=? "
                    "WHERE id=?", (OLD[2], instructions._request_digest(
                        "Fix the parser", None, False, OLD, None), original["id"]))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))

    restarted = Database(db.path)
    retried = instructions.enqueue(restarted, "setup", NEW, "Fix the parser", "archive-retry")
    assert retried["archived"] is True and retried["id"] == original["id"]


def test_legacy_progress_still_counts_for_migrated_claim(tmp_path):
    db = Database(tmp_path / "project.db")
    store = ChatStore(db)
    store.create_room("review", "Code review", OWNER)
    nid = graph_tasks.offer(db, "setup", "review", "Audit", "Check code")["node_id"]
    graph_tasks.claim(db, "setup", nid, NEW)
    progress = store.send("room", "review", NEW, "Reviewed the main logic",
                          "progress-old", kind="progress", task_node_id=nid,
                          summary="Reviewing the main logic")
    with db.write_light() as cur:
        cur.execute("UPDATE chat_message SET sender_client='claude-code' WHERE message_id=?",
                    (progress["id"],))
        cur.execute("UPDATE graph_task_claim SET holder_client='claude-code' WHERE node_id=?",
                    (nid,))
        cur.execute("DELETE FROM meta WHERE key=?", (MARKER,))

    restarted = Database(db.path)
    assert graph_tasks.read(restarted, nid)["claim"]["last_progress_at"] == progress["created_at"]
