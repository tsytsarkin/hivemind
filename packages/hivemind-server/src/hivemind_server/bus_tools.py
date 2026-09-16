"""Bus MCP tools + the long-poll REST route. Attached from mcp_tools.build_mcp.

The REST side exists for one reason: `GET /bus/wait` blocks until a message is visible, so a
client-side watcher can sit on it and EXIT the moment something lands. On a harness that
re-invokes an agent when a background process exits (Claude Code does), that exit is the
interrupt. MCP tools cannot do this — a tool call that blocks for 25s blocks the agent's turn.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Optional

from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import bus
from .db import Conflict, Invalid, NotFound

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)


def _envelope(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            out = fn(*a, **k)
            if isinstance(out, dict) and "ok" not in out:
                out = {"ok": True, **out}
            return out
        except Conflict as e:
            return {"ok": False, "error_kind": "conflict", "error": str(e)}
        except NotFound as e:
            return {"ok": False, "error_kind": "not_found", "error": str(e)}
        except Invalid as e:
            return {"ok": False, "error_kind": "invalid", "error": str(e)}
    return wrap


def attach(mcp, project, cfg) -> None:
    db = project.db
    S_TTL, M_TTL = cfg.bus_session_ttl, cfg.bus_message_ttl
    LEASE, MAX_WAIT = cfg.bus_lease_seconds, cfg.bus_max_wait

    # ── session lifecycle ────────────────────────────────────────────────────────
    @mcp.tool(annotations=WRITE,
              description="Join the agent bus and advertise what THIS session can do. Returns a "
                          "session_id that every other bus call needs. Identity is per-session, "
                          "NOT per-token: one token is shared by many agents with different "
                          "capabilities. capabilities={'browser.cdp':{}, "
                          "'device.handset.attached':{'serial':'...'}} — use specific dotted names "
                          "and check bus_capabilities() first so you match the vocabulary already "
                          "in use. Set interruptible=true only if a watcher can actually wake you "
                          "(true for Claude Code with a backgrounded `hivemind bus wait`).")
    @_envelope
    def bus_hello(label: str, capabilities: Optional[dict] = None,
                  harness: Optional[str] = None, interruptible: bool = False,
                  rooms: Optional[list[str]] = None, meta: Optional[dict] = None,
                  ttl: int = 0) -> dict:
        return bus.hello(db, label=label, capabilities=capabilities, harness=harness,
                         interruptible=interruptible, rooms=rooms, meta=meta,
                         ttl=ttl or S_TTL)

    @mcp.tool(annotations=WRITE,
              description="Heartbeat: keep this session in the directory. Call it before "
                          "expires_at or you silently drop out and your claims are released. "
                          "Passing capabilities REPLACES the whole advertised set — send them all, "
                          "not just the new one.")
    @_envelope
    def bus_ping(session_id: str, capabilities: Optional[dict] = None,
                 status: Optional[str] = None, ttl: int = 0) -> dict:
        return bus.ping(db, session_id, ttl=ttl or S_TTL, capabilities=capabilities,
                        status=status)

    @mcp.tool(annotations=WRITE,
              description="Leave the bus cleanly: drop out of the directory now and release any "
                          "request still claimed, so it reopens immediately instead of waiting "
                          "for the lease to expire.")
    @_envelope
    def bus_bye(session_id: str) -> dict:
        return bus.bye(db, session_id)

    # ── directory ────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO,
              description="Who is on the bus right now and what they can do. Filter with "
                          "capability='browser.cdp' (exact) or 'browser.*' (prefix). Capabilities "
                          "are SELF-ASSERTED — an agent advertising one it lacks will win a claim "
                          "and fail it. Prefer interruptible sessions for work that should start "
                          "promptly; a non-interruptible one only notices at its next poll.")
    @_envelope
    def bus_agents(capability: Optional[str] = None, include_ended: bool = False) -> dict:
        return bus.sessions(db, capability=capability, include_ended=include_ended)

    @mcp.tool(annotations=RO,
              description="Every capability currently advertised, with how many live sessions "
                          "hold it. Read this BEFORE inventing a capability name so you match the "
                          "vocabulary already in use instead of a synonym nobody queries for.")
    @_envelope
    def bus_capabilities() -> dict:
        return bus.capability_index(db)

    @mcp.tool(annotations=RO, description="Rooms with live members and message counts.")
    @_envelope
    def bus_rooms() -> dict:
        return bus.rooms(db)

    @mcp.tool(annotations=WRITE, description="Subscribe this session to a room's broadcasts.")
    @_envelope
    def bus_join(session_id: str, room: str) -> dict:
        return bus.join(db, session_id, room)

    @mcp.tool(annotations=WRITE, description="Unsubscribe this session from a room.")
    @_envelope
    def bus_leave(session_id: str, room: str) -> dict:
        return bus.leave(db, session_id, room)

    # ── messages ─────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=WRITE,
              description="Send a message: to a room (default 'lobby') or direct to one session "
                          "via to_session. To ASK the room a question use kind='question'; to "
                          "answer one, pass reply_to=<its seq> so your answer is attached to it "
                          "rather than merely nearby in a busy room (a reply inherits the "
                          "parent's room). This is CHATTER, not knowledge — it expires. Anything "
                          "that should outlive the conversation goes in the graph with "
                          "graph_upsert, and you POINT at it with refs=[...] rather than pasting "
                          "it. refs accepts bare node_id strings or objects: "
                          "{'kind':'node','id':...} | {'kind':'version','id':...} (pin an exact "
                          "revision) | {'kind':'subject','key':...,'version':...} | "
                          "{'kind':'traversal','id':...,'edge_types':[...],'depth':1-4} | "
                          "{'kind':'search','query':...}. Each may carry role and note.")
    @_envelope
    def bus_post(session_id: str, body: str, room: Optional[str] = None,
                 to_session: Optional[str] = None, kind: str = "chat",
                 data: Optional[dict] = None, refs: Optional[list] = None,
                 reply_to: Optional[int] = None) -> dict:
        return bus.post(db, sender=session_id, body=body, room=room, to_session=to_session,
                        kind=kind, data=data, refs=refs, reply_to=reply_to, ttl=M_TTL)

    @mcp.tool(annotations=WRITE,
              description="Read everything new for this session and ADVANCE the cursor. Returns "
                          "direct messages plus broadcasts from rooms you joined; your own posts "
                          "are excluded unless include_self. Delivery is at-least-once, so handle "
                          "messages idempotently. Call this at the top of a turn, and after a "
                          "`hivemind bus wait` wakes you.")
    @_envelope
    def bus_poll(session_id: str, after: Optional[int] = None, limit: int = 50,
                 rooms: Optional[list[str]] = None, include_self: bool = False,
                 advance: bool = True) -> dict:
        return bus.poll(db, session_id, after=after, limit=limit, rooms_filter=rooms,
                        include_self=include_self, advance=advance)

    @mcp.tool(annotations=RO,
              description="Look at new messages WITHOUT consuming them (cursor unmoved). Use when "
                          "you want to see what is waiting but are not ready to handle it; the "
                          "long-poll watcher uses this so a watcher that dies cannot swallow a "
                          "message on the agent's behalf.")
    @_envelope
    def bus_peek(session_id: str, after: Optional[int] = None, limit: int = 50,
                 rooms: Optional[list[str]] = None, include_self: bool = False) -> dict:
        return bus.peek(db, session_id, after=after, limit=limit, rooms_filter=rooms,
                        include_self=include_self)

    @mcp.tool(annotations=WRITE,
              description="Move the cursor by hand — after handling a peek, or to skip a backlog "
                          "you have decided not to read. Never moves backwards.")
    @_envelope
    def bus_ack(session_id: str, seq: int) -> dict:
        return bus.ack(db, session_id, seq)

    @mcp.tool(annotations=RO,
              description="A room's recent broadcasts, oldest-first, independent of your cursor. "
                          "Joining a room does NOT replay its backlog into bus_poll, so use this "
                          "to catch up on a room you just joined. Paginate with before=<seq>.")
    @_envelope
    def bus_history(room: str, limit: int = 50, before: Optional[int] = None) -> dict:
        return bus.history(db, room, limit=limit, before=before)

    @mcp.tool(annotations=RO,
              description="A message and everything posted in reply to it, oldest-first with "
                          "nesting depth — the answers to a question, gathered. Poll shows a "
                          "reply_count on anything that has replies; call this to read them. "
                          "Direct messages are excluded, so a private answer to a public question "
                          "stays private.")
    @_envelope
    def bus_thread(seq: int, limit: int = 200) -> dict:
        return bus.thread(db, seq, limit=limit)

    # ── requests ─────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=WRITE,
              description="Ask for work by CAPABILITY: every live session advertising all of "
                          "`needs` is notified, and the first to bus_claim wins. Use this instead "
                          "of picking a worker yourself — a wedged agent then simply never "
                          "claims, rather than stalling your request. Give to_session instead to "
                          "address one agent directly. Pass refs=[node_id, ...] to say what the "
                          "work is ABOUT; refs are validated now, so a bad pointer fails here "
                          "instead of confusing a worker later. Poll bus_request_get for the "
                          "answer.")
    @_envelope
    def bus_request(session_id: str, task: str, needs: Optional[list[str]] = None,
                    to_session: Optional[str] = None, payload: Optional[dict] = None,
                    room: Optional[str] = None, refs: Optional[list] = None,
                    lease_sec: int = 0, ttl: int = 3600) -> dict:
        return bus.request(db, requester=session_id, task=task, needs=needs,
                           to_session=to_session, payload=payload, room=room, refs=refs,
                           lease_sec=lease_sec or LEASE, ttl=ttl, msg_ttl=M_TTL)

    @mcp.tool(annotations=WRITE,
              description="Take a request. Exactly one session wins: won=false means someone beat "
                          "you and there is nothing to do. You must already advertise every "
                          "capability the request needs. Finish before lease_expires_at or it "
                          "reopens for someone else.")
    @_envelope
    def bus_claim(session_id: str, request_id: str, lease_sec: int = 0) -> dict:
        return bus.claim(db, request_id, session_id, lease_sec=lease_sec or LEASE)

    @mcp.tool(annotations=WRITE,
              description="Hand a claim back without failing it, so another agent can pick it up. "
                          "Do this the moment you know you cannot finish — it is much better than "
                          "letting the lease run out.")
    @_envelope
    def bus_release(session_id: str, request_id: str, reason: str = "") -> dict:
        return bus.release(db, request_id, session_id, reason)

    @mcp.tool(annotations=WRITE,
              description="Answer a request you claimed. Pass result= on success or error= on "
                          "failure; reporting a failure is far more useful than going silent, "
                          "which just makes the requester wait for the lease. If the work "
                          "produced durable knowledge, graph_upsert it and pass refs=[node_id] "
                          "instead of inlining it — the message expires, the node does not.")
    @_envelope
    def bus_respond(session_id: str, request_id: str, result: Any = None,
                    error: Optional[str] = None, refs: Optional[list] = None) -> dict:
        return bus.respond(db, request_id, session_id, result=result, error=error, refs=refs,
                           msg_ttl=M_TTL)

    @mcp.tool(annotations=RO, description="Full state of one request: claim, lease, result.")
    @_envelope
    def bus_request_get(request_id: str) -> dict:
        return bus.request_get(db, request_id)

    @mcp.tool(annotations=RO,
              description="List requests. claimable_only=true with your session_id returns just "
                          "the open ones you actually match and could win.")
    @_envelope
    def bus_requests(session_id: Optional[str] = None, state: Optional[str] = None,
                     claimable_only: bool = False, limit: int = 50) -> dict:
        return bus.requests(db, session_id=session_id, state=state,
                            claimable_only=claimable_only, limit=limit)

    # ── graph references ─────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO,
              description="Follow a message's or request's refs into the graph and return what "
                          "they actually point at — full nodes, a traversal's neighbours, or a "
                          "search's hits. Give exactly one of request_id or seq. Poll returns "
                          "only compact labels; call this when you have decided you care.")
    @_envelope
    def bus_resolve(request_id: Optional[str] = None, seq: Optional[int] = None,
                    limit: int = 25) -> dict:
        return bus.resolve(db, request_id=request_id, seq=seq, limit=limit)

    @mcp.tool(annotations=RO,
              description="Which live bus traffic points at this graph node — who is asking about "
                          "it, working on it, or reporting a result against it right now. The "
                          "reverse of a ref. Bus traffic is ephemeral, so an empty answer means "
                          "nobody is discussing it AT THE MOMENT, not that nobody ever did.")
    @_envelope
    def bus_node_refs(node_id: str, limit: int = 50) -> dict:
        return bus.refs_for_node(db, node_id, limit=limit)

    # ── housekeeping ─────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO, description="Bus counters: live sessions, messages, head seq, "
                                          "open/claimed requests.")
    @_envelope
    def bus_stats() -> dict:
        return bus.stats(db)

    @mcp.tool(annotations=WRITE,
              description="Expire dead sessions, reopen leases whose holder went away, and delete "
                          "expired messages. Runs automatically on read paths and from "
                          "deploy/maintenance.sh; call it directly only to force a sweep.")
    @_envelope
    def bus_reap() -> dict:
        return bus.reap(db)

    # ── REST: the long-poll that makes interrupts possible ───────────────────────
    @mcp.custom_route("/bus/wait", methods=["GET"])
    async def bus_wait(req: Request) -> Response:
        """Block until this session has a visible message, then return it WITHOUT consuming it.

        `hivemind bus wait` sits on this and exits as soon as it returns messages; a harness that
        re-invokes an agent on background-process exit turns that into an interrupt. Peek, not
        poll, on purpose: the watcher is not the reader, and a watcher that dies between seeing a
        message and waking the agent must not have consumed it.
        """
        q = req.query_params
        session_id = q.get("session")
        if not session_id:
            return JSONResponse({"ok": False, "error": "session= is required"}, status_code=400)
        try:
            after = int(q["after"]) if q.get("after") else None
            wait = min(max(float(q.get("wait", 25)), 0.0), MAX_WAIT)
            interval = min(max(float(q.get("interval", 1.0)), 0.2), 10.0)
            limit = int(q.get("limit", 50))
        except ValueError:
            return JSONResponse({"ok": False, "error": "after/wait/interval/limit must be numeric"},
                                status_code=400)
        rooms_filter = [r for r in (q.get("rooms") or "").split(",") if r]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait
        while True:
            try:
                out = await run_in_threadpool(
                    bus.peek, db, session_id, after=after, limit=limit,
                    rooms_filter=rooms_filter or None,
                    include_self=q.get("include_self") in ("1", "true", "yes"))
            except NotFound as e:
                return JSONResponse({"ok": False, "error_kind": "not_found", "error": str(e)},
                                    status_code=404)
            except Invalid as e:
                return JSONResponse({"ok": False, "error_kind": "invalid", "error": str(e)},
                                    status_code=409)
            if out["messages"] or loop.time() >= deadline:
                out["ok"] = True
                out["timed_out"] = not out["messages"]
                return JSONResponse(out)
            # `after` is left as passed. When it is None each pass re-reads the session cursor,
            # so if the agent drains the queue in another process mid-wait we correctly stop
            # seeing those messages instead of waking it for work it already did.
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))
