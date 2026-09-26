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
    nid = _task(db)
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
