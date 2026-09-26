"""Project-scoped, self-advertised capability registry."""

import pytest
import httpx

from conftest import Lifespan, _call, _post
from hivemind_server.db import Database, Invalid
from hivemind_server.identity import IdentityStore


OWNER = ("nik", "macbook", "codex")
PEER = ("nik", "macbook", "claude")


def test_replacing_capabilities_persists_for_one_stable_agent(db, tmp_path):
    from hivemind_server import capabilities

    first = capabilities.replace(db, OWNER, ["review", "python", "review"])
    assert first["capabilities"] == ["python", "review"]
    assert capabilities.get(Database(db.path), OWNER)["capabilities"] == ["python", "review"]
    assert capabilities.get(db, PEER)["capabilities"] == []
    assert capabilities.get(Database(tmp_path / "other.db"), OWNER)["capabilities"] == []

    capabilities.replace(db, OWNER, ["review"])
    assert capabilities.get(db, OWNER)["capabilities"] == ["review"]


def test_invalid_capability_tag_does_not_change_existing_advertisement(db):
    from hivemind_server import capabilities

    capabilities.replace(db, OWNER, ["python"])
    with pytest.raises(Invalid):
        capabilities.replace(db, OWNER, ["not valid"])
    assert capabilities.get(db, OWNER)["capabilities"] == ["python"]


def test_all_required_tags_must_be_advertised(db):
    from hivemind_server import capabilities

    capabilities.replace(db, OWNER, ["python", "review"])
    capabilities.require_tags(["review"], capabilities.get(db, OWNER)["capabilities"])
    with pytest.raises(Invalid, match="missing required capabilities: shell"):
        capabilities.require_tags(["review", "shell"],
                                  capabilities.get(db, OWNER)["capabilities"])


@pytest.mark.anyio
async def test_mcp_advertisement_is_bound_to_authenticated_user_and_device(env):
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "macbook")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        response = await _post(client, "", token, "tools/call", {
            "name": "agent_capabilities_set",
            "arguments": {"project": project.name, "client": "codex", "session_id": "one",
                          "capabilities": ["python", "review"]},
        })
        result = _call(response)
    assert result["ok"] is True
    from hivemind_server import capabilities
    assert capabilities.get(project.db, ("nik", "macbook", "codex"))["capabilities"] == [
        "python", "review"]
