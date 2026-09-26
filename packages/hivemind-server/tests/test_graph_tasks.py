"""Graph-backed optional tasks: the node endures; beats stay outside graph revisions."""

import pytest

from hivemind_server import graph
from hivemind_server.db import Database, Invalid


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
