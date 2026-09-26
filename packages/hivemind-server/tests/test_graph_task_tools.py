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
                               kind="progress", idempotency_key="p1")
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
