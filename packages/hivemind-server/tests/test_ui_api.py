"""Console mutations share the same project-scoped services as agent MCP."""

import httpx
import pytest
import time

from hivemind_server import assignments, instructions, teams
from hivemind_server.db import Conflict
from hivemind_server.identity import IdentityStore
from hivemind_server.ui_app import build_ui_app


@pytest.mark.anyio
async def test_console_creates_team_assigns_task_and_queues_manager_instruction(env):
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, ana = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": nik})).json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        root = f"/api/projects/{project.name}"
        assert (await c.post(root + "/rooms", json={"name": "reviews",
                "description": "Patch reviews"})).status_code == 403
        room = await c.post(root + "/rooms", json={"name": "reviews",
                            "description": "Patch reviews"}, headers=headers)
        assert room.status_code == 201, room.text
        for address in (("nik", "mac", "codex"), ("ana", "laptop", "claude")):
            member = await c.post(root + "/rooms/reviews/members", json={"address": address},
                                  headers=headers)
            assert member.status_code == 200, member.text
        manager = await c.post(root + "/rooms/reviews/manager", headers=headers,
                               json={"address": ["nik", "mac", "codex"], "expected_revision": 0})
        assert manager.status_code == 200, manager.text
        assert (await c.get(root + "/rooms/reviews/manager")).json()["revision"] == 1
        task = await c.post(root + "/tasks", headers=headers,
                            json={"room": "reviews", "title": "Review a fix", "summary": "Check it"})
        assert task.status_code == 201, task.text
        nid = task.json()["node_id"]
        listed = (await c.get(root + "/tasks")).json()["tasks"]
        assert listed[0]["title"] == "Review a fix"
        assigned = await c.post(root + f"/tasks/{nid}/assign", headers=headers,
                                json={"address": ["ana", "laptop", "claude"],
                                      "expected_revision": 0})
        assert assigned.status_code == 200 and assigned.json()["state"] == "assigned_waiting", assigned.text
        assert assignments.view(project.db, nid)["assignee"] == ("ana", "laptop", "claude")
        queued = await c.post(root + "/instructions", headers=headers,
                              json={"room": "reviews", "to_manager": True,
                                    "body": "Queue the next patch", "idempotency_key": "once"})
        assert queued.status_code == 201, queued.text
        assert instructions.list_project(project.db)["instructions"][0]["recipient"] == \
            ("nik", "mac", "codex")
        instructions.transition(project.db, ("nik", "mac", "codex"), queued.json()["id"],
                                "queued", "acknowledged")
        instructions.transition(project.db, ("nik", "mac", "codex"), queued.json()["id"],
                                "acknowledged", "failed", result="Need another attempt")
        retry = await c.post(root + "/instructions", headers=headers,
                             json={"room": "reviews", "to_manager": True,
                                   "body": "Queue the next patch", "idempotency_key": "retry-once",
                                   "retry_of": queued.json()["id"]})
        assert retry.status_code == 201 and retry.json()["retry_of"] == queued.json()["id"]
        await c.post("/api/login", json={"token": ana})
        assert (await c.post(root + f"/tasks/{nid}/assign", headers=headers,
                             json={"address": ["nik", "mac", "codex"]})).status_code == 403
    assert teams.manager(project.db, "reviews")["manager"] == ("nik", "mac", "codex")


@pytest.mark.anyio
async def test_active_reassignment_requires_explicit_displacement_confirmation(env):
    from hivemind_server import graph_tasks
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    ids.mint("ana", "laptop")
    who, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("reviews", "Review", who)
    teams.add_member(project.db, "reviews", who, who)
    teams.add_member(project.db, "reviews", peer, who)
    nid = graph_tasks.offer(project.db, "setup", "reviews", "Audit", "Review patch")["node_id"]
    assignments.assign(project.db, "setup", nid, who)
    lease = graph_tasks.claim(project.db, "setup", nid, who)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": token})).json()["csrf_token"]
        root = f"/api/projects/{project.name}/tasks/{nid}"
        live = (await c.get(f"/api/projects/{project.name}/tasks")).json()["tasks"][0]
        assert live["claim"]["interval_seconds"] == 300
        assert live["claim"]["progress_overdue"] is False
        payload = {"address": peer, "expected_revision": 1}
        refusal = await c.post(root + "/assign", json=payload,
                               headers={"x-csrf-token": csrf})
        assert refusal.status_code == 409
        no_clear = await c.post(root + "/clear", json={"expected_revision": 1},
                                headers={"x-csrf-token": csrf})
        assert no_clear.status_code == 409
        assert assignments.view(project.db, nid)["assignee"] == who
        confirmed = await c.post(root + "/assign", json={**payload,"confirm_displace": True},
                                 headers={"x-csrf-token": csrf})
        assert confirmed.status_code == 200 and confirmed.json()["assignee"] == list(peer)
    with pytest.raises(Conflict):
        graph_tasks.heartbeat(project.db, nid, lease["claim_token"], who)


@pytest.mark.anyio
async def test_console_rejects_oversized_mutation_body(env):
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": token})).json()["csrf_token"]
        response = await c.post(f"/api/projects/{project.name}/rooms",
                                content=b"{" + b" " * 150_000,
                                headers={"content-type": "application/json",
                                         "x-csrf-token": csrf})
    assert response.status_code == 413


@pytest.mark.anyio
async def test_browser_dm_notifies_connected_recipient_after_persisting(env, monkeypatch):
    from hivemind_server import ui_chat
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    ids.mint("ana", "laptop")
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(ui_chat.chat_ws, "hub_for", lambda path: Hub())
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": token})).json()["csrf_token"]
        response = await c.post(f"/api/projects/{project.name}/dm",
                                json={"address": ["ana", "laptop", "claude"],
                                      "body": "Review this", "idempotency_key": "once"},
                                headers={"x-csrf-token": csrf})
    assert response.status_code == 201 and response.json()["notified_live"] is True
    assert frames[0][0] == ("ana", "laptop", "claude")
    assert frames[0][1]["id"] == response.json()["id"]
    assert "body" not in frames[0][1]


@pytest.mark.anyio
async def test_console_task_list_pages_beyond_first_bounded_batch(env):
    from hivemind_server import graph_tasks
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    who = ("nik", "mac", "codex")
    token = IdentityStore(mcp.state.cfg.identities_path).mint("nik", "mac")
    ChatStore(project.db).create_room("reviews", "Review", who)
    node_ids = [graph_tasks.offer(project.db, "setup", "reviews",
                f"Task {n}", "Review diff")["node_id"] for n in range(3)]
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, mcp.state.identities)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        base = f"/api/projects/{project.name}/tasks?limit=2"
        await c.post("/api/login", json={"token": token})
        first = (await c.get(base)).json()
        older = (await c.get(base + "&before_id=" + first["older_cursor"])).json()
    assert [t["node_id"] for t in first["tasks"] + older["tasks"]] == sorted(node_ids, reverse=True)


@pytest.mark.anyio
async def test_overview_summary_counts_work_outside_first_task_and_instruction_page(env, monkeypatch):
    from hivemind_server import graph_tasks
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("nik", "mac", "codex")
    ChatStore(project.db).create_room("reviews", "Review", who)
    teams.add_member(project.db, "reviews", who, who)
    ids_task = [graph_tasks.offer(project.db, "setup", "reviews",
                f"Task {n}", "Review")["node_id"] for n in range(2)]
    assignments.assign(project.db, "setup", min(ids_task), who)
    instructions.enqueue(project.db, "nik", who, "First", "first")
    instructions.enqueue(project.db, "nik", who, "Second", "second")
    def fail_unbounded_agents(self, **kwargs):
        raise AssertionError("summary must count live listeners without loading every session")
    monkeypatch.setattr(ChatStore, "agents", fail_unbounded_agents)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        root = f"/api/projects/{project.name}"
        assert len((await c.get(root + "/tasks?limit=1")).json()["tasks"]) == 1
        summary = (await c.get(root + "/summary")).json()
    assert summary["waiting_assignments"] == 1
    assert summary["queued_instructions"] == 2


@pytest.mark.anyio
async def test_roster_aggregates_online_and_old_sessions_by_stable_agent(env, monkeypatch):
    from hivemind_server import ui_api
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("ana", "laptop", "claude")
    store = ChatStore(project.db)
    store.touch(who, "old-session", now=time.time() - 7200)
    store.touch(who, "new-session")

    class Hub:
        def online(self, address, session=None):
            return session is None or session == "new-session"

    monkeypatch.setattr(ui_api.chat_ws, "hub_for", lambda path: Hub())
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        roster = (await c.get(f"/api/projects/{project.name}/agents")).json()["agents"]
    assert len(roster) == 1
    assert roster[0]["address"] == list(who) and roster[0]["online"] is True
    assert roster[0]["sessions"][0]["session_id"] == "new-session"
    assert {s["session_id"] for s in roster[0]["sessions"]} == {"old-session", "new-session"}


@pytest.mark.anyio
async def test_agent_roster_includes_capabilities_beyond_first_hundred(env):
    from hivemind_server import capabilities
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("nik", "mac", "codex")
    ChatStore(project.db).create_room("reviews", "Code reviews", who)
    for n in range(101):
        address = (f"agent{n}", "laptop", "codex")
        teams.add_member(project.db, "reviews", address, who)
        capabilities.replace(project.db, address,
                             ["python"] if n == 0 else ["review"])
    capabilities.replace(project.db, ("unrelated", "laptop", "codex"), ["review"])
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        root = f"/api/projects/{project.name}/agents"
        first = (await c.get(root)).json()
        second = (await c.get(root, params={"after": first["older_cursor"]})).json()
        caps = first["capabilities"] + second["capabilities"]
    assert len(first["agents"]) == 100 and len(second["agents"]) == 2
    assert len(caps) == 102
    assert any(c["address"][0] == "unrelated" for c in caps)
    assert any(c["address"] == ["agent0", "laptop", "codex"] and
               c["capabilities"] == ["python"] for c in caps)


@pytest.mark.anyio
async def test_capability_only_agents_remain_visible_after_presence_expiry_and_page(env):
    from hivemind_server import capabilities
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    for n in range(3):
        capabilities.replace(project.db, (f"agent{n}", "laptop", "codex"), ["review"])
    ChatStore(project.db).touch(("agent0", "laptop", "codex"), "expired",
                                now=time.time() - 86401)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        base = f"/api/projects/{project.name}/agents?limit=2"
        first = (await c.get(base)).json()
        second = (await c.get(base, params={"after": first["older_cursor"]})).json()
    agents = first["agents"] + second["agents"]
    assert len(agents) == 3
    assert agents[0]["address"] == ["agent0", "laptop", "codex"]
    assert agents[0]["online"] is False and agents[0]["sessions"] == []
    assert all(a["address"] != ["agent0", "laptop", "codex"]
               for a in second["agents"])
    assert second["older_cursor"] is None


@pytest.mark.anyio
async def test_room_and_member_rosters_page_without_fetching_entire_project(env):
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("nik", "mac", "codex")
    for n in range(3):
        name = f"topic{n}"
        ChatStore(project.db).create_room(name, "Review", who)
        for m in range(3):
            teams.add_member(project.db, name, (f"agent{m}", "laptop", "codex"), who)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        base = f"/api/projects/{project.name}/rooms"
        first = (await c.get(base, params={"limit": 2})).json()
        second = (await c.get(base, params={"limit": 2,
                                           "after": first["older_cursor"]})).json()
        member = (await c.get(base + "/topic0/members",
                              params={"limit": 2, "after":
                                      first["rooms"][0]["members_older_cursor"]})).json()
    assert [r["name"] for r in first["rooms"] + second["rooms"]] == \
        ["topic0", "topic1", "topic2"]
    assert first["rooms"][0]["member_count"] == 3
    assert len(first["rooms"][0]["members"]) == 2
    assert member["members"] == [["agent2", "laptop", "codex"]]


@pytest.mark.anyio
async def test_assignment_candidates_page_and_mark_missing_capability_tags(env):
    from hivemind_server import capabilities, graph_tasks
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("nik", "mac", "codex")
    ChatStore(project.db).create_room("review", "Review", who)
    for n in range(3):
        address = (f"agent{n}", "laptop", "codex")
        teams.add_member(project.db, "review", address, who)
        if n == 2:
            capabilities.replace(project.db, address, ["python"])
    nid = graph_tasks.offer(project.db, "setup", "review", "Review fix", "Run checks",
                            required_capabilities=["python"])["node_id"]
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        base = f"/api/projects/{project.name}/tasks/{nid}/candidates"
        first = (await c.get(base, params={"limit": 2})).json()
        second = (await c.get(base, params={"limit": 2,
                                           "after": first["older_cursor"]})).json()
    assert [p["missing"] for p in first["candidates"]] == [["python"], ["python"]]
    assert second["candidates"][0]["address"] == ["agent2", "laptop", "codex"]
    assert second["candidates"][0]["missing"] == []
