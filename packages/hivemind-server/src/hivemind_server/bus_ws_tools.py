"""MCP surface for the push bus.

Five tools, where v1 had twenty-four. Everything v1 spent tools on for *receiving* — poll, peek,
ack, wait, cursors, history, reap — is gone, because a pushed message needs none of it. What is
left is: get connected, send, see who is there, leave.
"""
from __future__ import annotations

import functools
from typing import Optional

from mcp.types import ToolAnnotations

from .bus_ws import BusError, MAX_BODY, hub_for

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
        except BusError as e:
            return {"ok": False, "error_kind": "bus", "error": str(e)}
    return wrap


def attach(mcp, project, cfg) -> None:
    hub = hub_for(project.name)
    # The listener needs a URL it can reach. cfg.public_url is what the deployment advertises;
    # it is only ever used to build the string handed back to the agent.
    base = getattr(cfg, "public_url", "").rstrip("/")
    ws_base = ("ws://" + base.split("://", 1)[1]) if "://" in base else base
    ws_url = f"{ws_base}/p/{project.name}/bus/ws"

    @mcp.tool(annotations=WRITE,
              description="Join the agent bus and start receiving messages from other agents. "
                          "Call this ONCE at the start of a session, then run the returned "
                          "`monitor_command` with the Monitor tool (persistent=true). After that, "
                          "peer messages arrive on their own as notifications — there is nothing "
                          "to poll. `label` is how other agents address you; pick something "
                          "stable and descriptive (the machine or the job, not a random id).")
    @_envelope
    def bus_connect(label: str, meta: Optional[dict] = None) -> dict:
        t = hub.mint_ticket(label, meta)
        return {
            "peer": t["label"],
            # The command carries the LABEL, not the ticket. The listener mints its own ticket on
            # start and again on every reconnect, so a dropped network does not end the session —
            # a single-use ticket baked into argv would make the first blip terminal, which is
            # how the previous design failed. It also keeps the ticket out of the process list.
            "monitor_command": f"hivemind bus listen --label {t['label']}",
            "ticket": t["ticket"],
            # Advisory only. A server bound to 0.0.0.0 cannot know the address a client used to
            # reach it, so clients build the ws URL from their own base URL instead.
            "ws_url_hint": f"{ws_url}?ticket={t['ticket']}",
            "next": ("run monitor_command with the Monitor tool now: "
                     "Monitor(command=<monitor_command>, description='hivemind bus', "
                     "persistent=true). It reconnects by itself if the connection drops."),
        }

    @mcp.tool(annotations=WRITE,
              description="Send a message to one peer, by label. It arrives in that agent's "
                          "conversation as a notification within milliseconds. If the peer is "
                          "momentarily disconnected the message is queued and delivered when it "
                          "reconnects; the reply tells you which happened. Bus traffic is "
                          "EPHEMERAL — anything worth keeping goes in the graph.")
    @_envelope
    def bus_send(to: str, body: str, agent: str = "agent") -> dict:
        return _run(hub.send(agent, to, body))

    @mcp.tool(annotations=WRITE,
              description="Send a message to every other connected peer in a room (default "
                          "'lobby'). Use sparingly: every recipient pays attention for it.")
    @_envelope
    def bus_broadcast(body: str, room: str = "lobby", agent: str = "agent") -> dict:
        return _run(hub.broadcast(agent, body, room))

    @mcp.tool(annotations=RO,
              description="Fetch the FULL text of a bus message by id. Notifications are clipped "
                          "at ~512 characters, so a long message arrives truncated with its id — "
                          "call this to read the rest. Ids stay resolvable for about an hour.")
    @_envelope
    def bus_message(message_id: str) -> dict:
        return hub.message(message_id)

    @mcp.tool(annotations=RO,
              description="Who is on the bus right now, and whether each peer is currently "
                          "connected. Check this before sending, so you address a real label.")
    @_envelope
    def bus_peers(online_only: bool = False) -> dict:
        peers = hub.peers(online_only=online_only)
        return {"peers": peers, "count": len(peers),
                "hint": "bus_send(to=<peer>, body=…)" if peers else
                        "nobody is connected; run bus_connect to join"}

    @mcp.tool(annotations=WRITE,
              description="Leave the bus: drops the connection and removes this peer from "
                          "bus_peers. Stop the Monitor task as well.")
    @_envelope
    def bus_disconnect(label: str) -> dict:
        p = hub.peer(label)
        if p is None:
            raise BusError(f"no peer {label!r}")
        hub.forget(p, force=True)     # explicit intent, unlike the sweep in bus_peers
        return {"peer": label, "disconnected": True,
                "next": "stop the Monitor task with TaskStop"}


def _run(coro):
    """Bridge the hub's async fan-out into a sync MCP tool body.

    MCP tool functions run on a worker thread, so there is no running loop here to await on — but
    the WebSocket sends must happen on the loop that owns those sockets. `_LOOP` is captured when
    the app starts; scheduling onto it is what makes a send from a tool call reach a socket owned
    by the server's event loop.
    """
    import asyncio

    from . import bus_ws
    loop = getattr(bus_ws, "_LOOP", None)
    if loop is None or not loop.is_running():
        # No server loop (unit tests drive the hub directly); run it inline.
        return asyncio.run(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=10)
