"""Only the authenticated human console sees all project DMs."""

from hivemind_server import ui_chat
from hivemind_server.chat import ChatStore
from hivemind_server.db import Invalid
from hivemind_server.identity import Identity, IdentityStore
import time


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
    assert ui_chat.list_transcript(db, channel="dm")["messages"][0]["sender_origin"] == "human_ui"
    try:
        ui_chat.send_human_dm(db, who, store, ("ghost", "pc", "codex"), "Hello", "two")
    except Invalid:
        pass
    else:
        raise AssertionError("browser cannot send a DM to an unknown device")


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
