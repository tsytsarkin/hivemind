"""Project-local agent limits and MCP/web updates."""

import httpx
import pytest

from conftest import Lifespan, _call, _post
from hivemind_server import agent_config, capabilities, graph_tasks
from hivemind_server.chat import ChatStore
from hivemind_server.db import Conflict, Database
from hivemind_server.identity import IdentityStore
from hivemind_server.ui_app import build_ui_app


WHO = ("nik", "mac", "codex")


def test_config_persists_and_limits_live_task_claims(db):
    assert agent_config.get(db, WHO)["max_parallel_tasks"] == 20
    saved = agent_config.update(db, WHO, max_parallel_tasks=2, auto_claim_enabled=False,
                                capability_tags=["review"])
    assert saved["capabilities"] == ["review"] and saved["auto_claim_enabled"] is False
    assert agent_config.get(Database(db.path), WHO)["max_parallel_tasks"] == 2
    ChatStore(db).create_room("reviews", "Review", WHO)
    tasks = [graph_tasks.offer(db, "setup", "reviews", f"Task {n}", "Review",
                               required_capabilities=["review"])["node_id"] for n in range(3)]
    for node in tasks[:2]:
        graph_tasks.claim(db, "setup", node, WHO)
    with pytest.raises(Conflict, match="parallel task limit"):
        graph_tasks.claim(db, "setup", tasks[2], WHO)


@pytest.mark.anyio
async def test_agent_mcp_reads_and_updates_own_config(env):
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "mac")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        args = {"project": project.name, "client": "codex", "session_id": "one"}
        initial = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_config_get", "arguments": args}))
        updated = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_config_update", "arguments": {**args,
              "max_parallel_tasks": 3, "auto_claim_enabled": False,
              "capabilities": ["python"], "expected_updated_at": initial["updated_at"]}}))
        current = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_config_get", "arguments": args}))
    assert initial["max_parallel_tasks"] == 20 and initial["auto_claim_enabled"] is True
    assert updated["max_parallel_tasks"] == 3 and current["capabilities"] == ["python"]
    assert current["auto_claim_enabled"] is False


@pytest.mark.anyio
async def test_room_manager_can_read_peer_agent_config_for_capacity(env):
    app, project, _ = env
    ids = IdentityStore(app.state.cfg.identities_path)
    token, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    peer = ("ana", "laptop", "claude")
    agent_config.update(project.db, peer, max_parallel_tasks=4, auto_claim_enabled=False)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        other = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_config_get", "arguments": {"project": project.name,
              "client": "codex", "session_id": "one", "user": "ana", "device": "laptop",
              "agent_client": "claude"}}))
    assert other["address"] == list(peer) and other["max_parallel_tasks"] == 4
    assert other["auto_claim_enabled"] is False


@pytest.mark.anyio
async def test_browser_can_change_room_agents_config_without_leaking_to_other_projects(env):
    app, project, _ = env
    ids = IdentityStore(app.state.cfg.identities_path)
    token, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    target = ("ana", "laptop", "claude")
    ui = build_ui_app(app.state.cfg, app.state.registry, ids)
    root = f"/api/projects/{project.name}/agents/config"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://testserver") as client:
        csrf = (await client.post("/api/login", json={"token": token})).json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        first = await client.get(root, params=dict(zip(("user", "device", "client"), target)))
        saved = await client.post(root, headers=headers, json={"address": target,
            "max_parallel_tasks": 4, "auto_claim_enabled": False,
            "expected_updated_at": first.json()["updated_at"]})
        stale = await client.post(root, headers=headers, json={"address": target,
            "max_parallel_tasks": 6, "auto_claim_enabled": True,
            "expected_updated_at": first.json()["updated_at"]})
    assert saved.status_code == 200 and stale.status_code == 409
    assert agent_config.get(project.db, target)["max_parallel_tasks"] == 4


@pytest.mark.anyio
async def test_browser_config_capability_change_includes_description_and_room_announcement(env):
    app, project, _ = env
    ids = IdentityStore(app.state.cfg.identities_path)
    token, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    target = ("ana", "laptop", "claude")
    capabilities.define(project.db, "review", "Review for correctness")
    store = ChatStore(project.db)
    store.create_room("reviews", "Review", WHO)
    store.join("reviews", target)
    ui = build_ui_app(app.state.cfg, app.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://testserver") as client:
        csrf = (await client.post("/api/login", json={"token": token})).json()["csrf_token"]
        saved = await client.post(f"/api/projects/{project.name}/agents/config",
                                  headers={"x-csrf-token": csrf}, json={"address": target,
            "max_parallel_tasks": 2, "auto_claim_enabled": True,
            "expected_updated_at": None, "capabilities": ["review"],
            "expected_capabilities_updated_at": None})
    assert saved.status_code == 200, saved.text
    dm = store.inbox(target)["messages"]
    assert "Review for correctness" in dm[0]["body"]
    assert "Review for correctness" in store.history("reviews")["messages"][0]["body"]
