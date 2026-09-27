"""Authenticated MCP graph tasks live in an explicitly created project room."""

import httpx
import pytest

from conftest import Lifespan, _call, _post
from hivemind_server import capabilities, graph, projects_meta, schemas
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
    capabilities.define(project.db, "review", "Review source")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="parser",
                              title="Audit parser", summary="review diff", client="codex",
                              session_id="sid-1", required_capabilities=["review"])
    assert offered["ok"] is True
    assert offered["task"]["required_capabilities"] == ["review"]


@pytest.mark.anyio
async def test_mcp_task_offer_announces_it_to_subscribed_room(env, monkeypatch):
    from hivemind_server import chat_ws, teams
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    _token(app, "ana", "laptop")
    owner, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("reviews", "Review work", owner)
    teams.add_member(project.db, "reviews", peer, owner)
    capabilities.define(project.db, "review", "Review source")
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(chat_ws, "hub_for", lambda path: Hub())
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="reviews",
                              title="Review patch", summary="Check parser carefully",
                              required_capabilities=["review"], client="codex", session_id="nik-1")
    assert offered["ok"] is True
    posts = ChatStore(project.db).history("reviews")["messages"]
    assert len(posts) == 1
    assert offered["node_id"] in posts[0]["body"]
    assert "Review patch" in posts[0]["body"] and "review" in posts[0]["body"]
    assert frames[0][0] == peer and frames[0][1]["id"] == posts[0]["id"]


@pytest.mark.anyio
async def test_room_roster_failure_cannot_undo_offered_task_or_announcement(env, monkeypatch):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ChatStore(project.db).create_room("reviews", "Review work", ("nik", "mac", "codex"))

    def unavailable(*args):
        raise OSError("subscriber lookup failed")

    monkeypatch.setattr(ChatStore, "subscribers", unavailable)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="reviews",
                              title="Audit", summary="Review patch",
                              client="codex", session_id="nik-1")
    assert offered["ok"] is True and offered["room_notification"] == "stored"
    assert len(ChatStore(project.db).history("reviews")["messages"]) == 1


@pytest.mark.anyio
async def test_enabling_an_existing_node_as_room_task_announces_it(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ChatStore(project.db).create_room("reviews", "Review work", ("nik", "mac", "codex"))
    capabilities.define(project.db, "review", "Review source")
    with project.db.write("setup") as tx:
        schemas.define_type(tx.cur, tx, "node", "finding",
                            {"type": "object", "additionalProperties": True}, status="active")
    nid = graph.upsert_node(project.db, "nik", "finding", {"title": "Investigate bug"})["node_id"]
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        marked = await _tool(client, nik, project.name, "graph_task_enable", node_id=nid,
                             room="reviews", required_capabilities=["review"],
                             client="codex", session_id="nik-1")
    assert marked["ok"] is True
    assert marked["room_notification"] == "stored"
    posts = ChatStore(project.db).history("reviews")["messages"]
    assert len(posts) == 1 and nid in posts[0]["body"]


@pytest.mark.anyio
async def test_completion_posts_a_room_event_and_notifies_subscribed_peers(env, monkeypatch):
    from hivemind_server import chat_ws, teams
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    _token(app, "ana", "laptop")
    who, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("reviews", "Review", who)
    teams.add_member(project.db, "reviews", peer, who)
    frames = []

    class Hub:
        async def notify(self, target, frame):
            frames.append((target, frame))
            return 1

    monkeypatch.setattr(chat_ws, "hub_for", lambda path: Hub())
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        task = await _tool(client, nik, project.name, "graph_task_offer", room="reviews",
                           title="Audit parser", summary="Check parser", client="codex",
                           session_id="one")
        lease = await _tool(client, nik, project.name, "graph_task_claim",
                            node_id=task["node_id"], client="codex", session_id="one")
        done = await _tool(client, nik, project.name, "graph_task_complete",
                           node_id=task["node_id"], claim_token=lease["claim_token"],
                           client="codex", session_id="one")
    assert done["ok"] is True and done["room_notification"] == "stored"
    messages = ChatStore(project.db).history("reviews")["messages"]
    assert len(messages) == 2
    assert "completed" in messages[-1]["body"].lower() and task["node_id"] in messages[-1]["body"]
    assert lease["claim_token"] not in messages[-1]["body"]
    assert any(target == peer and frame["id"] == messages[-1]["id"] for target, frame in frames)


@pytest.mark.anyio
async def test_chat_quota_cannot_roll_back_a_completed_graph_task(env):
    import json
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ChatStore(project.db).create_room("reviews", "Review", ("nik", "mac", "codex"))
    (project.dir / "chat_limits.json").write_text(json.dumps({"max_messages": 1,
                                                         "max_bytes": 10000}))
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        offered = await _tool(client, nik, project.name, "graph_task_offer", room="reviews",
                              title="Audit", summary="Review parser", client="codex",
                              session_id="one")
        lease = await _tool(client, nik, project.name, "graph_task_claim",
                            node_id=offered["node_id"], client="codex", session_id="one")
        completed = await _tool(client, nik, project.name, "graph_task_complete",
                                node_id=offered["node_id"], claim_token=lease["claim_token"],
                                client="codex", session_id="one")
    assert completed["ok"] is True and completed["effective_status"] == "complete"
    assert "could not persist" in completed["room_notification"]


@pytest.mark.anyio
async def test_available_task_discovery_pages_only_eligible_unreserved_work(env):
    from hivemind_server import assignments, capabilities, graph_tasks, teams
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    who, other = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("reviews", "Code reviews", who)
    teams.add_member(project.db, "reviews", other, who)
    capabilities.define(project.db, "review", "Review source")
    capabilities.define(project.db, "python", "Implement Python")
    capabilities.replace(project.db, who, ["review"], managed_by_ui=True)
    capabilities.replace(project.db, other, ["review"], managed_by_ui=True)
    nodes = [graph_tasks.offer(project.db, "setup", "reviews", f"Task {n}", "Review",
                              required_capabilities=(["review"] if n != 2 else ["python"]))
             ["node_id"] for n in range(5)]
    assignments.assign(project.db, "setup", nodes[3], other)
    graph_tasks.claim(project.db, "setup", nodes[4], who)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        first = await _tool(client, nik, project.name, "graph_task_available", client="codex",
                            session_id="sid-1", limit=1)
        second = await _tool(client, nik, project.name, "graph_task_available", client="codex",
                             session_id="sid-1", limit=1, before_id=first["older_cursor"])
    assert {t["node_id"] for t in first["tasks"] + second["tasks"]} == {nodes[0], nodes[1]}
    assert first["older_cursor"] and second["older_cursor"] is None
    assert all(t["state"] == "available" and t["required_capabilities"] == ["review"]
               and t["summary"] == "Review" and t["room"] == "reviews"
               for t in first["tasks"] + second["tasks"])
    assert "claim_token" not in str(first) + str(second)


@pytest.mark.anyio
async def test_room_manager_can_page_room_work_and_find_overdue_progress(env):
    import time
    from hivemind_server import assignments, graph_tasks, teams

    app, project, _ = env
    nik, ana = _token(app, "nik", "mac"), _token(app, "ana", "laptop")
    owner, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("reviews", "Review work", owner)
    ChatStore(project.db).create_room("unrelated", "Other work", owner)
    teams.add_member(project.db, "reviews", owner, owner)
    teams.add_member(project.db, "reviews", peer, owner)
    teams.promote(project.db, "reviews", owner, owner, expected_revision=0)
    available = graph_tasks.offer(project.db, "setup", "reviews", "Start", "unclaimed")["node_id"]
    assigned = graph_tasks.offer(project.db, "setup", "reviews", "Wait", "assigned")["node_id"]
    active = graph_tasks.offer(project.db, "setup", "reviews", "Report", "progress")["node_id"]
    graph_tasks.offer(project.db, "setup", "unrelated", "Elsewhere", "ignore")
    assignments.assign(project.db, "setup", assigned, peer)
    graph_tasks.claim(project.db, "setup", active, peer)
    with project.db.write_light() as cur:
        cur.execute("UPDATE graph_task_claim SET claimed_at=?,last_beat_at=? WHERE node_id=?",
                    (time.time() - 1800, time.time() - 120, active))

    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        refused = await _tool(client, ana, project.name, "graph_task_room_status", room="reviews",
                              client="claude", session_id="ana-1", limit=1)
        first = await _tool(client, nik, project.name, "graph_task_room_status", room="reviews",
                            client="codex", session_id="nik-1", limit=2)
        second = await _tool(client, nik, project.name, "graph_task_room_status", room="reviews",
                             client="codex", session_id="nik-1", limit=2,
                             before_id=first["older_cursor"])
    assert refused["ok"] is False
    assert first["counts"] == {"available": 1, "assigned_waiting": 1,
                               "in_progress": 1, "complete": 0}
    assert {task["node_id"] for task in first["tasks"] + second["tasks"]} == \
           {available, assigned, active}
    assert first["older_cursor"] and second["older_cursor"] is None
    by_id = {task["node_id"]: task for task in first["tasks"] + second["tasks"]}
    assert by_id[assigned]["assignee"] == list(peer)
    assert by_id[assigned]["revision"] == 1
    assert by_id[active]["claim"]["progress_overdue"] is True
    assert by_id[active]["claim"]["holder"] == list(peer)
    assert "claim_token" not in str(first) + str(second)


@pytest.mark.anyio
async def test_manager_assigns_through_mcp_and_assignee_discovers_waiting_work(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    from hivemind_server import assignments, capabilities, graph_tasks, teams

    room = ChatStore(project.db).create_room("review-work", "Review work",
                                              ("nik", "mac", "codex"))
    owner = ("nik", "mac", "codex")
    peer = ("ana", "laptop", "claude")
    teams.add_member(project.db, "review-work", owner, owner)
    teams.add_member(project.db, "review-work", peer, owner)
    teams.promote(project.db, "review-work", owner, owner, expected_revision=0)
    capabilities.define(project.db, "review", "Review source")
    capabilities.replace(project.db, peer, ["review"], managed_by_ui=True)
    node_id = graph_tasks.offer(project.db, "setup", "review-work", "Audit code",
                                "Review the patch", required_capabilities=["review"])["node_id"]
    assert room["room_id"] == graph_tasks.read(project.db, node_id)["room_id"]

    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        missing_revision = await _post(client, "", nik, "tools/call", {
            "name": "graph_task_assign",
            "arguments": {"project": project.name, "node_id": node_id,
                          "to_user": "ana", "to_device": "laptop", "to_client": "claude",
                          "client": "codex", "session_id": "nik-1"}})
        assert "expected_revision" in missing_revision.text
        assert assignments.view(project.db, node_id)["state"] == "available"
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
    capabilities.define(project.db, "review", "Review source")
    capabilities.define(project.db, "python", "Implement Python")
    capabilities.replace(project.db, owner, ["review"], managed_by_ui=True)
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


@pytest.mark.anyio
async def test_only_manager_can_cancel_an_offline_task_assignment(env):
    app, project, _ = env
    nik, ana = _token(app, "nik", "mac"), _token(app, "ana", "laptop")
    from hivemind_server import assignments, graph_tasks, teams

    owner, peer = ("nik", "mac", "codex"), ("ana", "laptop", "claude")
    ChatStore(project.db).create_room("review-work", "Review work", owner)
    teams.add_member(project.db, "review-work", owner, owner)
    teams.add_member(project.db, "review-work", peer, owner)
    teams.promote(project.db, "review-work", owner, owner, expected_revision=0)
    node_id = graph_tasks.offer(project.db, "setup", "review-work", "Audit code",
                                "Review the patch")["node_id"]
    assignments.assign(project.db, "setup", node_id, peer)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        refused = await _post(client, "", ana, "tools/call", {
            "name": "graph_task_assignment_clear", "arguments": {"project": project.name,
               "node_id": node_id, "expected_revision": 1,
               "client": "claude", "session_id": "ana-1"}})
        assert "Unknown tool" not in refused.text
        assert _call(refused)["ok"] is False
        released = await _tool(client, nik, project.name, "graph_task_assignment_clear",
                               node_id=node_id, expected_revision=1,
                               client="codex", session_id="nik-1")
    assert released["state"] == "available" and assignments.mine(project.db, peer) == []
