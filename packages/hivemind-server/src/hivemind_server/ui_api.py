"""Project-scoped browser endpoints over the same coordination services as MCP."""
from __future__ import annotations

import json
import logging
import time

from starlette.responses import JSONResponse

from . import assignments, capabilities, graph, graph_tasks, instructions, teams, ui_agents, ui_chat, ui_rooms
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
                before_id = req.query_params.get("before_id")
                if before_id is not None and len(before_id) > 64:
                    raise Invalid("invalid before_id cursor")
                with db.read() as cur:
                    rows = cur.execute("SELECT node_id FROM graph_task WHERE node_id<? "
                                       "ORDER BY node_id DESC LIMIT ?",
                                       (before_id or "Z", limit + 1)).fetchall()
                has_older = len(rows) > limit
                rows = rows[:limit]
                items = []
                for row in rows:
                    nid = row["node_id"]
                    props = graph.get_node(db, node_id=nid)["current"]["props"]
                    items.append({**assignments.view(db, nid), **graph_tasks.read(db, nid),
                                  "title": props.get("title"), "summary": props.get("summary")})
                return _answer({"tasks": items,
                                "older_cursor": rows[-1]["node_id"] if has_older else None})
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
                return _answer(graph_tasks.offer(db, who.user, data.get("room"),
                    data.get("title"), data.get("summary"),
                    required_capabilities=data.get("required_capabilities")), 201)
            if len(path) == 3 and path[0] == "tasks" and path[2] == "assign":
                return _answer(assignments.assign(db, who.user, path[1], _recipient(
                    data.get("address"), identities, p),
                    expected_revision=data.get("expected_revision"),
                    confirm_displace=data.get("confirm_displace") is True))
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
