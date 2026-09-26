"""Console mutations share the same project-scoped services as agent MCP."""

import httpx
import pytest
import time

from hivemind_server import assignments, instructions, teams
from hivemind_server.db import Conflict
from hivemind_server.identity import IdentityStore
from hivemind_server.ui_app import build_ui_app


@pytest.mark.anyio
async def test_console_creates_team_assigns_task_and_queues_manager_instruction(env, monkeypatch):
    from hivemind_server import ui_chat
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, ana = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(ui_chat.chat_ws, "hub_for", lambda path: Hub())
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
        from hivemind_server.chat import ChatStore
        notice = ChatStore(project.db).inbox(("ana", "laptop", "claude"))["messages"]
        assert len(notice) == 1 and nid in notice[0]["body"]
        assert notice[0]["sender"] == ("nik", "mac", "human")
        assert any(target == ("ana", "laptop", "claude") and frame["id"] == notice[0]["id"]
                   for target, frame in frames)
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
async def test_console_task_offer_announces_to_room_subscribers(env, monkeypatch):
    from hivemind_server import ui_chat
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    store = ChatStore(project.db)
    store.create_room("reviews", "Review work", ("nik", "mac", "codex"))
    store.join("reviews", ("ana", "laptop", "claude"))
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(ui_chat.chat_ws, "hub_for", lambda path: Hub())
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": nik})).json()["csrf_token"]
        offered = await c.post(f"/api/projects/{project.name}/tasks", headers={"x-csrf-token": csrf},
                               json={"room": "reviews", "title": "Review patch",
                                     "summary": "Check parser carefully"})
    assert offered.status_code == 201
    posts = store.history("reviews")["messages"]
    assert len(posts) == 1 and offered.json()["node_id"] in posts[0]["body"]
    assert posts[0]["sender_origin"] == "human_ui"
    assert frames[0][0] == ("ana", "laptop", "claude")


@pytest.mark.anyio
async def test_console_can_offer_task_to_room_manager_and_notify_them(env):
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    store = ChatStore(project.db)
    owner, manager = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    store.create_room("reviews", "Review work", owner)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    root = f"/api/projects/{project.name}/tasks"
    payload = {"room": "reviews", "title": "Audit", "summary": "Review patch",
               "assign_to_manager": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": nik})).json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        missing = await c.post(root, headers=headers, json=payload)
        assert missing.status_code == 422 and (await c.get(root)).json()["counts"]["available"] == 0
        teams.add_member(project.db, "reviews", manager, owner)
        teams.promote(project.db, "reviews", manager, owner, expected_revision=0)
        offered = await c.post(root, headers=headers, json=payload)
    assert offered.status_code == 201, offered.text
    node_id = offered.json()["node_id"]
    assert assignments.view(project.db, node_id)["assignee"] == manager
    inbox = store.inbox(manager)["messages"]
    assert len(inbox) == 1 and node_id in inbox[0]["body"]
    assert inbox[0]["sender"] == ("nik", "mac", "human")


@pytest.mark.anyio
async def test_console_updates_agent_capabilities_fences_claim_and_notifies_rooms(env, monkeypatch):
    from hivemind_server import capabilities, graph_tasks, ui_chat
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    owner, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    store = ChatStore(project.db)
    store.create_room("reviews", "Review work", owner)
    store.join("reviews", peer)
    capabilities.replace(project.db, peer, ["review", "python"])
    revision = capabilities.get(project.db, peer)["updated_at"]
    node = graph_tasks.offer(project.db, "setup", "reviews", "Audit", "Review patch",
                             required_capabilities=["review"])["node_id"]
    graph_tasks.claim(project.db, "setup", node, peer)
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(ui_chat.chat_ws, "hub_for", lambda path: Hub())
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": nik})).json()["csrf_token"]
        path = f"/api/projects/{project.name}/agents/capabilities"
        invalid = await c.post(path, headers={"x-csrf-token": csrf},
                               json={"address": list(peer), "capabilities": ["Uppercase"],
                                     "expected_updated_at": revision})
        assert invalid.status_code == 422
        missing_csrf = await c.post(path, json={"address": list(peer), "capabilities": ["python"]})
        assert missing_csrf.status_code == 403
        changed = await c.post(f"/api/projects/{project.name}/agents/capabilities",
                               headers={"x-csrf-token": csrf},
                               json={"address": list(peer), "capabilities": ["python"],
                                     "expected_updated_at": revision})
        stale = await c.post(path, headers={"x-csrf-token": csrf},
                             json={"address": list(peer), "capabilities": ["review"],
                                   "expected_updated_at": revision})
    assert changed.status_code == 200, changed.text
    assert stale.status_code == 409
    assert capabilities.get(project.db, peer)["capabilities"] == ["python"]
    assert graph_tasks.read(project.db, node)["effective_status"] == "unclaimed"
    assert len(store.inbox(peer)["messages"]) == 1
    posts = store.history("reviews")["messages"]
    assert len(posts) == 1 and "capabilities" in posts[0]["body"].lower()
    assert any(target == peer for target, _ in frames)


@pytest.mark.anyio
async def test_console_defines_project_capability_and_pushes_description_to_assigned_agent(env):
    from hivemind_server import capabilities
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, _ = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    peer = ("ana", "laptop", "claude")
    capabilities.replace(project.db, peer, ["review"])
    ChatStore(project.db).create_room("reviews", "Review work", ("nik", "mac", "codex"))
    ChatStore(project.db).join("reviews", peer)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    root = f"/api/projects/{project.name}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        csrf = (await c.post("/api/login", json={"token": nik})).json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        saved = await c.post(root + "/capabilities", headers=headers,
                             json={"name": "review", "description": "Assess code for correctness"})
        listed = await c.get(root + "/capabilities")
        invalid = await c.post(root + "/agents/capabilities", headers=headers,
                               json={"address": peer, "capabilities": ["unknown"]})
    assert saved.status_code == 200 and listed.status_code == 200
    assert listed.json()["capabilities"][0]["description"] == "Assess code for correctness"
    assert invalid.status_code == 422
    assert capabilities.get(project.db, peer)["capabilities"] == ["review"]
    dm = ChatStore(project.db).inbox(peer)["messages"]
    assert len(dm) == 1 and "Assess code for correctness" in dm[0]["body"]
    room = ChatStore(project.db).history("reviews")["messages"]
    assert len(room) == 1 and "Assess code for correctness" in room[0]["body"]


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
async def test_browser_room_post_notifies_subscribed_agent_and_uses_token_identity(env, monkeypatch):
    from hivemind_server import ui_chat
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    ids.mint("ana", "laptop")
    who, peer = ("nik", "mac", "human"), ("ana", "laptop", "claude")
    store = ChatStore(project.db)
    store.create_room("reviews", "Review", who)
    store.join("reviews", peer)
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
        root = f"/api/projects/{project.name}/messages/room"
        data = {"room": "reviews", "body": "Please review", "idempotency_key": "once",
                "sender": ["ana", "laptop", "claude"]}
        no_csrf = await c.post(root, json=data)
        sent = await c.post(root, json=data, headers={"x-csrf-token": csrf})
        missing = await c.post(root, json={**data, "room": "missing"},
                               headers={"x-csrf-token": csrf})
    assert no_csrf.status_code == 403
    assert sent.status_code == 201 and sent.json()["sender"] == list(who)
    assert sent.json()["notified_count"] == 1
    assert frames[0][0] == peer and "body" not in frames[0][1]
    assert frames[0][1]["room"] == "reviews"
    assert missing.status_code in (404, 422)


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
async def test_task_counts_and_filter_use_effective_states_across_all_pages(env):
    from hivemind_server import graph_tasks
    from hivemind_server.chat import ChatStore
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    who = ("nik", "mac", "codex")
    ChatStore(project.db).create_room("reviews", "Review", who)
    teams.add_member(project.db, "reviews", who, who)
    nodes = [graph_tasks.offer(project.db, "setup", "reviews", f"Task {n}", "Review")
             ["node_id"] for n in range(5)]
    assignments.assign(project.db, "setup", nodes[1], who)
    graph_tasks.claim(project.db, "setup", nodes[2], who)
    finished = graph_tasks.claim(project.db, "setup", nodes[3], who)
    graph_tasks.complete(project.db, "setup", nodes[3], finished["claim_token"], who)
    graph_tasks.claim(project.db, "setup", nodes[4], who, now=time.time() - 4000)
    app = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": token})
        base = f"/api/projects/{project.name}/tasks"
        first = (await c.get(base, params={"limit": 1, "status": "available"})).json()
        second = (await c.get(base, params={"limit": 1, "status": "available",
                                            "before_id": first["older_cursor"]})).json()
        claimed = (await c.get(base, params={"status": "in_progress"})).json()
        invalid = await c.get(base, params={"status": "unknown"})
    assert first["counts"] == {"available": 2, "assigned_waiting": 1,
                                "in_progress": 1, "complete": 1}
    assert {t["node_id"] for t in first["tasks"] + second["tasks"]} == {nodes[0], nodes[4]}
    assert all(t["state"] == "available" for t in first["tasks"] + second["tasks"])
    assert [t["node_id"] for t in claimed["tasks"]] == [nodes[2]]
    assert invalid.status_code == 422


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
