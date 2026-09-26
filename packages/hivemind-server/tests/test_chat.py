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
