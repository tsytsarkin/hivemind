"""Authenticated MCP graph tasks live in an explicitly created project room."""

import httpx
import pytest

from conftest import Lifespan, _call, _post
from hivemind_server import graph, projects_meta, schemas
from hivemind_server.chat import ChatStore
from hivemind_server.identity import IdentityStore


def _token(app, user, device):
    return IdentityStore(app.state.cfg.identities_path).mint(user, device)


async def _tool(http_client, token, project, tool, **args):
    return _call(await _post(http_client, "", token, "tools/call", {
        "name": tool, "arguments": {"project": project, **args}}))


@pytest.mark.anyio
async def test_offer_claim_progress_and_complete_via_authenticated_mcp(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    with project.db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        refused = await _tool(client, nik, project.name, "graph_task_offer", room="parser",
                              title="Audit parser", summary="check bug", client="codex",
                              session_id="sid-1")
        assert refused["ok"] is False and ChatStore(project.db).rooms() == []
        await _tool(client, nik, project.name, "chat_room_create", name="parser",
                    description="Parser work", client="codex", session_id="sid-1")
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="parser",
                              title="Audit parser", summary="check bug", client="codex",
                              session_id="sid-1")
        nid = offered["node_id"]
        assert offered["task"]["effective_status"] == "unclaimed"
        claimed = await _tool(client, nik, project.name, "graph_task_claim", node_id=nid,
                              client="codex", session_id="sid-1")
        assert claimed["effective_status"] == "in_progress"
        assert "claim_token" in claimed
        denied = await _tool(client, ana, project.name, "graph_task_heartbeat", node_id=nid,
                             claim_token=claimed["claim_token"], client="claude", session_id="sid-2")
        assert denied["ok"] is False
        beat = await _tool(client, nik, project.name, "graph_task_heartbeat", node_id=nid,
                           claim_token=claimed["claim_token"], client="codex", session_id="sid-2")
        assert beat["ok"] is True
        progress = await _tool(client, nik, project.name, "chat_room_post", name="parser",
                               client="codex", session_id="sid-2", body="checking parser",
                               kind="progress", task_node_id=nid, idempotency_key="p1")
        assert progress["ok"] is True
        fetched = await _tool(client, ana, project.name, "graph_task_get", node_id=nid,
                              client="claude", session_id="sid-2")
        assert fetched["task"]["claim"]["progress_overdue"] is False
        assert "claim_token" not in str(fetched)
        completed = await _tool(client, nik, project.name, "graph_task_complete", node_id=nid,
                                claim_token=claimed["claim_token"], client="codex", session_id="sid-2")
        assert completed["effective_status"] == "complete"
        assert graph.get_node(project.db, node_id=nid)["current"]["props"]["status"] == "complete"


@pytest.mark.anyio
async def test_private_project_blocks_nonmember_task_access(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    outsider = _token(app, "outsider", "laptop")
    with project.db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "work_item",
                            {"type": "object", "additionalProperties": True}, status="active")
    ChatStore(project.db).create_room("parser", "Parser work", ("nik", "mac", "codex"))
    from hivemind_server import graph_tasks
    nid = graph_tasks.offer(project.db, "nik", "parser", "Audit parser", "check bug")["node_id"]
    meta = project.meta
    meta.visibility = "private"
    meta.owner = "nik"
    projects_meta.save(project.dir, meta)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        denied = await _post(client, "", outsider, "tools/call", {
            "name": "graph_task_get", "arguments": {"project": project.name, "node_id": nid,
                                                     "client": "claude", "session_id": "sid-2"}})
        assert _call(denied)["ok"] is False
        allowed = await _tool(client, nik, project.name, "graph_task_get", node_id=nid,
                              client="codex", session_id="sid-1")
        assert allowed["ok"] is True


@pytest.mark.anyio
async def test_mcp_offer_in_schema_less_project_records_capability_requirements(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ChatStore(project.db).create_room("parser", "Parser work", ("nik", "mac", "codex"))
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="parser",
                              title="Audit parser", summary="review diff", client="codex",
                              session_id="sid-1", required_capabilities=["review"])
    assert offered["ok"] is True
    assert offered["task"]["required_capabilities"] == ["review"]


@pytest.mark.anyio
async def test_manager_assigns_through_mcp_and_assignee_discovers_waiting_work(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    from hivemind_server import capabilities, graph_tasks, teams

    room = ChatStore(project.db).create_room("review-work", "Review work",
                                              ("nik", "mac", "codex"))
    owner = ("nik", "mac", "codex")
    peer = ("ana", "laptop", "claude")
    teams.add_member(project.db, "review-work", owner, owner)
    teams.add_member(project.db, "review-work", peer, owner)
    teams.promote(project.db, "review-work", owner, owner, expected_revision=0)
    capabilities.replace(project.db, peer, ["review"])
    node_id = graph_tasks.offer(project.db, "setup", "review-work", "Audit code",
                                "Review the patch", required_capabilities=["review"])["node_id"]
    assert room["room_id"] == graph_tasks.read(project.db, node_id)["room_id"]

    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        denied_response = await _post(client, "", ana, "tools/call", {
            "name": "graph_task_assign",
            "arguments": {"project": project.name, "node_id": node_id,
                          "to_user": "ana", "to_device": "laptop", "to_client": "claude",
                          "client": "claude", "session_id": "ana-1", "expected_revision": 0}})
        assert "Unknown tool" not in denied_response.text
        assert _call(denied_response)["ok"] is False
        assigned = await _tool(client, nik, project.name, "graph_task_assign", node_id=node_id,
                               to_user="ana", to_device="laptop", to_client="claude",
                               client="codex", session_id="nik-1", expected_revision=0)
        mine = await _tool(client, ana, project.name, "graph_task_my_assignments",
                           client="claude", session_id="ana-2")
    assert assigned["ok"] is True and assigned["state"] == "assigned_waiting"
    assert [task["node_id"] for task in mine["assignments"]] == [node_id]


@pytest.mark.anyio
async def test_mcp_can_change_task_requirements_and_release_ineligible_assignment(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    from hivemind_server import assignments, capabilities, graph_tasks, teams

    owner = ("nik", "mac", "codex")
    ChatStore(project.db).create_room("review-work", "Review work", owner)
    teams.add_member(project.db, "review-work", owner, owner)
    capabilities.replace(project.db, owner, ["review"])
    node_id = graph_tasks.offer(project.db, "setup", "review-work", "Audit code",
                                "Review the patch", required_capabilities=["review"])["node_id"]
    assignments.assign(project.db, "setup", node_id, owner)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", nik, "tools/call", {
            "name": "graph_task_requirements_set",
            "arguments": {"project": project.name, "node_id": node_id,
                          "required_capabilities": ["python"], "client": "codex",
                          "session_id": "nik-1"}})
        assert "Unknown tool" not in response.text
        result = _call(response)
    assert result["ok"] is True
    assert assignments.view(project.db, node_id)["state"] == "available"


@pytest.mark.anyio
async def test_manager_cannot_assign_room_member_after_their_private_project_access_is_revoked(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    _token(app, "ana", "laptop")
    from hivemind_server import graph_tasks, teams

    owner, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("review-work", "Review work", owner)
    teams.add_member(project.db, "review-work", owner, owner)
    teams.add_member(project.db, "review-work", peer, owner)
    teams.promote(project.db, "review-work", owner, owner, expected_revision=0)
    node_id = graph_tasks.offer(project.db, "setup", "review-work", "Audit code",
                                "Review the patch")["node_id"]
    meta = project.meta
    meta.visibility, meta.owner = "private", "nik"
    projects_meta.save(project.dir, meta)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", nik, "tools/call", {
            "name": "graph_task_assign",
            "arguments": {"project": project.name, "node_id": node_id,
                          "to_user": "ana", "to_device": "laptop", "to_client": "claude",
                          "client": "codex", "session_id": "nik-1", "expected_revision": 0}})
        assert "Unknown tool" not in response.text
        assert _call(response)["ok"] is False
