"""Authenticated MCP lifecycle for optional project graph tasks."""
from __future__ import annotations

from typing import Optional

from . import assignments, chat_ws, graph, graph_tasks, task_announcements, task_listing, teams
from .bus_ws_tools import _run
from .chat import ChatStore, StableAddress, _address, stable_identity
from .db import Invalid
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import Identity, current_identity
from .projects_meta import can_access


def attach(mcp, identities) -> None:
    def _notify_room(p, room: str, message: dict, sender: StableAddress) -> int:
        delivered = 0
        try:
            recipients = ChatStore(p.db).subscribers(room)
        except Exception:
            task_announcements.log.exception("task room subscriber lookup failed after persistence")
            recipients = []
        for recipient in recipients:
            try:
                if (recipient == sender or not identities.has_device(recipient[0], recipient[1])
                        or not can_access(Identity(recipient[0], recipient[1]), p.meta)):
                    continue
                frame = {"v": 2, "type": "chat", "id": message["id"], "channel": "room",
                         "room": room, "from": "-".join(sender),
                         "preview": message["body"].encode("utf-8")[:160].decode("utf-8", "ignore"),
                         "ts": message["created_at"]}
                delivered += _run(chat_ws.hub_for(p.dir).notify(recipient, frame))
            except Exception:
                # Room history is durable and available on reconnect; one dead socket cannot
                # prevent other subscribers from receiving the new-task signal.
                task_announcements.log.exception("room task push failed for %r", recipient)
        return delivered

    def _announce(p, room: str, offered: dict, sender: StableAddress) -> dict:
        message = task_announcements.announce_offer(p.db, p.dir, room, offered, sender)
        if message is None:
            return {**offered, "room_notification": "could not persist; task remains available"}
        return {**offered, "room_notification": "stored",
                "notified_count": _notify_room(p, room, message, sender)}

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
        p, who = _context(client, session_id)
        offered = graph_tasks.offer(p.db, "graph-task-offer", room, title, summary,
                                    required_capabilities=required_capabilities)
        return _announce(p, room, offered, who)

    @mcp.tool(annotations=WRITE, description="Mark an existing graph node as an optional task; optionally link an explicitly created room.")
    @_envelope
    def graph_task_enable(node_id: str, client: str, session_id: str,
                          room: Optional[str] = None,
                          required_capabilities: Optional[list[str]] = None) -> dict:
        p, who = _context(client, session_id)
        room_id = None
        if room is not None:
            with p.db.read() as cur:
                room_id = ChatStore(p.db)._lookup_room(cur, room)
        enabled = graph_tasks.enable(p.db, "graph-task-enable", node_id, room_id,
                                     required_capabilities=required_capabilities)
        if room is None:
            return enabled
        announcement = _announce(p, room, graph.get_node(p.db, node_id=node_id), who)
        return {**enabled, "room_notification": announcement["room_notification"],
                "notified_count": announcement.get("notified_count", 0)}

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

    @mcp.tool(annotations=WRITE, description="Page available, unreserved graph tasks whose required capability tags you advertise; an atomic claim is still required before starting work.")
    @_envelope
    def graph_task_available(client: str, session_id: str, limit: int = 25,
                             before_id: Optional[str] = None) -> dict:
        p, who = _context(client, session_id)
        return task_listing.page(p.db, status="available", before_id=before_id,
                                 limit=limit, eligible_for=who)

    @mcp.tool(annotations=WRITE, description="As current manager, page all tasks in your room with project-wide-for-room status totals, current assignees and claim progress. No claim token is returned.")
    @_envelope
    def graph_task_room_status(room: str, client: str, session_id: str,
                               limit: int = 25, before_id: Optional[str] = None) -> dict:
        p, who = _context(client, session_id)
        if teams.manager(p.db, room)["manager"] != who:
            raise Invalid("only the current room manager can inspect room task status")
        result = task_listing.page(p.db, room=room, limit=limit, before_id=before_id,
                                   with_counts=True)
        for task in result["tasks"]:
            state = assignments.view(p.db, task["node_id"])
            task["assignee"] = state["assignee"]
            task["revision"] = state["revision"]
            task["claim"] = graph_tasks.read(p.db, task["node_id"]).get("claim")
        return result

    @mcp.tool(annotations=WRITE, description="As the current room manager, cancel a waiting or active mandatory assignment and fence the old claim.")
    @_envelope
    def graph_task_assignment_clear(node_id: str, expected_revision: int,
                                    client: str, session_id: str,
                                    confirm_displace: bool = False) -> dict:
        p, who = _context(client, session_id)
        return assignments.clear(p.db, "graph-task-assignment-clear", node_id,
                                 "manager_cancelled", expected_revision=expected_revision,
                                 manager_actor=who, confirm_displace=confirm_displace is True)

    @mcp.tool(annotations=WRITE, description="Set a graph task's required project-approved capability tags; ineligible holders and assignees are immediately fenced.")
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
        completed = graph_tasks.complete(p.db, "graph-task-complete", node_id, claim_token, who)
        if completed["room_id"] is None:
            return completed
        try:
            with p.db.read() as cur:
                row = cur.execute("SELECT name FROM chat_room WHERE room_id=?",
                                  (completed["room_id"],)).fetchone()
            room = row["name"] if row else None
            if room is None:
                return {**completed, "room_notification": "room no longer exists"}
            message = task_announcements.announce_complete(p.db, p.dir, room, node_id, who)
            if message is None:
                return {**completed, "room_notification": "could not persist; task remains complete"}
            return {**completed, "room_notification": "stored",
                    "notified_count": _notify_room(p, room, message, who)}
        except Exception:
            task_announcements.log.exception("task %s completed but announcement lookup failed", node_id)
            return {**completed, "room_notification": "could not persist; task remains complete"}

    @mcp.tool(annotations=WRITE, description="Read the graph node and its effective claim/activity; expired claims are available even before a reaper runs. Returns no claim token.")
    @_envelope
    def graph_task_get(node_id: str, client: str, session_id: str) -> dict:
        p, _ = _context(client, session_id)
        return graph.get_node(p.db, node_id=node_id)
