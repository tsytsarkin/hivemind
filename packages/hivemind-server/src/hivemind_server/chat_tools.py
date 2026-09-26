"""MCP interface to project-scoped durable chat and canonical WebSocket notifications."""
from __future__ import annotations

import logging
from typing import Optional

from . import bus_ws, chat_ws
from .bus_ws_tools import _run
from .chat import ChatStore, StableAddress, _address, stable_identity
from .db import Invalid
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import Identity, current_identity
from .projects_meta import can_access

log = logging.getLogger(__name__)


def attach(mcp, cfg, identities) -> None:
    def _project():
        return current_project()

    def _store() -> ChatStore:
        p = _project()
        return ChatStore.for_project(p.db, p.dir)

    def _addressed(client: str, session_id: str) -> tuple[StableAddress, str]:
        user, device, actual_client, session = stable_identity(current_identity(), client, session_id)
        return (user, device, actual_client), session

    def _touch(store: ChatStore, stable: StableAddress, session: str) -> None:
        store.touch(stable, session)

    def _hub() -> chat_ws.CanonicalHub:
        p = _project()
        bus_ws.register_secret(p.dir)
        return chat_ws.hub_for(p.dir)

    def _ws_url() -> str:
        base = (bus_ws.current_origin() or getattr(cfg, "public_url", "")).rstrip("/")
        if "://" in base:
            scheme, _, hostport = base.partition("://")
            base = ("wss://" if scheme == "https" else "ws://") + hostport
        return f"{base}/p/{_project().name}/chat/ws"

    def _notify(stable: StableAddress, message: dict, room: Optional[str] = None) -> int:
        frame = {"v": 2, "type": "chat", "id": message["id"],
                 "channel": message["channel"], "room": room,
                 "from": "-".join(message["sender"]),
                 "preview": message["body"].encode("utf-8")[:160].decode("utf-8", errors="ignore"),
                 "ts": message["created_at"]}
        try:
            return _run(_hub().notify(stable, frame))
        except Exception:
            # Persistence already succeeded. Socket failure changes only the live-notified bit.
            return 0

    @mcp.tool(annotations=WRITE,
              description="Connect a canonical user-device-client-sessionid to the durable chat. "
                          "The server binds user/device to the token; run monitor_command with "
                          "the shipped listener, then fetch missed messages from chat_inbox.")
    @_envelope
    def chat_connect(client: str, session_id: str) -> dict:
        stable, session = _addressed(client, session_id)
        p = _project()
        if not bus_ws.is_mounted(p.dir):
            raise bus_ws.not_mounted_error(p.name)
        key = _hub().mint_key((*stable, session), current_identity().credential_hash)
        _store().touch(stable, session)
        ws_url = _ws_url()
        command = (f'python3 "$HOME/.hivemind/bus-listen.py" '
                   f'--url {ws_url} --key {key}')
        return {"peer": "-".join((*stable, session)), "address": stable,
                "ws_url": ws_url, "listen_key": key, "monitor_command": command,
                "next": "start the listener; call chat_inbox(client,session_id) to catch up"}

    @mcp.tool(annotations=WRITE, description="See last activity and live presence for project agents.")
    @_envelope
    def chat_agents(client: str, session_id: str, online_only: bool = False) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        _touch(store, who, session)
        hub = _hub()
        agents = [{**entry, "online": hub.online(entry["address"], entry["session_id"])}
                  for entry in store.agents()]
        if online_only:
            agents = [entry for entry in agents if entry["online"]]
        return {"agents": agents, "count": len(agents)}

    @mcp.tool(annotations=WRITE, description="Explicitly create a public-within-project topic room with a short description.")
    @_envelope
    def chat_room_create(name: str, description: str, client: str, session_id: str) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        out = store.create_room(name, description, who)
        _touch(store, who, session)
        return out

    @mcp.tool(annotations=WRITE, description="List rooms in this project and their descriptions.")
    @_envelope
    def chat_room_list(client: str, session_id: str) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        out = store.rooms()
        _touch(store, who, session)
        return {"rooms": out, "count": len(out)}

    @mcp.tool(annotations=WRITE, description="Subscribe this stable agent address to a room's live updates.")
    @_envelope
    def chat_room_join(name: str, client: str, session_id: str) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        result = store.join(name, who)
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE, description="Unsubscribe without deleting room history or your mailbox.")
    @_envelope
    def chat_room_leave(name: str, client: str, session_id: str) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        from . import teams
        result = teams.remove_member(_project().db, name, who, who)
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE,
              description="Send a 24-hour persistent DM to an eligible user-device-client address, "
                          "even when that agent is offline. Use a fresh idempotency_key per message.")
    @_envelope
    def chat_send(to_user: str, to_device: str, to_client: str, client: str,
                  session_id: str, body: str, idempotency_key: str) -> dict:
        who, session = _addressed(client, session_id)
        recipient = _address((to_user, to_device, to_client))
        project = _project()
        if not identities.has_device(to_user, to_device) or not can_access(
                Identity(to_user, to_device), project.meta):
            raise Invalid("recipient user/device does not exist or lacks project access")
        store = _store()
        message = store.send("dm", recipient, who, body, idempotency_key, session_id=session)
        live = _notify(recipient, message) > 0
        return {"id": message["id"], "seq": message["seq"],
                "expires_at": message["expires_at"], "notified_live": live,
                "duplicate": message["duplicate"]}

    @mcp.tool(annotations=WRITE,
              description="Read up to 100 unexpired direct messages after a sequence cursor; "
                          "a gap warns that messages expired after 24 hours.")
    @_envelope
    def chat_inbox(client: str, session_id: str, after_seq: int = 0, limit: int = 100) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        result = store.inbox(who, after_seq, limit)
        result.update(store.read_marker(who, "dm"))
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE, description="Advance this mailbox's read position after processing the DM.")
    @_envelope
    def chat_mark_read(client: str, session_id: str, up_to_seq: int) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        result = store.mark_read(who, "dm", up_to_seq)
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE,
              description="Post a freeform text or informational progress update to an EXISTING room; "
                          "progress needs no acknowledgement. Set task_node_id for a live "
                          "claim's nonempty progress so unrelated tasks stay overdue.")
    @_envelope
    def chat_room_post(name: str, client: str, session_id: str, body: str,
                       idempotency_key: str, kind: str = "text",
                       task_node_id: Optional[str] = None) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        message = store.send("room", name, who, body, idempotency_key,
                             kind=kind, session_id=session, task_node_id=task_node_id)
        delivered = 0
        try:
            for recipient in store.subscribers(name):
                if recipient != who and can_access(Identity(recipient[0], recipient[1]),
                                                   _project().meta):
                    delivered += _notify(recipient, message, room=name)
        except Exception:
            # The post was committed. A notification lookup cannot undo the accepted send.
            log.exception("room notification lookup failed after persistent send")
        return {"id": message["id"], "seq": message["seq"],
                "expires_at": message["expires_at"], "notified_live": delivered > 0,
                "notified_count": delivered, "duplicate": message["duplicate"]}

    @mcp.tool(annotations=WRITE, description="Fetch a room's retained history, including posts from before you joined.")
    @_envelope
    def chat_room_history(name: str, client: str, session_id: str,
                          after_seq: int = 0, limit: int = 100) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        result = store.history(name, after_seq, limit)
        result.update(store.read_marker(who, name, channel="room"))
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE, description="Advance this room's read position after processing a post.")
    @_envelope
    def chat_room_mark_read(name: str, client: str, session_id: str, up_to_seq: int) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        result = store.mark_read(who, name, up_to_seq, channel="room")
        _touch(store, who, session)
        return result

    @mcp.tool(annotations=WRITE,
              description="Fetch a full retained message by ID; private DMs are visible only to "
                          "their sender and recipient, never through legacy bus_message.")
    @_envelope
    def chat_message_get(id: str, client: str, session_id: str) -> dict:
        who, session = _addressed(client, session_id)
        store = _store()
        message = store.message(id, who)
        _touch(store, who, session)
        return message
