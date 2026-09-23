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
* **The credential names its user, and the project ACL is re-checked against it.** Signing only a
  label made a listen key a bearer credential for seven days: a member removed from a private
  project kept receiving until it expired. Both credentials now carry the minting user inside the
  signature, and `websocket_endpoint` re-runs `projects_meta.can_access` — at the handshake, and
  again while the socket is open, since a listener holds one socket for days and would otherwise
  never be re-checked. This is the only place the ACL can be enforced for the bus:
  `app.ProjectAuthMiddleware` returns early for non-HTTP scopes.
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
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from collections import deque
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Optional

from .identity import Identity
from .ids import ulid
from .projects_meta import can_access, load_with_problem

log = logging.getLogger(__name__)

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
LISTEN_KEY_TTL = 7 * 86400   # a listener's own credential: long-lived, reusable, revocable

# The user recorded in a credential minted with no identity in scope (auth off, or a test driving
# the Hub directly). The colon is load-bearing: identity.validate_username forbids it, and
# project_tools.share validates every member, so this can never equal a real owner or member — such
# a credential reaches a shared project and no private one.
UNKNOWN_USER = "unknown:bus"

# Per-project HMAC secret for listen keys, loaded from the project directory at startup.
# Keyed by _scope(), never by project name — see there.
_SECRETS: Dict[str, bytes] = {}


def _scope(project_dir: Path) -> str:
    """The key under which a project's hub and signing secret are held.

    The project DIRECTORY, not its name. Nothing enforces one app per process — the tests build
    several, and an embedding may too — and two deployments can hold a same-named project. Keyed by
    name, the second build_app to run would overwrite the first's signing secret and both would
    share one hub, so a listen key minted against one deployment would verify against the other's
    and admit its holder to that bus. Same hazard, and the same shape of fix, as the one
    projects_meta._CACHE applies to its own name collision (that one keys on the literal
    project.json path, not on this; the two need not agree, they only need to be per-directory).

    realpath, not str(): two spellings of ONE directory — a relative path, or a data root reached
    through a symlink — would split the hub and the secret in two, so a message sent by a tool
    would land on a different hub from the socket that should have received it. Every caller
    happens to pass the same registry Project.dir today, which makes that safe by accident; this
    makes it safe by construction.
    """
    return os.path.realpath(project_dir)


# The address the CURRENT caller used to reach us, captured per request by the ASGI middleware.
#
# A listener has to be handed a URL it can actually open, and the server cannot work that out from
# its own configuration: it binds 0.0.0.0 so that both the LAN and the mesh reach it, and
# HIVEMIND_PUBLIC_URL therefore reads `http://0.0.0.0:8787` unless a deployment overrides it.
# Handing that to a listener produced exactly what it says — ConnectionRefusedError against
# 0.0.0.0. The caller's own Host header is the one address known to work, since the request just
# arrived over it.
_ORIGIN: ContextVar = ContextVar("hivemind_origin", default="")


def set_origin(origin: str) -> None:
    _ORIGIN.set(origin)


def current_origin() -> str:
    return _ORIGIN.get()


def register_secret(project_dir: Path) -> bytes:
    """Load (or create) the secret that signs this project's listen keys.

    It lives on disk rather than in memory so a key stays valid across a server restart. That is
    the whole point of the key: a listener that reconnects after the server bounced must get back
    in on its own, without an agent noticing and re-running bus_connect.
    """
    scope, path = _scope(project_dir), project_dir / "bus_secret"
    try:
        secret = path.read_bytes()
        if len(secret) >= 32:
            _SECRETS[scope] = secret
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(secret)
    os.chmod(tmp, 0o600)          # the signing key for bus access: owner-only
    tmp.replace(path)
    _SECRETS[scope] = secret
    return secret


def _secret(scope: str) -> bytes:
    """Fall back to a process-lifetime secret when no project dir was registered (unit tests)."""
    got = _SECRETS.get(scope)
    if got is None:
        got = _SECRETS[scope] = secrets.token_bytes(32)
    return got


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


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

    def __init__(self, scope: str = "default") -> None:
        # Opaque key, shared with _SECRETS: a directory path in production (see _scope).
        self.scope = scope
        self._peers: Dict[str, _Peer] = {}        # peer_id -> _Peer
        self._by_label: Dict[str, str] = {}       # label -> peer_id (last writer wins)
        self._tickets: Dict[str, tuple] = {}      # ticket -> (peer_id, expires_at, user)
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
            f"no message {message_id!r} in the last {int(RECENT_TTL // 60)} minutes. If your own "
            f"listener received it, grep the id in ~/.hivemind/bus-inbox.jsonl (and its .1): that "
            f"copy has no time limit, only a size one. Otherwise bus traffic is ephemeral: ask "
            f"the sender to resend, or have them put durable content in the graph instead.")

    # ── registration ─────────────────────────────────────────────────────────────
    def mint_ticket(self, label: str, meta: Optional[dict] = None,
                    user: Optional[str] = None) -> dict:
        """Create (or re-use) a peer identity and hand back a single-use connect ticket.

        The ticket records who minted it for the same reason the listen key signs it: the handshake
        is the only place the bus ACL is enforced, and it has to know whose access to check. A
        ticket needs no signature for that — it is a random token this process holds in memory and
        burns on first use, so its user cannot be edited by whoever carries it.
        """
        label = (label or "agent").strip()[:64]
        with self._guard:
            peer_id = self._ensure_peer(label, meta).peer_id
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = (peer_id, _now() + TICKET_TTL, user or UNKNOWN_USER)
            self._sweep_tickets()
        return {"ticket": ticket, "peer_id": peer_id, "label": label,
                "expires_in": int(TICKET_TTL)}

    def mint_listen_key(self, label: str, meta: Optional[dict] = None,
                        user: Optional[str] = None) -> dict:
        """Hand back a reusable credential a listener can reconnect with on its own.

        A ticket is single-use and lasts a minute, which is right for one handshake and wrong for a
        process that must survive a dropped link or a server restart. This is signed rather than
        stored: verification needs no table, so it still works after the process that minted it is
        gone. It carries no authority beyond joining the bus under this label, expires, and is
        revocable wholesale by deleting the project's bus_secret.

        The minting user is INSIDE the signature, not beside it: the handshake re-checks the
        project ACL against that user (see authorize_key), and a user field the holder could edit
        would let any key holder nominate the owner of the project it is aimed at.
        """
        label = (label or "agent").strip()[:64]
        user = user or UNKNOWN_USER
        with self._guard:
            self._ensure_peer(label, meta)
        exp = int(_now() + LISTEN_KEY_TTL)
        body = f"{_b64(label.encode())}.{_b64(user.encode())}.{exp}"
        sig = hmac.new(_secret(self.scope), body.encode(), hashlib.sha256).digest()
        return {"listen_key": f"hk1.{body}.{_b64(sig)}", "label": label, "user": user,
                "expires_in": LISTEN_KEY_TTL}

    def verify_key(self, key: str) -> Optional[tuple]:
        """Check a listen key's signature and expiry; return (label, user). NO SIDE EFFECTS.

        Split from redeem_key because the project ACL runs between the two, and nothing that has
        not passed it may touch the registry. The registry is keyed by LABEL and the label is
        chosen by whoever minted the key, so a write from an unauthorized caller lands on a peer it
        named rather than one it owns — see authorize_key.

        A key in the pre-user format has four fields, not five, so it fails to unpack and is
        refused. That is deliberate: such a key names nobody, so there is no access to re-check and
        honouring it would leave exactly the hole this closes. Its holder re-runs bus_connect.
        """
        try:
            scheme, label_b64, user_b64, exp_s, sig_b64 = key.split(".")
        except (ValueError, AttributeError):
            return None
        if scheme != "hk1":
            return None
        body = f"{label_b64}.{user_b64}.{exp_s}"
        want = hmac.new(_secret(self.scope), body.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(want, _unb64(sig_b64)):
                return None
            if _now() > int(exp_s):
                return None
            label = _unb64(label_b64).decode()
            user = _unb64(user_b64).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        return label, user

    def admit(self, label: str) -> _Peer:
        """The peer for a connection that HAS passed the ACL, created on demand.

        Re-create on demand: after a restart the registry is empty, and refusing here would mean
        every listener stayed dead until a human noticed. Reached only from authorize_key, after
        can_access — that ordering is what keeps an unauthorized caller out of the registry.
        """
        with self._guard:
            return self._ensure_peer(label, None)

    def redeem_key(self, key: str) -> Optional[tuple]:
        """verify_key plus the peer it names. Callers that must gate on the ACL use the two halves
        separately (authorize_key); this is the whole handshake for an in-process caller."""
        got = self.verify_key(key)
        return None if got is None else (self.admit(got[0]), got[1])

    def _ensure_peer(self, label: str, meta: Optional[dict]) -> _Peer:
        """Get-or-create the peer for a label. Caller holds the guard."""
        peer_id = self._by_label.get(label)
        if peer_id is None or peer_id not in self._peers:
            peer_id = ulid()
            self._peers[peer_id] = _Peer(peer_id, label, meta)
            self._by_label[label] = peer_id
        elif meta:
            self._peers[peer_id].meta.update(meta)
        return self._peers[peer_id]

    def redeem(self, ticket: str) -> Optional[tuple]:
        """Burn a ticket and return (peer, user). Single use: a replayed ticket is refused."""
        with self._guard:
            got = self._tickets.pop(ticket, None)
            if got is None:
                return None
            peer_id, expires, user = got
            if _now() > expires:
                return None
            peer = self._peers.get(peer_id)
            return None if peer is None else (peer, user)

    def _has_pending_ticket(self, peer_id: str) -> bool:
        now = _now()
        return any(pid == peer_id and now <= exp for pid, exp, _u in self._tickets.values())

    def _sweep_tickets(self) -> None:
        now = _now()
        for t, (_, exp, _u) in list(self._tickets.items()):
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
        if not force and (peer.online or peer.queue or self._has_pending_ticket(peer.peer_id)):
            # A peer that has just called bus_connect is offline with an empty queue — exactly
            # what a ghost looks like — but its listener is about to redeem a ticket. Sweeping it
            # here made redeem() resolve to a deleted peer and the connection was refused, so a
            # bus_peers() call between connect and listen broke the connect.
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


# One hub per project, created lazily; bus state is per-process by design. Keyed by _scope().
_hubs: Dict[str, Hub] = {}

# The event loop that owns the WebSocket connections. MCP tool bodies run on worker threads, so a
# send issued from a tool has to be scheduled back onto this loop — writing to a socket from
# another thread is not safe. Set once at application startup.
_LOOP: Any = None


def set_loop(loop: Any) -> None:
    global _LOOP
    _LOOP = loop


def hub_for(project_dir: Path) -> Hub:
    scope = _scope(project_dir)
    h = _hubs.get(scope)
    if h is None:
        h = _hubs[scope] = Hub(scope)
    return h


def access_check(user: str, project_dir: Path, project_name: str,
                 require_auth: bool) -> tuple:
    """(allowed, problem) — may_access, plus WHY the metadata failed closed, for an operator log.

    One code path, so the two cannot drift; same split, and the same reason for it, as
    projects_meta.load_with_problem and app._note_metadata. The problem must never reach a caller:
    "this project's metadata is corrupt" tells them the project exists.
    """
    if not require_auth:
        return True, None
    meta, problem = load_with_problem(project_dir, project_name)
    return can_access(Identity(user=user, device="bus"), meta), problem


def may_access(user: str, project_dir: Path, project_name: str, *,
               require_auth: bool = True) -> bool:
    """Is this user allowed into this project *right now*?

    Re-read every call. projects_meta.load is mtime-stamped, so the common case is a stat() and a
    revocation takes effect on the very next check — there is no epoch to bump and nothing to
    invalidate. can_access is the one access rule every surface shares; this never second-guesses
    it. The identity is reconstructed from the credential rather than from a bearer token, which is
    why the user has to be inside the signature.

    `require_auth` is passed in, never inferred from "did the credential name anybody?", for the
    reason envelope.set_registry gives: with HIVEMIND_REQUIRE_AUTH=0 there is no credential naming
    a person, so app._authorize applies no ACL on any other surface, and a bus that failed closed
    on the same input would be the one thing such a deployment could not use. It defaults to the
    strict rule, so a caller that forgets the flag gets the check rather than the bypass.

    A `legacy:*` user is NOT flagged legacy here. can_access refuses a legacy identity whose
    project_scope is not this project, and the credential carries no scope to reconstruct, so
    flagging it would deny every legacy listener outright. The scope is enforced by the signature
    instead: a listen key is signed with one project's secret and verifies against no other. What
    is left over is nil — `legacy:` contains a colon, identity.validate_username forbids one and
    project_tools.share validates every member, so such a user is never an owner or a member and
    reaches shared projects only, exactly as can_access would have it.
    """
    return access_check(user, project_dir, project_name, require_auth)[0]


def authorize_key(hub: Hub, key: str, project_dir: Path, project_name: str, *,
                  require_auth: bool = True) -> Optional[tuple]:
    """Verify the key AND re-check the project ACL — a key outlives a revocation otherwise.

    Returns (peer, user), because authorising the handshake is not authorising the socket: the
    caller keeps the user so it can ask again while the connection is open.

    The peer is materialised only AFTER can_access, and a refusal writes nothing at all. That
    ordering is the whole defence, because peers are keyed by LABEL while authorization is keyed by
    USER and the label is chosen by whoever mints the credential: the two do not name the same
    principal, so any write from this path would land on a peer the caller merely named. Measured,
    when a refusal used to drop the peer it resolved: a revoked member holding a key minted under
    another agent's label destroyed that agent's queued mail, and — because forget(force=True) also
    clears the reprieve _forget_locked gives a peer whose listener has not attached yet — made that
    agent's own bus_connect ticket resolve to a deleted peer, refused 4401, replayable at will.

    Recording the minting user on _Peer and forgetting only "its own" peer does NOT fix that, and
    it was measured too: _ensure_peer is get-or-create by label, so whichever rule names the owner,
    the attacker just mints on the other side of the victim. Owner-on-create leaves the victim
    exposed when the attacker mints FIRST; letting an authenticated mint re-claim leaves them
    exposed when it mints LAST. Not writing at all is the only rule with no ordering in it.
    """
    got = hub.verify_key(key)
    if got is None:
        return None
    label, user = got
    if not may_access(user, project_dir, project_name, require_auth=require_auth):
        return None
    return hub.admit(label), user


def authorize_ticket(hub: Hub, ticket: str, project_dir: Path, project_name: str, *,
                     require_auth: bool = True) -> Optional[tuple]:
    """Burn the ticket AND re-check the ACL. Same rule as a key, on a much shorter fuse: a ticket
    is single-use and lasts `TICKET_TTL`, but the socket it opens lasts as long as any other.

    Nothing to undo on a refusal here: redeem() only looks a peer up, and the one it finds was
    created by the authenticated bus_connect that minted this ticket. A refused ticket is then
    exactly an expired one, which is how they were always treated.
    """
    got = hub.redeem(ticket)
    if got is None:
        return None
    peer, user = got
    if not may_access(user, project_dir, project_name, require_auth=require_auth):
        return None
    return peer, user


async def websocket_endpoint(ws: Any, project_name: str, project_dir: Path, *,
                             require_auth: bool = True) -> None:
    """Serve one agent connection for the lifetime of its socket.

    The client never sends anything meaningful — this is a one-way push channel — but we read in a
    loop anyway so that a closed socket is noticed promptly rather than only when the next message
    happens to be sent to it.

    This is also the ONLY place the project ACL is enforced for the bus: the ws route is registered
    at the Starlette level and app.ProjectAuthMiddleware returns early for non-HTTP scopes, so no
    middleware has looked at this caller. Hence the check here, and again below on the open socket.
    """
    hub = hub_for(project_dir)
    # Either credential works: a ticket (single-use, minted per connect) or a listen key (reusable,
    # so a listener reconnects by itself across drops and restarts). Both name their minting user,
    # and both are refused unless that user still passes the project ACL.
    key = ws.query_params.get("key", "")
    admitted = (authorize_key(hub, key, project_dir, project_name, require_auth=require_auth)
                if key else
                authorize_ticket(hub, ws.query_params.get("ticket", ""), project_dir, project_name,
                                 require_auth=require_auth))
    if admitted is None:
        # This refusal MUST stay pre-accept. uvicorn collapses any close sent before accept into a
        # bare 403 with an empty body and discards the code — every implementation does it
        # (websockets_sansio_impl, websockets_impl, wsproto_impl all reject with FORBIDDEN) — so the
        # 4401 below never reaches the client. That is the property we want, not a bug to fix: a bad
        # credential is then byte-identical to a project with no ws route at all.
        #
        # So do NOT call accept() first to make the code visible. An accepted-then-closed socket IS
        # distinguishable from an unmounted path, and since this endpoint is reached without a
        # bearer token that instantly hands an unauthenticated caller an existence oracle for
        # /p/<private>/bus/ws — the same oracle app.PROJECT_DENIED exists to remove on every other
        # path. 4401 (private range) is kept for the in-process/ASGI callers that do see it.
        await ws.close(code=4401)
        return

    peer, user = admitted
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
        # The handshake authorised ONE connection, and this socket then stays up for days: the
        # listener reconnects only on a blip or a restart, so a handshake-only check would in
        # practice almost never fire and a revoked member would keep receiving indefinitely.
        # Re-ask on an ABSOLUTE deadline instead — and never wait past it. The wait below is the
        # only thing that wakes this loop, so a full-HEARTBEAT timeout issued just after an inbound
        # frame would carry the loop well beyond `recheck_at`: a client that spoke at t+29.9 bought
        # itself a fresh 30 seconds and doubled its own window to 2×HEARTBEAT. A client that has
        # stopped cooperating is precisely the threat model for a revocation check, so the wait is
        # clamped to the time remaining. HEARTBEAT is then the upper bound on how long a revoked
        # listener keeps receiving, whatever it SENDS. Not "whatever it does": a client that stops
        # READING applies back-pressure to the send_text and close awaits below, and those are the
        # only awaits here the deadline does not clamp.
        recheck_at = _now() + HEARTBEAT
        while True:
            if _now() >= recheck_at:
                # Deadline first, so successive checks START exactly HEARTBEAT apart whatever
                # the check itself costs — that is the bound the comment above claims.
                recheck_at = _now() + HEARTBEAT
                allowed, problem = access_check(user, project_dir, project_name, require_auth)
                if not allowed:
                    if problem:
                        # Unreadable metadata denies like any other failure, but this close is
                        # TERMINAL for the listener (bus-listen.py prints and exits rather than
                        # retrying), so every agent on this project must re-run bus_connect once
                        # the file is fixed — they do not recover on their own the way an HTTP
                        # caller does. An operator has to be able to tell that from a real
                        # revocation, which is what app._note_metadata does for the HTTP path.
                        # Logged on eviction only, never on a refused handshake. Not because an
                        # outsider could flood it — they cannot reach it at all, since a bad
                        # signature returns before any metadata is read — but because eviction is
                        # BOUNDED at one line per socket already admitted, whereas nothing limits
                        # how often a credential holder retries a handshake, and each attempt would
                        # log another line for as long as the file stayed broken.
                        log.warning("bus: dropping peer %r on project %r: %s — failing closed; "
                                    "listeners must re-run bus_connect once this is fixed",
                                    peer.label, project_name, problem)
                    # Post-accept, so unlike the refusal above this close code DOES reach the
                    # client: the listener treats 4401 as terminal and tells its agent to re-run
                    # bus_connect. No oracle either — this caller was already admitted.
                    await ws.close(code=4401)
                    # Then drop the peer, rather than leaving it parked: detach() alone keeps it in
                    # the registry for as long as it holds queued mail, so an evicted label would
                    # stay listed by bus_peers and bus_send would keep telling senders "queued for
                    # reconnect" about someone whose reconnect can no longer be authorised.
                    #
                    # Void the mail and then use the ORDINARY sweep rule — deliberately not
                    # force=True. force bypasses two guards, and only one of them is the queue:
                    # the other reprieves a peer whose listener has not attached yet, and
                    # _forget_locked records the regression that reprieve exists to prevent. A
                    # label is caller-chosen at mint time, so this peer may be one another agent
                    # is mid-connect on; forcing past that would refuse ITS bus_connect. Voiding
                    # the queue first is what this eviction is entitled to do — a revoked peer
                    # must never drain one — and forget() then declines if a connect is in flight.
                    await hub.detach(peer, ws)
                    peer.queue.clear()
                    hub.forget(peer)
                    break
            # Heartbeat: if nothing arrives within the window, ping. A dead peer fails here and
            # we drop it, which is what keeps `bus_peers` honest without a TTL sweeper. The wait
            # never outlasts the re-check deadline above; see there.
            try:
                await asyncio.wait_for(ws.receive_text(),
                                       timeout=min(HEARTBEAT, max(0.0, recheck_at - _now())))
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
