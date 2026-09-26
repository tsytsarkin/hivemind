"""Only the authenticated human console sees all project DMs."""

from hivemind_server import ui_chat
from hivemind_server.chat import ChatStore
from hivemind_server.db import Invalid
from hivemind_server.identity import Identity, IdentityStore
import time
import pytest


def test_console_reads_all_project_dms_without_widening_agent_inbox(db):
    sender, recipient, stranger = ("nik", "mac", "codex"), ("ana", "laptop", "claude"), \
        ("zoe", "pc", "codex")
    ChatStore(db).send("dm", recipient, sender, "review this", "one")
    assert ui_chat.list_transcript(db, channel="dm")["messages"][0]["body"] == "review this"
    assert ChatStore(db).inbox(stranger, 0, 10)["messages"] == []


def test_console_reads_room_history_without_subscribing(db):
    sender = ("nik", "mac", "codex")
    ChatStore(db).create_room("review", "Reviews", sender)
    ChatStore(db).send("room", "review", sender, "Working on it", "once")
    page = ui_chat.list_transcript(db, channel="room", room="review")
    assert [m["body"] for m in page["messages"]] == ["Working on it"]


def test_browser_dm_is_human_origin_and_requires_real_recipient(db, tmp_path):
    store = IdentityStore(tmp_path / "ids.json")
    store.mint("ana", "laptop")
    who = Identity("nik", "mac")
    sent = ui_chat.send_human_dm(db, who, store, ("ana", "laptop", "claude"), "Hello", "one")
    assert sent["sender_origin"] == "human_ui"
    assert sent["sender"] == ("nik", "mac", "human")
    assert ui_chat.list_transcript(db, channel="dm")["messages"][0]["sender_origin"] == "human_ui"
    try:
        ui_chat.send_human_dm(db, who, store, ("ghost", "pc", "codex"), "Hello", "two")
    except Invalid:
        pass
    else:
        raise AssertionError("browser cannot send a DM to an unknown device")


def test_browser_room_post_is_from_token_identity_and_keeps_room_history(db):
    who = Identity("nik", "mac")
    ChatStore(db).create_room("reviews", "Review", ("nik", "mac", "codex"))
    sent = ui_chat.send_human_room(db, who, "reviews", "Please review", "once")
    assert sent["sender"] == ("nik", "mac", "human")
    assert sent["sender_origin"] == "human_ui"
    assert ui_chat.list_transcript(db, channel="room", room="reviews")["messages"][0][
        "body"] == "Please review"


@pytest.mark.anyio
async def test_human_room_push_failure_does_not_skip_other_subscribers(db, tmp_path, monkeypatch):
    owner = ("nik", "mac", "codex")
    first, second = ("ana", "laptop", "claude"), ("zoe", "desktop", "codex")
    store = ChatStore(db)
    store.create_room("reviews", "Review", owner)
    store.join("reviews", first)
    store.join("reviews", second)
    ids = IdentityStore(tmp_path / "identities.json")
    ids.mint("ana", "laptop")
    ids.mint("zoe", "desktop")
    sent = ui_chat.send_human_room(db, Identity("nik", "mac"), "reviews", "Update", "one")
    delivered = []

    class Hub:
        async def notify(self, recipient, frame):
            delivered.append(recipient)
            if recipient == first:
                raise OSError("subscriber disconnected")
            assert frame["id"] == sent["id"]
            return 1

    monkeypatch.setattr(ui_chat.chat_ws, "hub_for", lambda path: Hub())
    monkeypatch.setattr(ui_chat, "can_access", lambda identity, meta: True)
    count = await ui_chat.notify_human_room(tmp_path, db, object(), ids, "reviews", sent)
    assert delivered == [first, second]
    assert count == 1


def test_room_history_expiration_indicator_is_scoped_to_that_room(db):
    who = ("nik", "mac", "codex")
    store = ChatStore(db)
    store.create_room("old", "Old work", who)
    store.create_room("new", "New work", who)
    store.send("room", "old", who, "expired", "once", now=time.time() - 25 * 3600)
    store.send("room", "new", who, "current", "once")
    assert ui_chat.list_transcript(db, channel="room", room="old")["history_gap"] is True
    assert ui_chat.list_transcript(db, channel="room", room="new")["history_gap"] is False


def test_console_reports_expired_history_even_before_cleanup_persists_watermark(db):
    who = ("nik", "mac", "codex")
    store = ChatStore(db)
    store.create_room("old", "Old work", who)
    store.send("room", "old", who, "expired", "once", now=time.time() - 25 * 3600)
    page = ui_chat.list_transcript(db, channel="room", room="old")
    assert page["messages"] == [] and page["history_gap"] is True


def test_browser_read_marker_is_separate_from_agent_private_dm_cursor(db):
    sender, recipient = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    message = ChatStore(db).send("dm", recipient, sender, "Review this", "once")
    browser = ("nik", "mac", "webui")
    assert ui_chat.mark_console_read(db, browser, channel="dm", seq=message["seq"])[
        "last_read_message_id"] == message["id"]
    assert ui_chat.list_transcript(db, channel="dm", reader=browser)[
        "last_read_seq"] == message["seq"]
    assert ChatStore(db).read_marker(sender, "dm")["last_read_seq"] == 0


def test_console_begins_with_latest_message_page_and_can_page_back(db):
    who = ("nik", "mac", "codex")
    store = ChatStore(db)
    store.create_room("review", "Patch reviews", who)
    for n in range(105):
        store.send("room", "review", who, f"message-{n}", f"key-{n}")
    latest = ui_chat.list_transcript(db, channel="room", room="review")
    assert latest["messages"][0]["body"] == "message-5"
    assert latest["messages"][-1]["body"] == "message-104"
    older = ui_chat.list_transcript(db, channel="room", room="review",
                                    before_seq=latest["older_cursor"])
    assert [m["body"] for m in older["messages"]] == [f"message-{n}" for n in range(5)]
    reader = ("nik", "mac", "webui")
    unseen = ui_chat.list_transcript(db, channel="room", room="review", reader=reader)
    assert unseen["unread_count"] == 105
    assert unseen["older_cursor"] is not None
