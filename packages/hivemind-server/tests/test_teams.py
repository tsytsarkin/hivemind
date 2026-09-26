"""Room managers are project-local stable agents, not display labels."""

import httpx
import pytest

from conftest import Lifespan, _call, _post
from hivemind_server.chat import ChatStore
from hivemind_server.db import Conflict, Invalid
from hivemind_server.identity import IdentityStore


OWNER = ("nik", "mac", "codex")
PEER = ("ana", "laptop", "claude")


def test_room_manager_handoff_is_atomic_and_keeps_memberships(db):
    from hivemind_server import teams

    ChatStore(db).create_room("reviews", "Review work", OWNER)
    teams.add_member(db, "reviews", OWNER, OWNER)
    teams.add_member(db, "reviews", PEER, OWNER)
    first = teams.promote(db, "reviews", OWNER, OWNER, expected_revision=0)
    assert first["manager"] == OWNER
    assert first["revision"] == 1

    second = teams.promote(db, "reviews", PEER, PEER, expected_revision=1)
    assert second["previous_manager"] == OWNER
    assert teams.manager(db, "reviews")["manager"] == PEER
    assert set(ChatStore(db).subscribers("reviews")) == {OWNER, PEER}
    with pytest.raises(Conflict):
        teams.promote(db, "reviews", OWNER, OWNER, expected_revision=1)


def test_only_room_members_may_promote_and_removing_manager_vacates_role(db):
    from hivemind_server import teams

    ChatStore(db).create_room("reviews", "Review work", OWNER)
    teams.add_member(db, "reviews", OWNER, OWNER)
    with pytest.raises(Invalid, match="member"):
        teams.promote(db, "reviews", PEER, PEER, expected_revision=0)
    teams.promote(db, "reviews", OWNER, OWNER, expected_revision=0)
    teams.remove_member(db, "reviews", OWNER, OWNER)
    assert teams.manager(db, "reviews")["manager"] is None


def test_leaving_is_blocked_while_holding_any_live_room_claim(db):
    from hivemind_server import graph_tasks, teams

    ChatStore(db).create_room("reviews", "Review work", OWNER)
    teams.add_member(db, "reviews", OWNER, OWNER)
    node_id = graph_tasks.offer(db, "setup", "reviews", "Review fix", "Check diff")["node_id"]
    graph_tasks.claim(db, "setup", node_id, OWNER)
    with pytest.raises(Conflict, match="assignment|claim"):
        teams.remove_member(db, "reviews", OWNER, OWNER)
    assert OWNER in ChatStore(db).subscribers("reviews")


@pytest.mark.anyio
async def test_mcp_promotes_only_authenticated_room_member(env):
    app, project, _ = env
    nik = IdentityStore(app.state.cfg.identities_path).mint("nik", "mac")
    ana = IdentityStore(app.state.cfg.identities_path).mint("ana", "laptop")
    from hivemind_server import teams

    ChatStore(project.db).create_room("reviews", "Review work", OWNER)
    teams.add_member(project.db, "reviews", OWNER, OWNER)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        denied_response = await _post(client, "", ana, "tools/call", {
            "name": "team_manager_self_promote",
            "arguments": {"project": project.name, "room": "reviews", "client": "codex",
                          "session_id": "sid-ana", "expected_revision": 0}})
        assert "Unknown tool" not in denied_response.text
        denied = _call(denied_response)
        assert denied["ok"] is False
        promoted = _call(await _post(client, "", nik, "tools/call", {
            "name": "team_manager_self_promote",
            "arguments": {"project": project.name, "room": "reviews", "client": "codex",
                          "session_id": "sid-nik", "expected_revision": 0}}))
    assert promoted["ok"] is True
    assert teams.manager(project.db, "reviews")["manager"] == OWNER


@pytest.mark.anyio
async def test_mcp_exposes_room_manager_revision_and_member_addresses(env):
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "mac")
    from hivemind_server import teams

    ChatStore(project.db).create_room("reviews", "Review work", OWNER)
    teams.add_member(project.db, "reviews", OWNER, OWNER)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", token, "tools/call", {
            "name": "team_room_get",
            "arguments": {"project": project.name, "room": "reviews", "client": "codex",
                          "session_id": "sid-nik"}})
        assert "Unknown tool" not in response.text
        result = _call(response)
    assert result["manager"] is None and result["revision"] == 0
    assert result["members"] == [list(OWNER)]


@pytest.mark.anyio
async def test_mcp_adds_known_project_member_to_explicit_room(env):
    app, project, _ = env
    identities = IdentityStore(app.state.cfg.identities_path)
    nik = identities.mint("nik", "mac")
    identities.mint("ana", "laptop")
    ChatStore(project.db).create_room("reviews", "Review work", OWNER)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", nik, "tools/call", {
            "name": "team_room_member_add", "arguments": {"project": project.name,
                "room": "reviews", "to_user": "ana", "to_device": "laptop", "to_client": "claude",
                "client": "codex", "session_id": "sid-nik"}})
        assert "Unknown tool" not in response.text
        assert _call(response)["ok"] is True
    assert ChatStore(project.db).subscribers("reviews") == [PEER]


@pytest.mark.anyio
async def test_self_leave_cannot_bypass_live_assignment_or_abandon_manager(env):
    from hivemind_server import assignments, graph_tasks, teams
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "mac")
    ChatStore(project.db).create_room("reviews", "Review work", OWNER)
    teams.add_member(project.db, "reviews", OWNER, OWNER)
    teams.promote(project.db, "reviews", OWNER, OWNER, expected_revision=0)
    task_id = graph_tasks.offer(project.db, "setup", "reviews", "Review fix", "Check diff")["node_id"]
    assignments.assign(project.db, "setup", task_id, OWNER)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", token, "tools/call", {
            "name": "chat_room_leave", "arguments": {"project": project.name,
                "name": "reviews", "client": "codex", "session_id": "sid-nik"}})
        assert _call(response)["ok"] is False
    assert teams.manager(project.db, "reviews")["manager"] == OWNER
    assert OWNER in ChatStore(project.db).subscribers("reviews")
