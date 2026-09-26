"""Mandatory room assignments are fenced graph-task reservations."""

import time

import pytest

from hivemind_server import capabilities, graph_tasks, teams
from hivemind_server.chat import ChatStore
from hivemind_server.db import Conflict, Invalid


OWNER = ("nik", "mac", "codex")
PEER = ("ana", "laptop", "claude")


def _task(db, required=None):
    ChatStore(db).create_room("reviews", "Review work", OWNER)
    teams.add_member(db, "reviews", OWNER, OWNER)
    teams.add_member(db, "reviews", PEER, OWNER)
    return graph_tasks.offer(db, "setup", "reviews", "Audit code", "Inspect the diff",
                             required_capabilities=required)["node_id"]


def test_offline_assignment_reserves_task_without_starting_heartbeat(db):
    from hivemind_server import assignments

    node_id = _task(db, ["review"])
    capabilities.replace(db, OWNER, ["review"])
    assigned = assignments.assign(db, "manager", node_id, OWNER)
    assert assigned["state"] == "assigned_waiting"
    assert graph_tasks.read(db, node_id)["effective_status"] == "unclaimed"
    assert "claim" not in graph_tasks.read(db, node_id)
    with pytest.raises(Conflict, match="assigned"):
        graph_tasks.claim(db, "peer", node_id, PEER)

    claim = graph_tasks.claim(db, "owner", node_id, OWNER)
    assert claim["effective_status"] == "in_progress"
    assert assignments.view(db, node_id)["state"] == "in_progress"
    assert graph_tasks.complete(db, "owner", node_id, claim["claim_token"], OWNER)[
        "effective_status"] == "complete"


def test_ineligible_agent_can_neither_be_assigned_nor_claim(db):
    from hivemind_server import assignments

    node_id = _task(db, ["review", "python"])
    capabilities.replace(db, OWNER, ["review"])
    with pytest.raises(Invalid, match="python"):
        assignments.assign(db, "manager", node_id, OWNER)
    with pytest.raises(Invalid, match="python"):
        graph_tasks.claim(db, "owner", node_id, OWNER)
    assert assignments.view(db, node_id)["state"] == "available"


def test_losing_capability_fences_active_claim_and_releases_waiting_assignment(db):
    from hivemind_server import assignments

    node_id = _task(db, ["review"])
    capabilities.replace(db, OWNER, ["review"])
    assignments.assign(db, "manager", node_id, OWNER)
    capabilities.replace(db, OWNER, [])
    assert assignments.view(db, node_id)["state"] == "available"

    capabilities.replace(db, OWNER, ["review"])
    assignments.assign(db, "manager", node_id, OWNER)
    claim = graph_tasks.claim(db, "owner", node_id, OWNER)
    capabilities.replace(db, OWNER, [])
    assert graph_tasks.read(db, node_id)["effective_status"] == "unclaimed"
    assert assignments.view(db, node_id)["state"] == "available"
    with pytest.raises(Conflict):
        graph_tasks.heartbeat(db, node_id, claim["claim_token"], OWNER)


def test_reassignment_fences_live_holder_and_expiry_releases_reservation(db):
    from hivemind_server import assignments

    node_id = _task(db)
    assignments.assign(db, "manager", node_id, OWNER)
    old = graph_tasks.claim(db, "owner", node_id, OWNER)
    assignments.assign(db, "manager", node_id, PEER)
    with pytest.raises(Conflict):
        graph_tasks.complete(db, "owner", node_id, old["claim_token"], OWNER)
    t = time.time()
    graph_tasks.claim(db, "peer", node_id, PEER, now=t)
    assert assignments.view(db, node_id, now=t + 3600)["state"] == "available"
    graph_tasks.reap_expired(db, now=t + 3600)
    assert assignments.view(db, node_id)["state"] == "available"


def test_member_with_waiting_assignment_cannot_be_silently_removed(db):
    from hivemind_server import assignments

    node_id = _task(db)
    assignments.assign(db, "manager", node_id, OWNER)
    with pytest.raises(Conflict, match="assignment"):
        teams.remove_member(db, "reviews", OWNER, PEER)


def test_new_required_capability_revokes_ineligible_claim(db):
    from hivemind_server import assignments

    node_id = _task(db, ["review"])
    capabilities.replace(db, OWNER, ["review"])
    assignments.assign(db, "manager", node_id, OWNER)
    claim = graph_tasks.claim(db, "owner", node_id, OWNER)
    updated = graph_tasks.set_requirements(db, "manager", node_id, ["python"])
    assert updated["required_capabilities"] == ["python"]
    assert assignments.view(db, node_id)["state"] == "available"
    with pytest.raises(Conflict):
        graph_tasks.complete(db, "owner", node_id, claim["claim_token"], OWNER)
