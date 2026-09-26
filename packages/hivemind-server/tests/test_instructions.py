"""Durable project-scoped human-to-agent work instructions."""

import time

import httpx
import pytest

from conftest import Lifespan, _call, _post
from hivemind_server import instructions, teams
from hivemind_server.chat import ChatStore
from hivemind_server.db import Conflict, Database, Invalid
from hivemind_server.identity import IdentityStore


OWNER = ("nik", "mac", "codex")
PEER = ("ana", "laptop", "claude")


def test_queue_persists_and_is_project_local(db, tmp_path):
    item = instructions.enqueue(db, "nik", PEER, "Review the patch", "once")
    assert instructions.inbox(db, PEER)["instructions"][0]["id"] == item["id"]
    assert instructions.inbox(Database(tmp_path / "other.db"), PEER)["instructions"] == []
    assert instructions.inbox(Database(db.path), PEER)["instructions"][0]["id"] == item["id"]
    assert instructions.inbox(db, OWNER)["instructions"] == []


def test_retries_are_idempotent_but_changed_request_conflicts(db):
    first = instructions.enqueue(db, "nik", PEER, "Audit", "retry-1")
    assert instructions.enqueue(db, "nik", PEER, "Audit", "retry-1")["id"] == first["id"]
    with pytest.raises(Conflict):
        instructions.enqueue(db, "nik", PEER, "Changed", "retry-1")
    assert len(instructions.inbox(db, PEER)["instructions"]) == 1


def test_recipient_transitions_fenced_and_terminal_stays_readable(db):
    item = instructions.enqueue(db, "nik", PEER, "Audit", "retry-1")
    with pytest.raises(Conflict):
        instructions.transition(db, OWNER, item["id"], "queued", "acknowledged")
    ack = instructions.transition(db, PEER, item["id"], "queued", "acknowledged")
    assert ack["state"] == "acknowledged"
    with pytest.raises(Conflict):
        instructions.transition(db, PEER, item["id"], "queued", "acknowledged")
    done = instructions.transition(db, PEER, item["id"], "acknowledged", "completed",
                                   result="Reviewed")
    assert done["state"] == "completed" and done["result"] == "Reviewed"
    with pytest.raises(Invalid):
        instructions.transition(db, PEER, item["id"], "completed", "in_progress")
    assert instructions.inbox(db, PEER)["instructions"][0]["state"] == "completed"


def test_only_queued_manager_instructions_follow_atomic_handoff(db):
    ChatStore(db).create_room("patches", "Patch reviews", OWNER)
    teams.add_member(db, "patches", OWNER, OWNER)
    teams.add_member(db, "patches", PEER, OWNER)
    teams.promote(db, "patches", OWNER, OWNER, expected_revision=0)
    queued = instructions.enqueue(db, "nik", OWNER, "Queue a review", "q1",
                                  room="patches", to_manager=True)
    started = instructions.enqueue(db, "nik", OWNER, "Check the branch", "q2",
                                   room="patches", to_manager=True)
    direct = instructions.enqueue(db, "nik", OWNER, "Direct message", "q3", room="patches")
    instructions.transition(db, OWNER, started["id"], "queued", "acknowledged")
    teams.promote(db, "patches", PEER, PEER, expected_revision=1)
    assert [i["id"] for i in instructions.inbox(db, PEER)["instructions"]] == [queued["id"]]
    assert set(i["id"] for i in instructions.inbox(db, OWNER)["instructions"]) == {
        started["id"], direct["id"]}


def test_handoff_reaches_new_manager_even_if_old_item_precedes_their_cursor(db, monkeypatch):
    from itertools import count
    ids = count(1)
    monkeypatch.setattr(instructions, "ulid", lambda: f"{next(ids):026d}")
    ChatStore(db).create_room("patches", "Patch reviews", OWNER)
    teams.add_member(db, "patches", OWNER, OWNER)
    teams.add_member(db, "patches", PEER, OWNER)
    teams.promote(db, "patches", OWNER, OWNER, expected_revision=0)
    queued = instructions.enqueue(db, "nik", OWNER, "Older manager work", "old",
                                  room="patches", to_manager=True)
    newer = instructions.enqueue(db, "nik", PEER, "Newer direct work", "new")
    page = instructions.inbox(db, PEER)
    assert [item["id"] for item in page["instructions"]] == [newer["id"]]
    teams.promote(db, "patches", PEER, PEER, expected_revision=1)
    caught_up = instructions.inbox(db, PEER, after_id=page["next_cursor"])
    assert [item["id"] for item in caught_up["instructions"]] == [queued["id"]]


def test_removed_manager_cannot_start_queued_manager_directed_work(db):
    ChatStore(db).create_room("patches", "Patch reviews", OWNER)
    teams.add_member(db, "patches", OWNER, OWNER)
    teams.add_member(db, "patches", PEER, OWNER)
    teams.promote(db, "patches", OWNER, OWNER, expected_revision=0)
    item = instructions.enqueue(db, "nik", OWNER, "Queue the review", "once",
                                room="patches", to_manager=True)
    teams.remove_member(db, "patches", OWNER, PEER)
    with pytest.raises(Conflict, match="manager"):
        instructions.transition(db, OWNER, item["id"], "queued", "acknowledged")
    teams.promote(db, "patches", PEER, PEER, expected_revision=2)
    assert instructions.inbox(db, PEER)["instructions"][0]["id"] == item["id"]


def test_queue_rejects_invalid_bodies_and_pending_quota(db, monkeypatch):
    with pytest.raises(Invalid):
        instructions.enqueue(db, "nik", PEER, " ", "bad")
    monkeypatch.setattr(instructions, "MAX_PENDING", 1)
    instructions.enqueue(db, "nik", PEER, "Audit", "one")
    with pytest.raises(Conflict):
        instructions.enqueue(db, "nik", OWNER, "Audit", "two")


def test_retry_requires_failed_or_stalled_work_and_cancel_is_queued_only(db):
    first = instructions.enqueue(db, "nik", PEER, "Audit", "one")
    with pytest.raises(Conflict):
        instructions.enqueue(db, "nik", PEER, "Retry", "two", retry_of=first["id"])
    instructions.transition(db, PEER, first["id"], "queued", "acknowledged")
    with pytest.raises(Conflict):
        instructions.enqueue(db, "nik", PEER, "Retry", "two", retry_of=first["id"])
    instructions.transition(db, PEER, first["id"], "acknowledged", "failed", result="error")
    second = instructions.enqueue(db, "nik", PEER, "Retry", "two", retry_of=first["id"])
    assert second["retry_of"] == first["id"]
    assert instructions.cancel(db, "nik", second["id"])["state"] == "cancelled"
    with pytest.raises(Conflict):
        instructions.cancel(db, "nik", first["id"])


def test_old_terminal_work_archives_metadata_and_does_not_permanently_fill_queue(db, monkeypatch):
    monkeypatch.setattr(instructions, "MAX_TOTAL", 1)
    old = instructions.enqueue(db, "nik", PEER, "Large resolved details", "first")
    instructions.transition(db, PEER, old["id"], "queued", "acknowledged")
    instructions.transition(db, PEER, old["id"], "acknowledged", "completed", result="Done")
    with db.write("test", "age terminal instruction") as tx:
        tx.cur.execute("UPDATE agent_instruction SET updated_at=? WHERE id=?",
                       (time.time() - 31 * 86400, old["id"]))
    fresh = instructions.enqueue(db, "nik", PEER, "New work", "second")
    assert fresh["state"] == "queued"
    with db.read() as cur:
        archived = cur.execute("SELECT * FROM agent_instruction_archive WHERE id=?",
                               (old["id"],)).fetchone()
        assert archived["author_user"] == "nik"
        assert archived["state"] == "completed"
        assert "body" not in archived.keys()
        history = cur.execute("SELECT action,actor FROM agent_instruction_archive_event WHERE "
                              "instruction_id=? ORDER BY created_at,id", (old["id"],)).fetchall()
        assert {row["action"] for row in history} == {"enqueue", "acknowledged", "completed"}
        assert any(row["actor"] == "ana-laptop-claude" for row in history)


def test_project_console_instruction_list_pages_newest_first(db, monkeypatch):
    from itertools import count
    ids = count(1)
    monkeypatch.setattr(instructions, "ulid", lambda: f"{next(ids):026d}")
    for n in range(3):
        instructions.enqueue(db, "nik", PEER, f"Work {n}", f"key-{n}")
    latest = instructions.list_project(db, limit=2)
    assert [i["body"] for i in latest["instructions"]] == ["Work 2", "Work 1"]
    older = instructions.list_project(db, limit=2, before_id=latest["older_cursor"])
    assert [i["body"] for i in older["instructions"]] == ["Work 0"]


@pytest.mark.anyio
async def test_agent_inbox_and_update_bind_identity_to_credential(env):
    app, project, _ = env
    ids = IdentityStore(app.state.cfg.identities_path)
    nik, ana = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    item = instructions.enqueue(project.db, "nik", PEER, "Review work", "q1")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t") as client:
        args = {"project": project.name, "client": "claude", "session_id": "ana-1"}
        denied = _call(await _post(client, "", nik, "tools/call", {
            "name": "agent_instruction_update", "arguments": {**args, "id": item["id"],
              "expected_state": "queued", "new_state": "acknowledged"}}))
        assert denied["ok"] is False
        read = _call(await _post(client, "", ana, "tools/call", {
            "name": "agent_instruction_inbox", "arguments": args}))
        assert [i["id"] for i in read["instructions"]] == [item["id"]]
        ack = _call(await _post(client, "", ana, "tools/call", {
            "name": "agent_instruction_update", "arguments": {**args, "id": item["id"],
              "expected_state": "queued", "new_state": "acknowledged"}}))
        assert ack["state"] == "acknowledged"
