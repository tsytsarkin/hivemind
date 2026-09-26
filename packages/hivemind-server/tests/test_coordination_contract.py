"""One project-local read/write contract shared by browser and MCP adapters."""

from hivemind_server import assignments, capabilities, graph, graph_tasks, instructions, teams
from hivemind_server.chat import ChatStore


def test_capability_loss_releases_assigned_work_and_preserves_human_queue(db):
    who = ("nik", "mac", "codex")
    ChatStore(db).create_room("reviews", "Review work", who)
    teams.add_member(db, "reviews", who, who)
    nid = graph.upsert_node(db, "setup", "finding", {"title": "Audit"})["node_id"]
    room_id = ChatStore(db).rooms()[0]["room_id"]
    graph_tasks.enable(db, "setup", nid, room_id=room_id, required_capabilities=["python"])
    capabilities.replace(db, who, ["python"])
    assignments.assign(db, "manager", nid, who)
    item = instructions.enqueue(db, "nik", who, "Queue another review", "once")
    capabilities.replace(db, who, [])
    assert assignments.view(db, nid)["state"] == "available"
    assert instructions.inbox(db, who)["instructions"][0]["id"] == item["id"]
    assert who in teams.list_members(db, "reviews")
    assert capabilities.list_project(db)[0]["address"] == who
    assert instructions.list_project(db)["instructions"][0]["id"] == item["id"]
    assert assignments.list_room(db, "reviews")[0]["node_id"] == nid
