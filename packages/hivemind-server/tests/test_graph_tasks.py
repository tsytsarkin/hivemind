"""Graph-backed optional tasks: the node endures; beats stay outside graph revisions."""

import pytest

from hivemind_server import graph
from hivemind_server.db import Conflict, Database, Invalid, NotFound


OWNER = ("nik", "mac", "codex")
PEER = ("ana", "laptop", "claude")


def test_task_marker_accepts_arbitrary_schema_and_nonexclusive_activity(db):
    from hivemind_server import graph_tasks
    node = graph.upsert_node(db, "nik", "finding", {"title": "inspect parser"})
    nid = node["node_id"]
    marked = graph_tasks.enable(db, "nik", nid)
    assert marked["effective_status"] == "unclaimed"
    before = graph.get_node(db, node_id=nid)["current"]
    with db.read() as cur:
        tx_before = cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    graph_tasks.activity(db, nid, OWNER, now=1000)
    graph_tasks.activity(db, nid, PEER, now=1100)
    assert len(graph_tasks.read(db, nid, now=1101)["active_agents"]) == 2
    assert len(graph_tasks.read(db, nid, now=4701)["active_agents"]) == 0
    assert graph.get_node(db, node_id=nid)["current"] == before
    assert graph.get_node(db, node_id=nid)["task"]["effective_status"] == "unclaimed"
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == tx_before
    assert graph_tasks.read(Database(db.path), nid)["effective_status"] == "unclaimed"


def test_nonexclusive_activity_never_moves_backwards(db):
    from hivemind_server import graph_tasks
    nid = _task(db)
    graph_tasks.activity(db, nid, OWNER, now=1200)
    graph_tasks.activity(db, nid, OWNER, now=1100)
    assert graph_tasks.read(db, nid, now=1201)["active_agents"][0]["last_beat_at"] == 1200


def test_marked_task_heartbeat_does_not_require_status_field_in_node_schema(db):
    from hivemind_server import graph_tasks
    node = graph.upsert_node(db, "nik", "finding", {"title": "inspect parser"})
    graph_tasks.enable(db, "nik", node["node_id"])
    graph_tasks.activity(db, node["node_id"], OWNER, now=1000)
    assert "status" not in graph.get_node(db, node_id=node["node_id"])["current"]["props"]
    with pytest.raises(Invalid):
        graph_tasks.activity(db, node["node_id"], OWNER, interval_seconds=12, now=1200)


def _task(db):
    from hivemind_server import graph_tasks
    node = graph.upsert_node(db, "nik", "finding", {"title": "inspect parser"})
    graph_tasks.enable(db, "nik", node["node_id"])
    return node["node_id"]


def _versioned_task(db):
    from hivemind_server import graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "properties": {"title": {"type": "string"},
                             "status": {"enum": ["unclaimed", "in_progress", "complete"]}},
                             "required": ["title", "status"]}, status="active")
    nid = graph.upsert_node(db, "nik", "work_item", {"title": "audit parser",
                                                       "status": "unclaimed"})["node_id"]
    graph_tasks.enable(db, "nik", nid)
    return nid


def test_claim_heartbeat_does_not_version_node_or_stamp_tx(db):
    from hivemind_server import graph_tasks
    nid = _task(db)
    claimed = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert claimed["effective_status"] == "in_progress"
    head = graph.get_node(db, node_id=nid)["current"]["version_id"]
    with db.read() as cur:
        before = cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    for step in range(10):
        graph_tasks.heartbeat(db, nid, claimed["claim_token"], OWNER, now=1100 + 200 * step)
    assert graph_tasks.read(db, nid, now=2901)["effective_status"] == "in_progress"
    assert graph.get_node(db, node_id=nid)["current"]["version_id"] == head
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == before
        assert claimed["claim_token"] not in str(cur.execute(
            "SELECT * FROM graph_task_claim WHERE node_id=?", (nid,)).fetchone())


def test_expiry_and_takeover_fence_old_token_and_preserve_graph_status(db):
    from hivemind_server import graph_tasks
    nid = _versioned_task(db)
    old = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert graph_tasks.read(db, nid, now=4599)["effective_status"] == "in_progress"
    assert graph_tasks.read(db, nid, now=4600)["effective_status"] == "unclaimed"
    new = graph_tasks.claim(db, "ana", nid, PEER, now=4600)
    assert new["claim_token"] != old["claim_token"]
    assert graph_tasks.read(db, nid, now=4601)["effective_status"] == "in_progress"
    with pytest.raises(Conflict):
        graph_tasks.complete(db, "nik", nid, old["claim_token"], OWNER, now=4601)
    with pytest.raises(Conflict):
        graph_tasks.heartbeat(db, nid, new["claim_token"], OWNER, now=4601)
    assert graph_tasks.complete(db, "ana", nid, new["claim_token"], PEER, now=4602)["effective_status"] == "complete"
    with pytest.raises(Conflict):
        graph_tasks.claim(db, "nik", nid, OWNER, now=100_000)


def test_conflicting_claim_release_and_reap_are_atomic(db):
    from hivemind_server import graph_tasks
    nid = _task(db)
    first = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    with pytest.raises(Conflict):
        graph_tasks.claim(db, "ana", nid, PEER, now=1200)
    assert graph_tasks.reap_expired(db, now=1500) == 0
    assert graph_tasks.release(db, "nik", nid, first["claim_token"], OWNER, now=1600)["effective_status"] == "unclaimed"
    with pytest.raises(Conflict):
        graph_tasks.heartbeat(db, nid, first["claim_token"], OWNER, now=1601)
    graph_tasks.claim(db, "ana", nid, PEER, now=1700)
    assert graph_tasks.reap_expired(db, now=5300) == 1
    assert graph_tasks.read(db, nid, now=5300)["status"] == "unclaimed"


def test_expiry_settings_must_be_bounded_but_task_never_expires(db):
    from hivemind_server import graph_tasks
    nid = _task(db)
    for interval, expiry in ((29, 100), (300, 599), (300, 86401), (28801, 86400)):
        with pytest.raises(Invalid):
            graph_tasks.claim(db, "nik", nid, OWNER, interval_seconds=interval,
                              expires_after_seconds=expiry, now=1000)
    long = graph_tasks.claim(db, "nik", nid, OWNER, interval_seconds=28800,
                             expires_after_seconds=86400, now=1000)
    graph_tasks.heartbeat(db, nid, long["claim_token"], OWNER, now=1000 + 80_000)
    assert graph_tasks.read(db, nid, now=1000 + 80_001)["effective_status"] == "in_progress"


def test_structured_graph_status_versions_only_on_real_transitions(db):
    from hivemind_server import graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "properties": {"title": {"type": "string"},
                             "status": {"enum": ["unclaimed", "in_progress", "complete"]}},
                             "required": ["title", "status"]}, status="active")
    node = graph.upsert_node(db, "nik", "work_item", {"title": "audit parser",
                                                        "status": "unclaimed"})
    nid = node["node_id"]
    graph_tasks.enable(db, "nik", nid)
    old = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert graph.get_node(db, node_id=nid)["current"]["props"]["status"] == "in_progress"
    with pytest.raises(Invalid, match="task status"):
        graph.upsert_node(db, "nik", "work_item", {"title": "audit parser", "status": "complete"},
                          node_id=nid)
    graph_tasks.heartbeat(db, nid, old["claim_token"], OWNER, now=1100)
    assert len(graph.get_node(db, node_id=nid, history=True)["history"]) == 2
    graph_tasks.complete(db, "nik", nid, old["claim_token"], OWNER, now=1200)
    current = graph.get_node(db, node_id=nid, history=True)
    assert [h["props"]["status"] for h in current["history"]] == [
        "complete", "in_progress", "unclaimed"]


def test_parallel_claimers_have_one_winner(db):
    import threading
    from hivemind_server import graph_tasks
    nid = _task(db)
    outcomes = []
    mutex = threading.Lock()

    def worker(who):
        try:
            graph_tasks.claim(db, who[0], nid, who, now=1000)
            result = "claimed"
        except Conflict:
            result = "lost"
        with mutex:
            outcomes.append(result)

    workers = [threading.Thread(target=worker, args=(who,)) for who in (OWNER, PEER)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    assert sorted(outcomes) == ["claimed", "lost"]


def test_statusless_task_can_claim_but_cannot_complete_without_versioned_status(db):
    from hivemind_server import graph_tasks
    nid = _task(db)
    claimed = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    with pytest.raises(Invalid, match="versioned status"):
        graph_tasks.complete(db, "nik", nid, claimed["claim_token"], OWNER, now=1100)
    assert graph_tasks.read(db, nid, now=1100)["effective_status"] == "in_progress"
    graph_tasks.release(db, "nik", nid, claimed["claim_token"], OWNER, now=1101)


def test_existing_unrelated_or_restricted_status_uses_sidecar_not_invalid_graph_props(db):
    from hivemind_server import graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "restricted",
                            {"type": "object", "properties": {"status": {"enum": ["open"]}},
                             "required": ["status"], "additionalProperties": False}, status="active")
    nid = graph.upsert_node(db, "nik", "restricted", {"status": "open"})["node_id"]
    graph_tasks.enable(db, "nik", nid)
    claimed = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert claimed["status_mode"] == "sidecar"
    assert claimed["effective_status"] == "in_progress"
    assert graph.get_node(db, node_id=nid)["current"]["props"] == {"status": "open"}


def test_schema_that_only_accepts_unclaimed_can_still_use_sidecar_claim(db):
    from hivemind_server import graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "narrow",
                            {"type": "object", "properties": {"status": {"enum": ["unclaimed"]}},
                             "required": ["status"], "additionalProperties": False}, status="active")
    nid = graph.upsert_node(db, "nik", "narrow", {"status": "unclaimed"})["node_id"]
    assert graph_tasks.enable(db, "nik", nid)["status_mode"] == "sidecar"
    claim = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert claim["effective_status"] == "in_progress"
    assert graph.get_node(db, node_id=nid)["current"]["props"] == {"status": "unclaimed"}


def test_existing_claims_upgrade_to_versioned_status_without_losing_completion(db):
    from hivemind_server import graph_tasks
    nid = _versioned_task(db)
    claim = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    # Simulate the already-written 1.4.0 task table from the branch before status_mode existed.
    db.conn().execute("ALTER TABLE graph_task DROP COLUMN status_mode")
    db.conn().execute("DELETE FROM meta WHERE key='graph_task_status_mode_migrated_v1'")
    upgraded = Database(db.path)
    assert graph_tasks.read(upgraded, nid, now=1100)["status_mode"] == "versioned"
    graph_tasks.complete(upgraded, "nik", nid, claim["claim_token"], OWNER, now=1100)
    assert graph.get_node(upgraded, node_id=nid)["current"]["props"]["status"] == "complete"


def test_heartbeat_clock_never_regresses_or_renews_after_write_lock_expiry(db, monkeypatch):
    import contextlib
    from hivemind_server import graph_tasks
    nid = _task(db)
    claim = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    first = graph_tasks.heartbeat(db, nid, claim["claim_token"], OWNER, now=1200)
    second = graph_tasks.heartbeat(db, nid, claim["claim_token"], OWNER, now=1100)
    assert second["expires_at"] == first["expires_at"]

    class Clock:
        current = 4799

        @classmethod
        def time(cls):
            return cls.current

    monkeypatch.setattr(graph_tasks, "time", Clock)
    original = db.write_light

    @contextlib.contextmanager
    def lock_delayed():
        Clock.current = 4800  # expiry after previous last accepted beat 1200 + 3600
        with original() as cur:
            yield cur

    monkeypatch.setattr(db, "write_light", lock_delayed)
    with pytest.raises(Conflict):
        graph_tasks.heartbeat(db, nid, claim["claim_token"], OWNER)


def test_offering_graph_task_requires_explicit_room_and_schema(db):
    from hivemind_server import graph_tasks
    from hivemind_server.chat import ChatStore
    with pytest.raises(NotFound, match="room"):
        graph_tasks.offer(db, "nik", "missing", "Audit parser", "Check malformed input")
    assert ChatStore(db).rooms() == []
    ChatStore(db).create_room("parser", "Parser audit", OWNER)
    with pytest.raises(Invalid, match="schema"):
        graph_tasks.offer(db, "nik", "parser", "Audit parser", "Check malformed input")


def test_offered_graph_task_keeps_versioned_status_after_chat_history_expires(db):
    from hivemind_server import graph_tasks, schemas
    from hivemind_server.chat import ChatStore
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    room = ChatStore(db).create_room("parser", "Parser audit", OWNER)
    task = graph_tasks.offer(db, "nik", "parser", "Audit parser", "Check malformed input")
    assert task["task"]["room_id"] == room["room_id"]
    assert task["current"]["props"]["status"] == "unclaimed"
    claim = graph_tasks.claim(db, "nik", task["node_id"], OWNER, now=1000)
    ChatStore(db).send("room", "parser", OWNER, "working", "progress-1", kind="progress", now=1000)
    assert graph_tasks.read(db, task["node_id"], now=1000 + 899)["claim"]["progress_overdue"] is False
    assert graph_tasks.read(db, task["node_id"], now=1000 + 901)["claim"]["progress_overdue"] is True
    graph_tasks.complete(db, "nik", task["node_id"], claim["claim_token"], OWNER, now=1300)
    ChatStore(db).cleanup(now=1000 + 86400)
    assert graph.get_node(db, node_id=task["node_id"])["current"]["props"]["status"] == "complete"
    assert graph_tasks.read(Database(db.path), task["node_id"])["effective_status"] == "complete"


def test_progress_must_be_meaningful_and_correlated_with_its_claimed_task(db):
    from hivemind_server import graph_tasks, schemas
    from hivemind_server.chat import ChatStore
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    store = ChatStore(db)
    store.create_room("parser", "Parser work", OWNER)
    first = graph_tasks.offer(db, "nik", "parser", "Check lexer", "audit tokenizer")["node_id"]
    second = graph_tasks.offer(db, "nik", "parser", "Check parser", "audit parse tree")["node_id"]
    graph_tasks.claim(db, "nik", first, OWNER, now=1000)
    graph_tasks.claim(db, "nik", second, OWNER, now=1000)
    with pytest.raises(Invalid, match="progress"):
        store.send("room", "parser", OWNER, "   ", "empty", kind="progress",
                   task_node_id=first, now=1900)
    store.send("room", "parser", OWNER, "Lexer checks underway", "lexer-1",
               kind="progress", task_node_id=first, now=1900)
    assert graph_tasks.read(db, first, now=1901)["claim"]["progress_overdue"] is False
    assert graph_tasks.read(db, second, now=1901)["claim"]["progress_overdue"] is True


def test_accepted_task_progress_retry_survives_claim_expiry(db):
    from hivemind_server import graph_tasks, schemas
    from hivemind_server.chat import ChatStore
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    store = ChatStore(db)
    store.create_room("parser", "Parser work", OWNER)
    nid = graph_tasks.offer(db, "nik", "parser", "Check lexer", "audit tokenizer")["node_id"]
    graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    original = store.send("room", "parser", OWNER, "Lexer checks underway", "retry-1",
                          kind="progress", task_node_id=nid, now=1000)
    duplicate = store.send("room", "parser", OWNER, "Lexer checks underway", "retry-1",
                           kind="progress", task_node_id=nid, now=4600)
    assert duplicate["duplicate"] is True and duplicate["id"] == original["id"]
    with pytest.raises(Conflict):
        store.send("room", "parser", OWNER, "Different text", "retry-1",
                   kind="progress", task_node_id=nid, now=4601)


def test_new_task_progress_checks_lease_at_write_time(db, monkeypatch):
    import contextlib
    from hivemind_server import chat, graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    store = chat.ChatStore(db)
    store.create_room("parser", "Parser work", OWNER)
    nid = graph_tasks.offer(db, "nik", "parser", "Check lexer", "audit tokenizer")["node_id"]
    graph_tasks.claim(db, "nik", nid, OWNER, now=1000)

    class Clock:
        current = 4599

        @classmethod
        def time(cls):
            return cls.current

    monkeypatch.setattr(chat, "time", Clock)
    original = db.write_light

    @contextlib.contextmanager
    def delayed():
        Clock.current = 4600
        with original() as cur:
            yield cur

    monkeypatch.setattr(db, "write_light", delayed)
    with pytest.raises(Invalid, match="live claim"):
        store.send("room", "parser", OWNER, "New lexer work", "retry-new",
                   kind="progress", task_node_id=nid)


def test_a_roomless_task_is_never_reported_as_overdue(db):
    """progress_overdue was computed unconditionally while `progress` was only read for a task
    WITH a room. A room-less marker is a supported shape and can never receive a progress post —
    ChatStore.send refuses a task-correlated post whose room is not the task's — so every claimed
    room-less task went permanently overdue 15 minutes after the claim, however diligently its
    holder heartbeat. Peers read that as an abandoned claim."""
    from hivemind_server import graph_tasks
    nid = _task(db)                                   # enable() with room=None
    claimed = graph_tasks.claim(db, "nik", nid, OWNER, now=1000)
    assert graph_tasks.read(db, nid, now=1000)["claim"]["progress_overdue"] is False
    graph_tasks.heartbeat(db, nid, claimed["claim_token"], OWNER, now=1900)
    late = graph_tasks.read(db, nid, now=2500)        # 25 minutes after the claim
    assert late["claim"]["progress_overdue"] is False, \
        "a task with no room owes no room progress, so it can never be overdue"


def test_marking_a_started_node_does_not_advertise_it_as_unclaimed(db):
    """enable() asserted "unclaimed" whatever the node's own props said. Marking a work_item that
    was already in_progress or complete produced a marker disagreeing with the node version, and
    the claim guard reads the MARKER — so a finished task could still be claimed, and only
    graph_task_complete failed later with an unrelated message about a versioned status field."""
    from hivemind_server import graph_tasks, schemas
    with db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "properties": {"title": {"type": "string"},
                             "status": {"enum": ["unclaimed", "in_progress", "complete"]}},
                             "required": ["title", "status"]}, status="active")
    done = graph.upsert_node(db, "nik", "work_item",
                             {"title": "already shipped", "status": "complete"})["node_id"]
    marked = graph_tasks.enable(db, "nik", done)
    assert marked["status"] == "complete", marked
    assert marked["effective_status"] == "complete"
    with pytest.raises(Conflict):
        graph_tasks.claim(db, "nik", done, OWNER, now=1000)
    # and an untouched node still starts where it always did
    fresh = graph.upsert_node(db, "nik", "work_item",
                              {"title": "todo", "status": "unclaimed"})["node_id"]
    assert graph_tasks.enable(db, "nik", fresh)["status"] == "unclaimed"


def test_a_second_process_opening_the_same_database_does_not_abort_startup(tmp_path, monkeypatch):
    """The migration marker was checked OUTSIDE its BEGIN IMMEDIATE. Two processes opening one
    project DB both saw no marker; the loser blocked, then its bare INSERT hit the primary key and
    raised out of apply_schema — which runs from Database.__init__, so the server died at STARTUP
    rather than losing one query.

    Driven deterministically rather than by racing threads: a thread race reproduced this only
    about one run in three, which is the wrong kind of test to leave behind. The competing writer
    commits the marker in the exact window — after this process has read it as absent, before it
    inserts — by hooking the BEGIN that opens the transaction.
    """
    import sqlite3
    from hivemind_server.db import Database
    marker = "graph_task_status_mode_migrated_v1"
    path = tmp_path / "race.db"
    Database(path)                                     # first open: schema + marker
    scrub = sqlite3.connect(path)
    scrub.execute("DELETE FROM meta WHERE key=?", (marker,))
    scrub.commit()
    scrub.close()

    db = Database.__new__(Database)                    # build without running apply_schema
    Database.__init__(db, path)
    real_conn = db.conn()
    fired = []

    class Racer:
        """Delegates to the real connection, but lets a competitor win once, mid-window."""
        def execute(self, sql, *args):
            if sql.startswith("BEGIN IMMEDIATE") and not fired:
                fired.append(True)
                other = sqlite3.connect(path)
                other.execute("INSERT INTO meta(key,value) VALUES(?,?)", (marker, "1"))
                other.commit()
                other.close()
            return real_conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    scrub = sqlite3.connect(path)
    scrub.execute("DELETE FROM meta WHERE key=?", (marker,))
    scrub.commit()
    scrub.close()
    monkeypatch.setattr(db, "conn", lambda: Racer())
    db.apply_schema()                                  # must not raise
    assert fired, "the competing write never happened; the test proved nothing"
