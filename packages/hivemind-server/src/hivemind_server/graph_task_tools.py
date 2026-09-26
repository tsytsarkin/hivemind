"""Authenticated MCP lifecycle for optional project graph tasks."""
from __future__ import annotations

from typing import Optional

from . import assignments, graph, graph_tasks
from .chat import ChatStore, StableAddress, _address, stable_identity
from .db import Invalid
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import Identity, current_identity
from .projects_meta import can_access


def attach(mcp, identities) -> None:
    def _context(client: str, session_id: str) -> tuple[object, StableAddress]:
        user, device, actual_client, session = stable_identity(
            current_identity(), client, session_id)
        p = current_project()
        # A failed task action is still genuine agent activity. Do this before any committed
        # claim/status transition so a secondary presence failure cannot lose its claim token.
        ChatStore(p.db).touch((user, device, actual_client), session)
        return p, (user, device, actual_client)

    @mcp.tool(annotations=WRITE, description="Offer an optional graph work_item in an EXISTING project room. Does not create a room or expire with its messages.")
    @_envelope
    def graph_task_offer(room: str, title: str, summary: str, client: str, session_id: str,
                         required_capabilities: Optional[list[str]] = None) -> dict:
        p, _ = _context(client, session_id)
        return graph_tasks.offer(p.db, "graph-task-offer", room, title, summary,
                                 required_capabilities=required_capabilities)

    @mcp.tool(annotations=WRITE, description="Mark an existing graph node as an optional task; optionally link an explicitly created room.")
    @_envelope
    def graph_task_enable(node_id: str, client: str, session_id: str,
                          room: Optional[str] = None,
                          required_capabilities: Optional[list[str]] = None) -> dict:
        p, _ = _context(client, session_id)
        room_id = None
        if room is not None:
            with p.db.read() as cur:
                room_id = ChatStore(p.db)._lookup_room(cur, room)
        return graph_tasks.enable(p.db, "graph-task-enable", node_id, room_id,
                                  required_capabilities=required_capabilities)

    @mcp.tool(annotations=WRITE, description="Optional non-exclusive graph task heartbeat; never changes the node revision or claims a task.")
    @_envelope
    def graph_task_activity(node_id: str, client: str, session_id: str,
                            interval_seconds: int = 300,
                            expires_after_seconds: int = 3600) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.activity(p.db, node_id, who,
                                    interval_seconds=interval_seconds,
                                    expires_after_seconds=expires_after_seconds)

    @mcp.tool(annotations=WRITE, description="Exclusively claim a graph task; defaults: beat every 5 minutes, expires 1 hour after the last accepted heartbeat; expiry maximum 24 hours. Retain returned token privately.")
    @_envelope
    def graph_task_claim(node_id: str, client: str, session_id: str,
                         interval_seconds: int = 300,
                         expires_after_seconds: int = 3600) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.claim(p.db, "graph-task-claim", node_id, who,
                                 interval_seconds=interval_seconds,
                                 expires_after_seconds=expires_after_seconds)

    @mcp.tool(annotations=WRITE, description="As the current room manager, assign an eligible room member a graph task; offline agents discover it when they check in. Reassignment fences the previous claim.")
    @_envelope
    def graph_task_assign(node_id: str, to_user: str, to_device: str, to_client: str,
                          client: str, session_id: str, expected_revision: int,
                          confirm_displace: bool = False) -> dict:
        # confirm_displace is forwarded, and defaults to "not confirmed". It was omitted here
        # while assignments.assign defaults it to True, so the guard that refuses to silently
        # revoke a live, heartbeating claim could never fire for an MCP caller — only the browser
        # path passed it. A manager reassigning now gets a Conflict telling them to confirm.
        p, who = _context(client, session_id)
        target = _address((to_user, to_device, to_client))
        if not identities.has_device(to_user, to_device) or not can_access(
                Identity(to_user, to_device), p.meta):
            raise Invalid("assignee user/device does not exist or lacks project access")
        return assignments.assign(p.db, "graph-task-assign", node_id, target,
                                  expected_revision=expected_revision, manager_actor=who,
                                  confirm_displace=confirm_displace is True)

    @mcp.tool(annotations=WRITE, description="Find your waiting mandatory graph task assignments after reconnecting; does not claim them or start their heartbeat.")
    @_envelope
    def graph_task_my_assignments(client: str, session_id: str) -> dict:
        p, who = _context(client, session_id)
        pending = assignments.mine(p.db, who)
        return {"assignments": pending, "count": len(pending)}

    @mcp.tool(annotations=WRITE, description="As the current room manager, cancel a waiting or active mandatory assignment and fence the old claim.")
    @_envelope
    def graph_task_assignment_clear(node_id: str, expected_revision: int,
                                    client: str, session_id: str,
                                    confirm_displace: bool = False) -> dict:
        p, who = _context(client, session_id)
        return assignments.clear(p.db, "graph-task-assignment-clear", node_id,
                                 "manager_cancelled", expected_revision=expected_revision,
                                 manager_actor=who, confirm_displace=confirm_displace is True)

    @mcp.tool(annotations=WRITE, description="Set a graph task's required self-advertised capability tags; ineligible holders and assignees are immediately fenced.")
    @_envelope
    def graph_task_requirements_set(node_id: str, required_capabilities: list[str],
                                    client: str, session_id: str) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.set_requirements(p.db, "graph-task-requirements", node_id,
                                            required_capabilities, actor=who)

    @mcp.tool(annotations=WRITE, description="Renew the current claimant's expiring claim with its private token; graph node version does not change.")
    @_envelope
    def graph_task_heartbeat(node_id: str, claim_token: str, client: str,
                             session_id: str) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.heartbeat(p.db, node_id, claim_token, who)

    @mcp.tool(annotations=WRITE, description="Release your live claim to make the graph task unclaimed and available again.")
    @_envelope
    def graph_task_release(node_id: str, claim_token: str, client: str,
                           session_id: str) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.release(p.db, "graph-task-release", node_id, claim_token, who)

    @mcp.tool(annotations=WRITE, description="Complete the graph task using your live claim token; the completed graph node persists permanently.")
    @_envelope
    def graph_task_complete(node_id: str, claim_token: str, client: str,
                            session_id: str) -> dict:
        p, who = _context(client, session_id)
        return graph_tasks.complete(p.db, "graph-task-complete", node_id, claim_token, who)

    @mcp.tool(annotations=WRITE, description="Read the graph node and its effective claim/activity; expired claims are available even before a reaper runs. Returns no claim token.")
    @_envelope
    def graph_task_get(node_id: str, client: str, session_id: str) -> dict:
        p, _ = _context(client, session_id)
        return graph.get_node(p.db, node_id=node_id)
