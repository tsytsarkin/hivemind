"""Agent bus v2: WebSocket push.

The v1 bus was poll-based, and that is why it was flaky. A Claude Code session is turn-based —
it runs no background loop — so a message was only ever seen if the receiver happened to call
`bus_poll`. An idle session (waiting on the user) or a busy one (mid-turn) never learned anything
had arrived, and a session whose TTL lapsed hard-errored on poll while senders were still told
their message was accepted.

Here delivery is a push: the agent opens one WebSocket through Claude Code's `Monitor` tool and
every text frame the server sends becomes a notification in its conversation. No polling, no
cursors, no acks, nothing to forget to call.

Design notes that are load-bearing:

* **Presence is the socket.** A peer is connected exactly as long as its WebSocket is open. There
  is no session table, no TTL and no reaper — the three things that made v1 lose messages silently.
* **State is in memory, not SQLite.** v1 persisted every chat message and then reaped it, which
  meant a provenance row outliving the message it described. Bus traffic is ephemeral by
  definition; anything durable belongs in the graph. Keeping it in memory also means a restart is
  a clean slate rather than a pile of dead sessions.
* **The offline queue is bounded.** A message sent while a peer is briefly disconnected is held
  and delivered on reconnect, but only `MAX_QUEUE` of them and only for `QUEUE_TTL`. Unbounded
  retention is how the blob store quietly grew to 94 GB; a chat buffer gets a hard cap.
* **Auth is a ticket, not a header.** The listener connects over a URL, not an authenticated HTTP
  call, so an authenticated MCP call mints a single-use, short-lived ticket and the long-lived
  bearer token never appears in a URL, a shell history or an access log.
* **Agents reach this through `hivemind bus listen`, not `Monitor(ws=…)` directly.** Measured:
  Monitor's ws source refuses private addresses ("the address is in a private, link-local, or
  cloud-metadata range"), and this server lives on a LAN address. A subprocess carries no such
  policy, so the CLI is the WebSocket client and prints one line per frame for Monitor's command
  source. The wire protocol below is unchanged by that.
* **Notification lines are clipped around 512 characters** by Claude Code, so the listener prints a
  short prefix plus a capped body and leaves the full text retrievable by id. A frame that is
  merely long must not push the useful part off the end.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, Optional

from .ids import ulid

PROTOCOL_VERSION = 1

TICKET_TTL = 60.0            # seconds a mint stays redeemable; single use
MAX_QUEUE = 100              # per-peer offline messages
QUEUE_TTL = 3600.0           # seconds an undelivered message is worth keeping
MAX_BODY = 256 * 1024        # per-frame body cap; big payloads belong in the blob store
HEARTBEAT = 30.0             # server->client ping interval
RECENT_MAX = 500             # recent frames kept retrievable by id
RECENT_TTL = 3600.0          # ...and for how long
RECENT_BYTES = 32 * 1024 * 1024   # ...and never more than this in total
QUEUE_BYTES = 8 * 1024 * 1024     # per-peer offline queue byte ceiling


class BusError(Exception):
    """Bad request against the bus (unknown peer, oversized body, spent ticket)."""


def _now() -> float:
    return time.time()


def _iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _Peer:
    """One connected agent. `send` is None while disconnected but the peer is still known if it
    has queued mail waiting."""

    __slots__ = ("peer_id", "label", "rooms", "ws", "queue", "connected_at", "meta")

    def __init__(self, peer_id: str, label: str, meta: Optional[dict] = None):
        self.peer_id = peer_id
        self.label = label
        self.rooms: set = {"lobby"}
        self.ws: Any = None                      # starlette WebSocket while connected
        self.queue: deque = deque(maxlen=MAX_QUEUE)
        self.connected_at: Optional[float] = None
        self.meta = meta or {}

    @property
    def online(self) -> bool:
        return self.ws is not None

    def public(self) -> dict:
        return {"peer": self.label, "peer_id": self.peer_id, "online": self.online,
                "rooms": sorted(self.rooms),
                "connected_at": self.connected_at and _iso(),
                "queued": len(self.queue), **({"meta": self.meta} if self.meta else {})}


class Hub:
    """In-process registry of connected agents and the fan-out that feeds them."""

    def __init__(self) -> None:
        self._peers: Dict[str, _Peer] = {}        # peer_id -> _Peer
        self._by_label: Dict[str, str] = {}       # label -> peer_id (last writer wins)
        self._tickets: Dict[str, tuple] = {}      # ticket -> (peer_id, expires_at)
        # MCP tool bodies run on worker threads while the event loop owns the sockets, so the
        # registry is touched from two threads. An asyncio.Lock would not help — it only
        # serialises coroutines on one loop. Individual dict ops are atomic under the GIL, but the
        # compound ones here (check-then-create, check-then-forget) are not, and the second is how
        # a peer that connects mid-sweep gets its live socket dropped.
        self._guard = threading.RLock()
        # Sent frames, newest last, so a notification can carry a pointer instead of the text.
        # Claude Code clips a notification at ~512 characters, so anything longer would simply be
        # lost if the wire format were the only copy. Bounded and TTL'd like everything else here.
        self._recent: deque = deque(maxlen=RECENT_MAX)

    def _remember(self, frame: dict) -> None:
        # Bounding the COUNT is not enough: 500 frames at the 256 KB body cap is 128 MB held for
        # an hour. Trim by bytes as well, oldest first, so retention cannot become a slow leak.
        with self._guard:
            self._recent.append({**frame, "_t": _now()})
            total = sum(len(f.get("body") or "") for f in self._recent)
            while total > RECENT_BYTES and len(self._recent) > 1:
                dropped = self._recent.popleft()
                total -= len(dropped.get("body") or "")

    def message(self, message_id: str) -> dict:
        """Full text of a recent message, by id — the out-of-band half of a clipped notification."""
        cutoff = _now() - RECENT_TTL
        for f in reversed(self._recent):
            if f.get("_t", 0) < cutoff:
                break
            if f.get("id") == message_id or str(f.get("id", ""))[-8:] == message_id:
                out = {k: v for k, v in f.items() if k != "_t"}
                out["chars"] = len(out.get("body") or "")
                return out
        raise BusError(
            f"no message {message_id!r} in the last {int(RECENT_TTL // 60)} minutes. Bus traffic "
            f"is ephemeral — ask the sender to resend, or have them put durable content in the "
            f"graph instead.")

    # ── registration ─────────────────────────────────────────────────────────────
    def mint_ticket(self, label: str, meta: Optional[dict] = None) -> dict:
        """Create (or re-use) a peer identity and hand back a single-use connect ticket."""
        label = (label or "agent").strip()[:64]
        with self._guard:
            peer_id = self._by_label.get(label)
            if peer_id is None or peer_id not in self._peers:
                peer_id = ulid()
                self._peers[peer_id] = _Peer(peer_id, label, meta)
                self._by_label[label] = peer_id
            elif meta:
                self._peers[peer_id].meta.update(meta)
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = (peer_id, _now() + TICKET_TTL)
            self._sweep_tickets()
        return {"ticket": ticket, "peer_id": peer_id, "label": label,
                "expires_in": int(TICKET_TTL)}

    def redeem(self, ticket: str) -> Optional[_Peer]:
        """Burn a ticket and return its peer. Single use: a replayed ticket is refused."""
        with self._guard:
            got = self._tickets.pop(ticket, None)
            if got is None:
                return None
            peer_id, expires = got
            if _now() > expires:
                return None
            return self._peers.get(peer_id)

    def _sweep_tickets(self) -> None:
        now = _now()
        for t, (_, exp) in list(self._tickets.items()):
            if now > exp:
                self._tickets.pop(t, None)

    # ── lookup ───────────────────────────────────────────────────────────────────
    def peer(self, ref: str) -> Optional[_Peer]:
        """Resolve by peer_id or by label, so agents can address each other by name."""
        if ref in self._peers:
            return self._peers[ref]
        pid = self._by_label.get(ref)
        return self._peers.get(pid) if pid else None

    def peers(self, *, online_only: bool = False) -> list:
        # Drop ghosts first: a peer that is offline AND has nothing queued is not coming back to
        # anything, and listing it invites a sender to address a label that will never read.
        with self._guard:
            for _pid, p in list(self._peers.items()):
                if not p.online and not p.queue:
                    self._forget_locked(p)
            out = [p.public() for p in self._peers.values() if p.online or not online_only]
        return sorted(out, key=lambda d: (not d["online"], d["peer"]))

    # ── connection lifecycle ─────────────────────────────────────────────────────
    async def attach(self, peer: _Peer, ws: Any) -> list:
        """Bind a live socket, replacing any previous one, and drain queued mail."""
        with self._guard:
            old = peer.ws
            peer.ws = ws
            peer.connected_at = _now()
        if old is not None:
            # A second connection for the same identity supersedes the first; closing the old
            # socket keeps presence honest instead of leaving a ghost peer online forever.
            try:
                await old.close(code=4409)
            except Exception:
                pass
        cutoff = _now() - QUEUE_TTL
        drained = [m for m in peer.queue if m.get("_t", 0) >= cutoff]
        peer.queue.clear()
        return drained

    async def detach(self, peer: _Peer, ws: Any) -> None:
        with self._guard:
            if peer.ws is ws:
                peer.ws = None
                peer.connected_at = None

    def forget(self, peer: _Peer, *, force: bool = False) -> None:
        """Remove a peer. `force` is for an explicit bus_disconnect; without it this is a sweep
        and must not touch a peer that is live or still holding mail."""
        with self._guard:
            self._forget_locked(peer, force=force)

    def _forget_locked(self, peer: _Peer, *, force: bool = False) -> None:
        # A sweep must never drop a peer holding a live socket: a connection can land between the
        # liveness check and this call, and forgetting it would orphan the socket — the peer would
        # look gone while its listener sat there receiving nothing. An explicit disconnect is a
        # different intent and says so with force=True.
        if not force and (peer.online or peer.queue):
            return
        self._peers.pop(peer.peer_id, None)
        if self._by_label.get(peer.label) == peer.peer_id:
            self._by_label.pop(peer.label, None)

    # ── delivery ─────────────────────────────────────────────────────────────────
    async def _deliver(self, peer: _Peer, frame: dict) -> bool:
        """Send now if connected, else queue. Returns True when it went out on the wire."""
        if peer.ws is not None:
            try:
                await peer.ws.send_text(json.dumps(frame, ensure_ascii=False))
                return True
            except Exception:
                # The socket died between the check and the send; fall through and queue so the
                # message survives a reconnect rather than evaporating.
                peer.ws = None
                peer.connected_at = None
        peer.queue.append({**frame, "_t": _now()})
        # Same reasoning per peer: maxlen caps the count, this caps the footprint.
        qbytes = sum(len(f.get("body") or "") for f in peer.queue)
        while qbytes > QUEUE_BYTES and len(peer.queue) > 1:
            qbytes -= len(peer.queue.popleft().get("body") or "")
        return False

    async def send(self, sender: str, to: str, body: str,
                   kind: str = "message", data: Optional[dict] = None) -> dict:
        if len(body) > MAX_BODY:
            raise BusError(f"body is {len(body)} bytes; cap is {MAX_BODY}. "
                           f"Upload large payloads as an artifact and send the digest.")
        target = self.peer(to)
        if target is None:
            known = [p.label for p in self._peers.values()]
            raise BusError(f"no peer {to!r}; connected peers: {known or '(none)'}")
        frame = {"v": PROTOCOL_VERSION, "type": kind, "id": ulid(), "from": sender,
                 "to": target.label, "room": None, "body": body, "ts": _iso()}
        if data:
            frame["data"] = data
        self._remember(frame)
        live = await self._deliver(target, frame)
        return {"id": frame["id"], "to": target.label, "delivered": live,
                "queued": not live,
                "note": None if live else "peer offline; queued for reconnect"}

    async def broadcast(self, sender: str, body: str, room: str = "lobby",
                        data: Optional[dict] = None) -> dict:
        if len(body) > MAX_BODY:
            raise BusError(f"body is {len(body)} bytes; cap is {MAX_BODY}")
        frame = {"v": PROTOCOL_VERSION, "type": "broadcast", "id": ulid(), "from": sender,
                 "to": None, "room": room, "body": body, "ts": _iso()}
        if data:
            frame["data"] = data
        self._remember(frame)
        sent = queued = 0
        for p in list(self._peers.values()):
            if p.label == sender or room not in p.rooms:
                continue
            if await self._deliver(p, frame):
                sent += 1
            else:
                queued += 1
        return {"id": frame["id"], "room": room, "delivered": sent, "queued": queued}

    async def presence(self, peer: _Peer, event: str) -> None:
        """Tell the room when someone joins or leaves, so agents can react to arrivals."""
        frame = {"v": PROTOCOL_VERSION, "type": "presence", "id": ulid(),
                 "event": event, "peer": peer.label, "ts": _iso()}
        for p in list(self._peers.values()):
            if p.peer_id != peer.peer_id and p.online:
                await self._deliver(p, frame)


# One hub per project, created lazily; bus state is per-process by design.
_hubs: Dict[str, Hub] = {}

# The event loop that owns the WebSocket connections. MCP tool bodies run on worker threads, so a
# send issued from a tool has to be scheduled back onto this loop — writing to a socket from
# another thread is not safe. Set once at application startup.
_LOOP: Any = None


def set_loop(loop: Any) -> None:
    global _LOOP
    _LOOP = loop


def hub_for(project_name: str) -> Hub:
    h = _hubs.get(project_name)
    if h is None:
        h = _hubs[project_name] = Hub()
    return h


async def websocket_endpoint(ws: Any, project_name: str) -> None:
    """Serve one agent connection for the lifetime of its socket.

    The client never sends anything meaningful — this is a one-way push channel — but we read in a
    loop anyway so that a closed socket is noticed promptly rather than only when the next message
    happens to be sent to it.
    """
    hub = hub_for(project_name)
    ticket = ws.query_params.get("ticket", "")
    peer = hub.redeem(ticket)
    if peer is None:
        # 4401 is in the private range; the close code surfaces to Monitor so the agent sees WHY.
        await ws.close(code=4401)
        return

    await ws.accept()
    queued = await hub.attach(peer, ws)
    await ws.send_text(json.dumps({
        "v": PROTOCOL_VERSION, "type": "hello", "peer": peer.label, "peer_id": peer.peer_id,
        "peers": [p["peer"] for p in hub.peers(online_only=True) if p["peer"] != peer.label],
        "queued": len(queued), "ts": _iso(),
    }, ensure_ascii=False))
    for m in queued:
        m.pop("_t", None)
        await ws.send_text(json.dumps(m, ensure_ascii=False))
    await hub.presence(peer, "connected")

    try:
        while True:
            # Heartbeat: if nothing arrives within the window, ping. A dead peer fails here and
            # we drop it, which is what keeps `bus_peers` honest without a TTL sweeper.
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=HEARTBEAT)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"v": PROTOCOL_VERSION, "type": "ping",
                                                   "ts": _iso()}))
                except Exception:
                    break
            except Exception:
                break
    finally:
        await hub.detach(peer, ws)
        await hub.presence(peer, "disconnected")
