"""Project-scoped, self-advertised capability registry."""

import pytest
import httpx

from conftest import Lifespan, _call, _post
from hivemind_server.db import Conflict, Database, Invalid
from hivemind_server.identity import IdentityStore


OWNER = ("nik", "macbook", "codex")
PEER = ("nik", "macbook", "claude")


def test_legacy_advertisements_persist_without_granting_eligibility(db, tmp_path):
    from hivemind_server import capabilities

    first = capabilities.replace(db, OWNER, ["review", "python", "review"])
    assert first["capabilities"] == []
    assert first["pending_capabilities"] == ["python", "review"]
    assert capabilities.get(Database(db.path), OWNER)["pending_capabilities"] == ["python", "review"]
    assert capabilities.get(db, PEER)["capabilities"] == []
    assert capabilities.get(Database(tmp_path / "other.db"), OWNER)["capabilities"] == []

    capabilities.replace(db, OWNER, ["review"])
    assert capabilities.get(db, OWNER)["pending_capabilities"] == ["review"]


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


def test_retired_capability_removes_grants_and_cannot_resurrect_on_restart(db):
    from hivemind_server import capabilities

    created = capabilities.define(db, "review", "Review source code")
    capabilities.replace(db, OWNER, ["review"], managed_by_ui=True)
    capabilities.replace(db, PEER, ["review", "swift"])
    retired = capabilities.retire(db, "review", expected_updated_at=created["updated_at"])
    assert retired["affected_agents"] == [PEER, OWNER]
    assert capabilities.get(db, OWNER)["capabilities"] == []
    assert capabilities.get(db, PEER)["pending_capabilities"] == ["swift"]
    assert [c["name"] for c in capabilities.catalog(db)["capabilities"]] == ["swift"]
    reopened = Database(db.path)
    capabilities.replace(reopened, PEER, ["review"])
    assert "review" not in [c["name"] for c in capabilities.catalog(Database(db.path))["capabilities"]]
    restored = capabilities.define(reopened, "review", "Review source code",
                                    expected_updated_at=None, enforce_revision=True)
    assert restored["approved"] is True
    assert capabilities.get(reopened, PEER)["capabilities"] == []


def test_retiring_capability_blocks_open_tasks_and_preserves_completed_task_history(db):
    from hivemind_server import capabilities, graph_tasks
    from hivemind_server.chat import ChatStore

    created = capabilities.define(db, "review", "Review patches")
    capabilities.replace(db, OWNER, ["review"], managed_by_ui=True)
    ChatStore(db).create_room("reviews", "Review patches", OWNER)
    task = graph_tasks.offer(db, "nik", "reviews", "Review patch", "Verify correctness",
                             required_capabilities=["review"])
    with pytest.raises(Conflict, match="open task"):
        capabilities.retire(db, "review", expected_updated_at=created["updated_at"])
    assert capabilities.get(db, OWNER)["capabilities"] == ["review"]
    claim = graph_tasks.claim(db, "nik", task["node_id"], OWNER)
    graph_tasks.complete(db, "nik", task["node_id"], claim["claim_token"], OWNER)
    capabilities.retire(db, "review", expected_updated_at=created["updated_at"])
    assert graph_tasks.read(db, task["node_id"])["required_capabilities"] == ["review"]


def test_retiring_capability_rejects_stale_revision_without_changing_definition(db):
    from hivemind_server import capabilities

    initial = capabilities.define(db, "review", "Review patches")
    capabilities.define(db, "review", "Review patches and tests")
    with pytest.raises(Conflict, match="refresh"):
        capabilities.retire(db, "review", expected_updated_at=initial["updated_at"])
    assert capabilities.catalog(db)["capabilities"][0]["approved"] is True


def test_project_user_catalog_update_rejects_stale_revision(db):
    from hivemind_server import capabilities

    first = capabilities.define(db, "review", "Review code")
    current = capabilities.define(db, "review", "Review code and tests",
                                  expected_updated_at=first["updated_at"],
                                  enforce_revision=True)
    assert current["description"] == "Review code and tests"
    with pytest.raises(Conflict, match="capability.*changed"):
        capabilities.define(db, "review", "Replace without seeing recent edit",
                            expected_updated_at=first["updated_at"],
                            enforce_revision=True)
    assert capabilities.catalog(db)["capabilities"][0]["description"] == "Review code and tests"


def test_legacy_agent_tags_are_registered_without_losing_advertisements(db):
    from hivemind_server import capabilities
    capabilities.replace(db, OWNER, ["swift"])
    assert capabilities.catalog(db)["capabilities"][0]["name"] == "swift"
    assert capabilities.catalog(db)["capabilities"][0]["description"] == ""
    capabilities.define(db, "swift", "Build Swift apps")
    assert capabilities.get(db, OWNER)["pending_capabilities"] == ["swift"]
    assert capabilities.get(db, OWNER)["capabilities"] == []


def test_legacy_tag_definition_and_agent_grant_require_separate_approval(db):
    from hivemind_server import capabilities

    with db.write_light() as cur:
        cur.execute("INSERT INTO project_capability(name,description,created_at,updated_at) "
                    "VALUES('swift','',0,0)")
        cur.execute("INSERT INTO agent_capability(user,device,client,tags_json,updated_at,"
                    "human_managed) VALUES(?,?,?,?,?,?)", (*OWNER, '["swift"]', 0, 0))

    assert capabilities.catalog(db)["capabilities"][0]["approved"] is False
    assert capabilities.get(db, OWNER)["pending_capabilities"] == ["swift"]
    assert capabilities.get(db, OWNER)["capabilities"] == []

    capabilities.define(db, "swift", "Build and review Swift apps")
    assert capabilities.catalog(db)["capabilities"][0]["approved"] is True
    assert capabilities.get(db, OWNER)["capabilities"] == []

    capabilities.replace(db, OWNER, ["swift"], managed_by_ui=True)
    assert capabilities.get(db, OWNER)["pending_capabilities"] == []
    assert capabilities.get(db, OWNER)["capabilities"] == ["swift"]


def test_human_managed_tags_cannot_be_silently_overwritten_by_stale_agent(db):
    from hivemind_server import capabilities
    capabilities.define(db, "python", "Implement Python tasks")
    capabilities.replace(db, OWNER, ["review"])
    human = capabilities.replace(db, OWNER, ["python"], managed_by_ui=True)
    assert human["human_managed"] is True
    with pytest.raises(Conflict, match="read.*expected_updated_at"):
        capabilities.replace(db, OWNER, ["review"])
    assert capabilities.get(db, OWNER)["capabilities"] == ["python"]
    refreshed = capabilities.replace(db, OWNER, ["review"],
                                     expected_updated_at=human["updated_at"])
    assert refreshed["capabilities"] == []
    assert refreshed["pending_capabilities"] == ["review"]
    assert refreshed["human_managed"] is True
    with pytest.raises(Conflict):
        capabilities.replace(db, OWNER, ["python"],
                             expected_updated_at=human["updated_at"])


def test_invalid_capability_tag_does_not_change_existing_advertisement(db):
    from hivemind_server import capabilities

    capabilities.replace(db, OWNER, ["python"])
    with pytest.raises(Invalid):
        capabilities.replace(db, OWNER, ["not valid"])
    assert capabilities.get(db, OWNER)["pending_capabilities"] == ["python"]


def test_all_required_tags_must_be_approved_for_the_agent(db):
    from hivemind_server import capabilities

    capabilities.define(db, "python", "Implement Python tasks")
    capabilities.define(db, "review", "Review code")
    capabilities.replace(db, OWNER, ["python", "review"], managed_by_ui=True)
    capabilities.require_tags(["review"], capabilities.get(db, OWNER)["capabilities"])
    with pytest.raises(Invalid, match="missing required capabilities: shell"):
        capabilities.require_tags(["review", "shell"],
                                  capabilities.get(db, OWNER)["capabilities"])


@pytest.mark.anyio
async def test_mcp_agent_cannot_create_or_assign_project_capabilities(env):
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
    assert result["ok"] is False
    assert "project user" in result["error"]
    from hivemind_server import capabilities
    assert capabilities.get(project.db, ("nik", "macbook", "codex"))["capabilities"] == []
    assert capabilities.catalog(project.db)["capabilities"] == []


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
async def test_mcp_agent_cannot_override_portal_capability_edit_even_with_current_revision(env):
    from hivemind_server import capabilities
    app, project, _ = env
    token = IdentityStore(app.state.cfg.identities_path).mint("nik", "macbook")
    capabilities.define(project.db, "review", "Review source")
    capabilities.define(project.db, "python", "Implement Python")
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
    assert stale["ok"] is False and "project user" in stale["error"]
    assert current["human_managed"] is True and current["capabilities"] == ["review"]
    assert updated["ok"] is False and "project user" in updated["error"]
    assert capabilities.get(project.db, OWNER)["capabilities"] == ["review"]


def _pre_upgrade_row(path, address, tags, human_managed, approved_json="[]"):
    """Write a 1.5.1-shaped agent_capability row straight into an already-built database."""
    import json as _json
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO agent_capability(user,device,client,tags_json,"
                "updated_at,human_managed,approved_tags_json) VALUES(?,?,?,?,?,?,?)",
                (*address, _json.dumps(tags), 1.0, human_managed, approved_json))
    con.execute("DELETE FROM meta WHERE key='capability_approval_migrated_v1'")
    con.commit()
    con.close()


def test_upgrade_does_not_approve_tags_an_agent_declared_on_a_human_managed_row(db, tmp_path):
    """human_managed is a ROW flag, and 1.5.1 let an AGENT overwrite such a row whenever it
    passed the current expected_updated_at — the guard only fired when that was None, and the
    flag survived. Backfilling approved_tags_json from tags_json therefore blessed self-declared
    tags on upgrade, which is exactly the state 1.5.2 exists to remove. Only a tag with its own
    evidence survives: a curated definition, or a task this agent is actually committed to.
    """
    import json as _json
    import sqlite3
    from hivemind_server import capabilities

    path = db.path
    # 'review' is curated by a human (non-empty description); 'admin' is a bare name the agent
    # slipped onto the same row.
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO project_capability(name,description,created_at,"
                "updated_at,approved) VALUES('review','Reviews patches',0,0,0)")
    con.execute("INSERT OR REPLACE INTO project_capability(name,description,created_at,"
                "updated_at,approved) VALUES('admin','',0,0,0)")
    con.commit()
    con.close()
    _pre_upgrade_row(path, OWNER, ["review", "admin"], human_managed=1)

    Database(path)                                   # re-open: runs the approval migration
    granted = capabilities.get(Database(path), OWNER)
    assert granted["capabilities"] == [], granted
    assert sorted(granted["pending_capabilities"]) == ["admin", "review"], granted


def test_upgrade_keeps_an_agent_able_to_finish_work_it_already_holds(db, tmp_path):
    """The conservative rule must not strand in-flight work: DEPLOY.md promises a live claim can
    still heartbeat and complete, and a queued assignment must not become unclaimable forever."""
    import sqlite3
    from hivemind_server import capabilities, graph, graph_tasks

    capabilities.define(db, "deploy", "Ships releases")
    nid = graph.upsert_node(db, "nik", "finding", {"title": "audit"})["node_id"]
    graph_tasks.enable(db, "nik", nid, required_capabilities=["deploy"])
    capabilities.replace(db, OWNER, ["deploy"])
    con = sqlite3.connect(db.path)
    con.execute("UPDATE agent_capability SET approved_tags_json=tags_json")
    con.commit()
    con.close()
    graph_tasks.claim(db, "nik", nid, OWNER, now=1000)

    # Rewind to what a 1.5.1 database looks like: no approvals recorded anywhere, and the
    # definition carrying the empty description the backfill gives it.
    con = sqlite3.connect(db.path)
    con.execute("UPDATE project_capability SET approved=0, description=''")
    con.commit()
    con.close()
    _pre_upgrade_row(db.path, OWNER, ["deploy"], human_managed=0)
    Database(db.path)                                # upgrade again with the stricter rule

    held = capabilities.get(Database(db.path), OWNER)
    assert held["capabilities"] == ["deploy"], \
        "a tag the agent's own live claim depends on must stay approved"
    with Database(db.path).read() as cur:
        graph_tasks.eligible(cur, nid, OWNER)        # must not raise
