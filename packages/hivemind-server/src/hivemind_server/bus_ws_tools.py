"""MCP surface for the push bus.

Five tools, where v1 had twenty-four. Everything v1 spent tools on for *receiving* — poll, peek,
ack, wait, cursors, history, reap — is gone, because a pushed message needs none of it. What is
left is: get connected, send, see who is there, leave.
"""
from __future__ import annotations

from typing import Optional

from .bus_ws import BusError, MAX_BODY, current_origin, hub_for, register_secret
from .envelope import RO, WRITE, current_project, envelope as _envelope


# Where the skill installs the dependency-free listener. It must be an absolute path that any
# shell expands, because a Monitor command runs in a plain shell: measured, neither
# CLAUDE_PLUGIN_ROOT nor CLAUDE_SKILL_DIR is set there, so a path built from either is empty.
LISTENER = "$HOME/.hivemind/bus-listen.py"


def attach(mcp, cfg) -> None:
    def _hub():
        """The hub of the project this CALL is for — one server now answers for every project, so
        binding a hub at attach time would push every message onto one project's bus.

        register_secret is idempotent (it reads the key back off disk) and is repeated here for a
        project created after startup, which build_app never looped over.
        """
        p = current_project()
        register_secret(p.name, p.dir / "bus_secret")
        return hub_for(p.name)

    def _ws_url() -> str:
        """Build a ws:// URL from the address THIS caller used, falling back to config.

        Per call, not once at startup: the server binds 0.0.0.0 for both the LAN and the mesh, so
        its configured public_url is `http://0.0.0.0:8787` and a listener given that URL fails with
        ConnectionRefusedError. Whatever Host the request arrived on is reachable by definition.
        """
        base = (current_origin() or getattr(cfg, "public_url", "")).rstrip("/")
        if "://" in base:
            scheme, _, hostport = base.partition("://")
            base = ("wss://" if scheme == "https" else "ws://") + hostport
        return f"{base}/p/{current_project().name}/bus/ws"

    @mcp.tool(annotations=WRITE,
              description="Join the agent bus and start receiving messages from other agents. "
                          "Call this ONCE at the start of a session, then run the returned "
                          "`monitor_command` with the Monitor tool (persistent=true). After that, "
                          "peer messages arrive on their own as notifications — there is nothing "
                          "to poll. `label` is how other agents address you; pick something "
                          "stable and descriptive (the machine or the job, not a random id).")
    @_envelope
    def bus_connect(label: str, meta: Optional[dict] = None) -> dict:
        hub = _hub()
        k = hub.mint_listen_key(label, meta)
        t = hub.mint_ticket(label, meta)
        ws_url = _ws_url()
        return {
            "peer": k["label"],
            # The default command runs the listener the SKILL installs, with python3 and nothing
            # else. It deliberately does not name the `hivemind` CLI: that ships in
            # hivemind-client, which a machine holding only the plugin does not have, and it needs
            # a third-party websockets package on top. A plugin-only agent could not connect at
            # all. The listener under $HOME is dependency-free and always present once the skill
            # has loaded once.
            "monitor_command": (f'python3 "{LISTENER}" --url {ws_url} '
                                f'--key {k["listen_key"]}'),
            # The key is reusable and outlives a restart, so the listener reconnects on its own.
            # A single-use ticket in argv would make the first network blip terminal.
            "listen_key": k["listen_key"],
            "ws_url": ws_url,
            # For a machine that does have the hivemind CLI installed, with HIVEMIND_SERVER_URL
            # and HIVEMIND_TOKEN exported. Equivalent, just not available by default.
            "monitor_command_cli": f"hivemind bus listen --label {k['label']}",
            "ticket": t["ticket"],
            "next": ("run monitor_command with the Monitor tool now: "
                     "Monitor(command=<monitor_command>, description='hivemind bus', "
                     "persistent=true). It reconnects by itself if the connection drops. "
                     f"If it reports that {LISTENER} does not exist, load the hivemind skill "
                     "once — it installs the listener — then run it again."),
        }

    @mcp.tool(annotations=WRITE,
              description="Send a message to one peer, by label. It arrives in that agent's "
                          "conversation as a notification within milliseconds. If the peer is "
                          "momentarily disconnected the message is queued and delivered when it "
                          "reconnects; the reply tells you which happened. Bus traffic is "
                          "EPHEMERAL — anything worth keeping goes in the graph.")
    @_envelope
    def bus_send(to: str, body: str, agent: str = "agent") -> dict:
        return _run(_hub().send(agent, to, body))

    @mcp.tool(annotations=WRITE,
              description="Send a message to every other connected peer in a room (default "
                          "'lobby'). Use sparingly: every recipient pays attention for it.")
    @_envelope
    def bus_broadcast(body: str, room: str = "lobby", agent: str = "agent") -> dict:
        return _run(_hub().broadcast(agent, body, room))

    @mcp.tool(annotations=RO,
              description="Fetch the FULL text of a bus message by id. Notifications are clipped "
                          "at ~512 characters, so a long message arrives truncated with its id — "
                          "call this to read the rest. Ids stay resolvable for about an hour.")
    @_envelope
    def bus_message(message_id: str) -> dict:
        return _hub().message(message_id)

    @mcp.tool(annotations=RO,
              description="Who is on the bus right now, and whether each peer is currently "
                          "connected. Check this before sending, so you address a real label.")
    @_envelope
    def bus_peers(online_only: bool = False) -> dict:
        peers = _hub().peers(online_only=online_only)
        return {"peers": peers, "count": len(peers),
                "hint": "bus_send(to=<peer>, body=…)" if peers else
                        "nobody is connected; run bus_connect to join"}

    @mcp.tool(annotations=WRITE,
              description="Leave the bus: drops the connection and removes this peer from "
                          "bus_peers. Stop the Monitor task as well.")
    @_envelope
    def bus_disconnect(label: str) -> dict:
        hub = _hub()
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
