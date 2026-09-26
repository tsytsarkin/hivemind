"""Durable chat storage: rooms and identities are separate from live socket presence."""

import pytest

from hivemind_server.db import Conflict, Database, Invalid, NotFound
from hivemind_server.identity import Identity


def test_room_is_explicit_and_stays_joinable_after_restart(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    assert store.rooms() == []

    room = store.create_room("search-bugs", "Parser crashes", ("nikt", "mac", "codex"))
    store.join(room["name"], ("nikt", "mac", "codex"))

    restarted = ChatStore(Database(db.path))
    assert restarted.rooms()[0]["name"] == "search-bugs"
    assert restarted.rooms()[0]["description"] == "Parser crashes"
    assert restarted.subscribed("search-bugs", ("nikt", "mac", "codex"))
    assert not restarted.subscribed("search-bugs", ("nikt", "mac", "claude"))


def test_room_subscription_roster_changes_on_leave(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    store.create_room("search-bugs", "Parser crashes", ("nikt", "mac", "codex"))
    store.join("search-bugs", ("peer", "mac", "claude"))
    assert store.subscribers("search-bugs") == [("peer", "mac", "claude")]
    store.leave("search-bugs", ("peer", "mac", "claude"))
    assert store.subscribers("search-bugs") == []


def test_duplicate_room_name_and_missing_room_are_errors(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    owner = ("nikt", "mac", "codex")
    store.create_room("search-bugs", "Parser crashes", owner)
    with pytest.raises(Conflict):
        store.create_room("search-bugs", "Another topic", owner)
    with pytest.raises(NotFound):
        store.join("no-such-room", owner)


def test_hyphenated_identity_parts_do_not_collide(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    store.create_room("search-bugs", "Parser crashes", ("a-b", "c", "d"))
    store.join("search-bugs", ("a-b", "c", "d"))
    assert store.subscribed("search-bugs", ("a-b", "c", "d"))
    assert not store.subscribed("search-bugs", ("a", "b-c", "d"))


def test_stable_identity_uses_credentials_not_self_declared_username():
    from hivemind_server.chat import stable_identity
    assert stable_identity(Identity("nikt", "macbook"), "codex", "session-1") == (
        "nikt", "macbook", "codex", "session-1")
    with pytest.raises(Invalid):
        stable_identity(Identity("legacy:peer", "peer", legacy=True), "codex", "session-1")
    with pytest.raises(Invalid):
        stable_identity(Identity("nikt", "?"), "codex", "session-1")
    with pytest.raises(Invalid):
        stable_identity(Identity("nikt", "macbook"), "CODEX", "session-1")
    with pytest.raises(Invalid):
        stable_identity(Identity("nikt", "macbook"), "codex", "x" * 65)


SENDER = ("nikt", "mac", "codex")
RECEIVER = ("peer", "mac", "claude")
T0 = 1_700_000_000.0


def test_offline_dm_survives_database_reopen(db):
    from hivemind_server.chat import ChatStore
    first = ChatStore(db).send("dm", RECEIVER, SENDER, "hello", "retry-1", now=T0)
    rows = ChatStore(Database(db.path)).inbox(RECEIVER, 0, now=T0 + 10)["messages"]
    assert [m["id"] for m in rows] == [first["id"]]
    assert rows[0]["body"] == "hello"


def test_same_retry_key_is_idempotent_and_cannot_change_body(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    first = store.send("dm", RECEIVER, SENDER, "hello", "retry-1", now=T0)
    second = store.send("dm", RECEIVER, SENDER, "hello", "retry-1", now=T0 + 1)
    assert second["id"] == first["id"] and second["duplicate"] is True
    assert len(store.inbox(RECEIVER, 0, now=T0 + 2)["messages"]) == 1
    with pytest.raises(Conflict):
        store.send("dm", RECEIVER, SENDER, "changed", "retry-1", now=T0 + 2)


def test_room_history_available_to_late_joiner_for_24_hours(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    store.create_room("search-bugs", "Parser crashes", SENDER)
    sent = store.send("room", "search-bugs", SENDER, "fix is pending", "retry-2", now=T0)
    store.join("search-bugs", RECEIVER)
    assert [m["id"] for m in store.history("search-bugs", 0, now=T0 + 86_399)["messages"]] == [sent["id"]]
    assert store.history("search-bugs", 0, now=T0 + 86_400)["messages"] == []


def test_expired_empty_inbox_reports_gap_even_after_cleanup(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    sent = store.send("dm", RECEIVER, SENDER, "hello", "retry-1", now=T0)
    result = store.inbox(RECEIVER, 0, now=T0 + 86_400)
    assert result["messages"] == []
    assert result["gap"] is True
    assert result["expired_through_seq"] >= sent["seq"]


def test_chat_body_cap_counts_utf8_bytes(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    with pytest.raises(Invalid):
        store.send("dm", RECEIVER, SENDER, "\U0001F600" * 65_537, "retry-1", now=T0)


def test_project_quota_rejects_new_mail_without_evicting_unexpired(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db, max_bytes=520)
    first = store.send("dm", RECEIVER, SENDER, "hello", "retry-1", now=T0)
    with pytest.raises(Invalid, match="quota"):
        store.send("dm", RECEIVER, SENDER, "world", "retry-2", now=T0 + 1)
    assert [m["id"] for m in store.inbox(RECEIVER, 0, now=T0 + 2)["messages"]] == [first["id"]]


def test_private_dm_lookup_and_read_cursor_are_authorized(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    message = store.send("dm", RECEIVER, SENDER, "secret", "retry-1", now=T0)
    assert store.message(message["id"], RECEIVER, now=T0 + 1)["body"] == "secret"
    with pytest.raises(NotFound):
        store.message(message["id"], ("other", "mac", "codex"), now=T0 + 1)
    with pytest.raises(Invalid):
        store.mark_read(("other", "mac", "codex"), "dm", message["seq"], now=T0 + 1)
    store.mark_read(RECEIVER, "dm", message["seq"], now=T0 + 1)
    assert store.read_cursor(RECEIVER, "dm") == message["seq"]


def test_presence_expires_without_deleting_room_subscription(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    store.create_room("search-bugs", "Parser crashes", SENDER)
    store.join("search-bugs", RECEIVER)
    store.touch(RECEIVER, "session-1", now=T0)
    assert [p["session_id"] for p in store.agents(now=T0 + 86_399)] == ["session-1"]
    assert store.agents(now=T0 + 86_400) == []
    assert store.subscribed("search-bugs", RECEIVER)


def test_housekeeping_physically_purges_expired_chat_and_stale_sessions(db):
    from hivemind_server.chat import ChatStore
    store = ChatStore(db)
    store.send("dm", RECEIVER, SENDER, "expired", "retry-1", now=T0)
    store.touch(RECEIVER, "old-session", now=T0)
    assert store.cleanup(now=T0 + 86_400) == 1
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM chat_session").fetchone()[0] == 0
    assert store.inbox(RECEIVER, 0, now=T0 + 86_400)["gap"] is True
