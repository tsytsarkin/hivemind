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
