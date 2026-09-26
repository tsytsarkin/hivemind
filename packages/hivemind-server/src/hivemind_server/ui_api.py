"""Project-scoped browser endpoints over the same coordination services as MCP."""
from __future__ import annotations

import json
import logging
import time

from starlette.responses import JSONResponse

from . import agent_config, assignments, capabilities, graph_tasks, instructions, task_announcements, task_listing, teams, ui_agents, ui_capabilities, ui_chat, ui_rooms
from . import chat_ws
from .chat import ChatStore, _address
from .db import Conflict, Invalid, NotFound
from .identity import Identity, IdentityStore
from .projects_meta import can_access
from .ui_payload import TooLarge, read_json

log = logging.getLogger(__name__)


def _answer(value, status=200):
    return JSONResponse(value, status_code=status, headers={"cache-control": "no-store"})


def _recipient(raw, identities, project):
    who = _address(raw)
    if not identities.has_device(who[0], who[1]) or not can_access(
            Identity(who[0], who[1]), project.meta):
        raise Invalid("agent user/device does not exist or lacks project access")
    return who


def _page(req):
    try:
        limit = int(req.query_params.get("limit", "100"))
    except ValueError as exc:
        raise Invalid("limit must be an integer") from exc
    if not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    return limit


async def handle(req, p, who, identities: IdentityStore):
    """Dispatch only after ui_app validates cookie, CSRF and per-project ACL."""
    path = req.path_params["tail"].strip("/").split("/")
    method = req.method
    db = p.db
    actor = (who.user, who.device, "webui")
    try:
        data = await read_json(req) if method == "POST" else {}
        if not isinstance(data, dict):
            raise Invalid("body must be a JSON object")
        if method == "POST":
            log.info("console project mutation project=%r user=%r action=%r target=%r",
                     p.name, who.user, path[0], path[1] if len(path) > 1 else None)
        store = ChatStore.for_project(db, p.dir)
        if method == "GET":
            if path == ["capabilities"]:
                return _answer(capabilities.catalog(db, after=req.query_params.get("after"),
                                                    limit=_page(req)))
            if path == ["agents", "capabilities"]:
                address = _recipient([req.query_params.get(part) for part in
                                      ("user", "device", "client")], identities, p)
                return _answer(capabilities.get(db, address))
            if path == ["agents", "config"]:
                address = _recipient([req.query_params.get(part) for part in
                                      ("user", "device", "client")], identities, p)
                return _answer(agent_config.get(db, address))
            if path == ["summary"]:
                now = time.time()
                hub = chat_ws.hub_for(p.dir)
                online = hub.online_addresses()
                with db.read() as cur:
                    waiting = cur.execute(
                        "SELECT COUNT(*) AS n FROM graph_task_assignment a "
                        "JOIN graph_task t ON t.node_id=a.node_id "
                        "LEFT JOIN graph_task_claim c ON c.node_id=a.node_id "
                        "WHERE a.assignee_user IS NOT NULL AND t.status='unclaimed' "
                        "AND (c.token_digest IS NULL OR c.node_id IS NULL)").fetchone()["n"]
                    overdue = cur.execute(
                        "SELECT COUNT(*) AS n FROM graph_task_claim c JOIN graph_task t "
                        "ON t.node_id=c.node_id WHERE c.token_digest IS NOT NULL AND "
                        "c.last_beat_at+c.expires_after_seconds>? AND "
                        "(c.last_beat_at+c.interval_seconds<=? OR "
                        "(t.room_id IS NOT NULL AND "
                        "COALESCE((SELECT MAX(m.created_at) FROM chat_message m "
                        "WHERE m.room_id=t.room_id AND m.task_node_id=c.node_id AND "
                        "m.sender_user=c.holder_user AND m.sender_device=c.holder_device "
                        "AND m.sender_client=c.holder_client AND m.message_kind='progress' "
                        "AND m.created_at>=c.claimed_at),c.claimed_at)+900<=?))",
                        (now, now, now)).fetchone()["n"]
                    queued = cur.execute("SELECT COUNT(*) AS n FROM agent_instruction "
                                         "WHERE state='queued'").fetchone()["n"]
                    stalled = cur.execute(
                        "SELECT COUNT(*) AS n FROM agent_instruction i WHERE i.state IN "
                        "('queued','acknowledged','in_progress') AND i.updated_at<=? "
                        "AND NOT EXISTS (SELECT 1 FROM chat_session s WHERE s.user=i.recipient_user "
                        "AND s.device=i.recipient_device AND s.client=i.recipient_client "
                        "AND s.last_activity_at>?)",
                        (now - 86400, now - 86400)).fetchone()["n"]
                return _answer({"online_agents": len(online), "waiting_assignments": waiting,
                                "overdue_updates": overdue, "queued_instructions": queued,
                                "stalled_instructions": stalled})
            if path == ["rooms"]:
                return _answer(ui_rooms.room_page(db,
                    after=req.query_params.get("after"),
                    limit=int(req.query_params.get("limit", "25"))))
            if len(path) == 3 and path[0] == "rooms" and path[2] == "members":
                return _answer(ui_rooms.member_page(db, path[1],
                    after=req.query_params.get("after"), limit=_page(req)))
            if len(path) == 3 and path[0] == "rooms" and path[2] == "manager":
                return _answer(teams.manager(db, path[1]))
            if path == ["agents"]:
                return _answer(ui_agents.page(db, chat_ws.hub_for(p.dir),
                    after=req.query_params.get("after"), limit=_page(req)))
            if len(path) == 3 and path[0] == "tasks" and path[2] == "candidates":
                with db.read() as cur:
                    task = graph_tasks._marker(cur, path[1])
                    room = (cur.execute("SELECT name FROM chat_room WHERE room_id=?",
                                        (task["room_id"],)).fetchone()
                            if task["room_id"] else None)
                if room is None:
                    raise Invalid("task requires a room before assignment")
                page = ui_rooms.member_page(db, room["name"],
                    after=req.query_params.get("after"), limit=_page(req))
                required = json.loads(task["required_capabilities_json"])
                candidates = []
                for address in page["members"]:
                    advertised = capabilities.get(db, address)["capabilities"]
                    candidates.append({"address": address,
                                       "missing": sorted(set(required) - set(advertised))})
                return _answer({"candidates": candidates,
                                "older_cursor": page["members_older_cursor"],
                                "member_count": page["member_count"]})
            if path == ["tasks"]:
                limit = _page(req)
                page = task_listing.page(db, status=req.query_params.get("status", "all"),
                    before_id=req.query_params.get("before_id"), limit=limit, with_counts=True)
                page["tasks"] = [{**assignments.view(db, item["node_id"]),
                                  **graph_tasks.read(db, item["node_id"]), **item}
                                 for item in page["tasks"]]
                return _answer(page)
            if path == ["instructions"]:
                return _answer(instructions.list_project(db,
                    before_id=req.query_params.get("before_id"), limit=_page(req)))
            if path == ["messages"]:
                if req.query_params.get("channel") == "dm":
                    log.info("console DM transcript read project=%r user=%r", p.name, who.user)
                return _answer(ui_chat.list_transcript(db,
                    channel=req.query_params.get("channel", "room"),
                    room=req.query_params.get("room"),
                    after_seq=int(req.query_params.get("after_seq", "0")),
                    before_seq=(int(req.query_params["before_seq"])
                                if "before_seq" in req.query_params else None),
                    limit=_page(req),
                    reader=actor))
        if method == "POST":
            if path == ["messages", "mark-read"]:
                return _answer(ui_chat.mark_console_read(db, actor,
                    channel=data.get("channel"), room=data.get("room"),
                    seq=data.get("up_to_seq")))
            if path == ["messages", "room"]:
                room = data.get("room")
                sent = ui_chat.send_human_room(db, who, room, data.get("body"),
                                               data.get("idempotency_key"))
                delivered = await ui_chat.notify_human_room(p.dir, db, p.meta, identities,
                                                             room, sent)
                return _answer({**sent, "notified_live": delivered > 0,
                                "notified_count": delivered}, 201)
            if path == ["agents", "capabilities"]:
                target = _recipient(data.get("address"), identities, p)
                if "expected_updated_at" not in data:
                    raise Invalid("expected_updated_at is required; refresh agent capabilities")
                return _answer(await ui_capabilities.replace_and_notify(
                    p, identities, who, target, data.get("capabilities"),
                    data["expected_updated_at"]))
            if path == ["agents", "config"]:
                target = _recipient(data.get("address"), identities, p)
                if "expected_updated_at" not in data:
                    raise Invalid("expected_updated_at is required; refresh agent config")
                return _answer(await ui_capabilities.update_agent_config(
                    p, identities, who, target, max_parallel_tasks=data.get("max_parallel_tasks"),
                    auto_claim_enabled=data.get("auto_claim_enabled"),
                    expected_updated_at=data["expected_updated_at"],
                    capability_tags=data.get("capabilities"),
                    expected_capabilities_updated_at=data.get("expected_capabilities_updated_at")))
            if path == ["capabilities"]:
                return _answer(await ui_capabilities.define_and_notify(
                    p, identities, who, data.get("name"), data.get("description")))
            if path == ["rooms"]:
                return _answer(store.create_room(data.get("name"), data.get("description"), actor), 201)
            if len(path) == 3 and path[0] == "rooms" and path[2] == "members":
                return _answer(teams.add_member(db, path[1], _recipient(
                    data.get("address"), identities, p), actor))
            if len(path) == 4 and path[0] == "rooms" and path[2:] == ["members", "remove"]:
                return _answer(teams.remove_member(db, path[1], _address(data.get("address")), actor))
            if len(path) == 3 and path[0] == "rooms" and path[2] == "manager":
                return _answer(teams.promote(db, path[1], _recipient(data.get("address"),
                    identities, p), actor, expected_revision=data.get("expected_revision")))
            if path == ["tasks"]:
                to_manager = data.get("assign_to_manager", False)
                if type(to_manager) is not bool:
                    raise Invalid("assign_to_manager must be true or false")
                room = data.get("room")
                manager = None
                if to_manager:
                    manager = teams.manager(db, room)["manager"]
                    if manager is None:
                        raise Invalid("room has no manager to assign this task to")
                    manager = _recipient(manager, identities, p)
                offered = graph_tasks.offer(db, who.user, data.get("room"),
                    data.get("title"), data.get("summary"),
                    required_capabilities=data.get("required_capabilities"))
                if manager is not None:
                    try:
                        assigned = assignments.assign(db, who.user, offered["node_id"], manager,
                                                       expected_revision=0, manager_actor=manager)
                        offered["assignment"] = assigned
                        dm = task_announcements.announce_assignment(
                            db, p.dir, offered["node_id"], manager, (who.user, who.device, "human"),
                            assigned["revision"])
                        if dm is None:
                            offered["assignment_notification"] = "could not persist; assignment remains queued"
                        else:
                            offered["assignment_notification"] = "stored"
                            offered["assignment_notified_live"] = await ui_chat.notify_human_dm(
                                p.dir, manager, dm)
                    except (Conflict, Invalid) as exc:
                        offered["assignment_warning"] = str(exc)
                message = task_announcements.announce_offer(
                    db, p.dir, room, offered, (who.user, who.device, "human"),
                    sender_origin="human_ui", assigned=offered.get("assignment") is not None)
                if message is None:
                    return _answer({**offered, "room_notification": "could not persist; task remains available"}, 201)
                delivered = await ui_chat.notify_human_room(p.dir, db, p.meta, identities,
                                                             room, message)
                return _answer({**offered, "room_notification": "stored",
                                "notified_count": delivered}, 201)
            if len(path) == 3 and path[0] == "tasks" and path[2] == "assign":
                # Required, not `data.get(...)`: assign() only compares the revision when it is
                # not None, so a body that simply omitted the key performed an UNFENCED assign
                # and clobbered a concurrent manager's reassignment. /clear and /rooms/{r}/manager
                # already reject a missing revision; this endpoint was the odd one out.
                revision = data.get("expected_revision")
                if type(revision) is not int or revision < 0:
                    raise Invalid("expected_revision must be the current assignment revision")
                recipient = _recipient(data.get("address"), identities, p)
                assigned = assignments.assign(db, who.user, path[1], recipient,
                    expected_revision=revision,
                    confirm_displace=data.get("confirm_displace") is True)
                message = task_announcements.announce_assignment(
                    db, p.dir, path[1], recipient, (who.user, who.device, "human"),
                    assigned["revision"])
                if message is None:
                    return _answer({**assigned, "assignment_notification":
                                    "could not persist; assignment remains queued"})
                live = await ui_chat.notify_human_dm(p.dir, recipient, message)
                return _answer({**assigned, "assignment_notification": "stored",
                                "assignment_notified_live": live})
            if len(path) == 3 and path[0] == "tasks" and path[2] == "clear":
                return _answer(assignments.clear(db, who.user, path[1], "browser_unassign",
                    expected_revision=data.get("expected_revision"),
                    confirm_displace=data.get("confirm_displace") is True))
            if path == ["instructions"]:
                target = (_recipient(data.get("address"), identities, p)
                          if not data.get("to_manager") else actor)
                return _answer(instructions.enqueue(db, who.user, target, data.get("body"),
                    data.get("idempotency_key"), room=data.get("room"),
                    to_manager=data.get("to_manager", False),
                    retry_of=data.get("retry_of")), 201)
            if len(path) == 3 and path[0] == "instructions" and path[2] == "cancel":
                return _answer(instructions.cancel(db, who.user, path[1]))
            if path == ["dm"]:
                recipient = _recipient(data.get("address"), identities, p)
                sent = ui_chat.send_human_dm(db, who, identities, recipient,
                    data.get("body"), data.get("idempotency_key"), project_meta=p.meta)
                live = await ui_chat.notify_human_dm(p.dir, recipient, sent)
                return _answer({**sent, "notified_live": live}, 201)
    except Conflict as exc:
        return _answer({"error": str(exc)}, 409)
    except TooLarge as exc:
        return _answer({"error": str(exc)}, 413)
    except (Invalid, ValueError, TypeError) as exc:
        return _answer({"error": str(exc)}, 422)
    except NotFound as exc:
        return _answer({"error": str(exc)}, 404)
    return _answer({"error": "not found"}, 404)
