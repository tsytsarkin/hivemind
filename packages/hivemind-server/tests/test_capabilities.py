"""Project-scoped, self-advertised capability registry."""

import pytest
import httpx

from conftest import Lifespan, _call, _post
from hivemind_server.db import Conflict, Database, Invalid
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


def test_catalog_definitions_are_project_global_persistent_and_pageable(db, tmp_path):
    from hivemind_server import capabilities
    capabilities.define(db, "review", "Review code and reason about risks")
    capabilities.define(db, "python", "Implement and test Python")
    capabilities.define(db, "review", "Assess patch quality and risks")
    first = capabilities.catalog(db, limit=1)
    second = capabilities.catalog(db, after=first["next_cursor"], limit=1)
    assert first["capabilities"][0]["name"] == "python"
    assert second["capabilities"][0]["description"] == "Assess patch quality and risks"
    assert capabilities.catalog(Database(db.path))["capabilities"] == \
           first["capabilities"] + second["capabilities"]
    assert capabilities.catalog(Database(tmp_path / "other.db"))["capabilities"] == []


def test_legacy_agent_tags_are_registered_without_losing_advertisements(db):
    from hivemind_server import capabilities
    capabilities.replace(db, OWNER, ["swift"])
    assert capabilities.catalog(db)["capabilities"][0]["name"] == "swift"
    assert capabilities.catalog(db)["capabilities"][0]["description"] == ""
    capabilities.define(db, "swift", "Build Swift apps")
    assert capabilities.get(db, OWNER)["capabilities"] == ["swift"]


def test_human_managed_tags_cannot_be_silently_overwritten_by_stale_agent(db):
    from hivemind_server import capabilities
    capabilities.replace(db, OWNER, ["review"])
    human = capabilities.replace(db, OWNER, ["python"], managed_by_ui=True)
    assert human["human_managed"] is True
    with pytest.raises(Conflict, match="read.*expected_updated_at"):
        capabilities.replace(db, OWNER, ["review"])
    assert capabilities.get(db, OWNER)["capabilities"] == ["python"]
    refreshed = capabilities.replace(db, OWNER, ["review"],
                                     expected_updated_at=human["updated_at"])
    assert refreshed["capabilities"] == ["review"]
    assert refreshed["human_managed"] is True
    with pytest.raises(Conflict):
        capabilities.replace(db, OWNER, ["python"],
                             expected_updated_at=human["updated_at"])


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


@pytest.mark.anyio
async def test_mcp_can_page_server_defined_capabilities_and_descriptions(env):
    from hivemind_server import capabilities
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "macbook")
    capabilities.define(project.db, "python", "Implement Python changes")
    capabilities.define(project.db, "review", "Review code for risks")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        first = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_capability_catalog",
            "arguments": {"project": project.name, "client": "codex", "session_id": "one",
                          "limit": 1}}))
        second = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_capability_catalog",
            "arguments": {"project": project.name, "client": "codex", "session_id": "one",
                          "limit": 1, "after": first["next_cursor"]}}))
    assert [c["name"] for c in first["capabilities"] + second["capabilities"]] == ["python", "review"]
    assert second["capabilities"][0]["description"] == "Review code for risks"


@pytest.mark.anyio
async def test_mcp_agent_must_refresh_before_overriding_portal_capability_edit(env):
    from hivemind_server import capabilities
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "macbook")
    capabilities.replace(project.db, OWNER, ["review"], managed_by_ui=True)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        stale = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_capabilities_set", "arguments": {"project": project.name,
              "client": "codex", "session_id": "one", "capabilities": ["python"]}}))
        current = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_capabilities_get", "arguments": {"project": project.name,
              "user": "nik", "device": "macbook", "agent_client": "codex",
              "client": "codex", "session_id": "one"}}))
        updated = _call(await _post(client, "", token, "tools/call", {
            "name": "agent_capabilities_set", "arguments": {"project": project.name,
              "client": "codex", "session_id": "one", "capabilities": ["python"],
              "expected_updated_at": current["updated_at"]}}))
    assert stale["ok"] is False and "expected_updated_at" in stale["error"]
    assert current["human_managed"] is True and current["capabilities"] == ["review"]
    assert updated["capabilities"] == ["python"]
