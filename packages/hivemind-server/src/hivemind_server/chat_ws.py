"""Live notifications for canonical chat addresses; no legacy bus queues or recent cache."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import bus_ws
from .chat import ChatStore, StableAddress, stable_identity
from .db import Database, Invalid
from .identity import Identity, IdentityStore


log = logging.getLogger(__name__)
KEY_TTL = 7 * 86400


class CanonicalHub:
    """A socket registry keyed by identity *components*, never by an ambiguous display label."""

    def __init__(self, scope: str = "default"):
        self.scope = scope
        self._sessions: dict[tuple[str, str, str, str], Any] = {}
        self._credentials: dict[tuple[str, str, str, str], tuple[str, IdentityStore]] = {}
        self._guard = threading.RLock()

    def mint_key(self, parts: tuple[str, str, str, str], credential_hash: str) -> str:
        user, device, client, session = parts
        stable_identity(Identity(user, device), client, session)
        if not isinstance(credential_hash, str) or len(credential_hash) != 64:
            raise Invalid("chat socket key requires a bound user/device credential")
        exp = int(time.time() + KEY_TTL)
        payload = bus_ws._b64(json.dumps([*parts, credential_hash], separators=(",", ":")).encode())
        body = f"{payload}.{exp}"
        sig = hmac.new(bus_ws._secret(self.scope), body.encode(), hashlib.sha256).digest()
        return f"hk2.{body}.{bus_ws._b64(sig)}"

    def verify_key(self, key: str) -> tuple[tuple[str, str, str, str], str] | None:
        try:
            scheme, encoded, exp, signed = key.split(".")
            if scheme != "hk2" or time.time() > int(exp):
                return None
            want = hmac.new(bus_ws._secret(self.scope), f"{encoded}.{exp}".encode(),
                            hashlib.sha256).digest()
            if not hmac.compare_digest(want, bus_ws._unb64(signed)):
                return None
            parts = json.loads(bus_ws._unb64(encoded))
            if not isinstance(parts, list) or len(parts) != 5 or not isinstance(parts[4], str) \
                    or len(parts[4]) != 64:
                return None
            return stable_identity(Identity(parts[0], parts[1]), parts[2], parts[3]), parts[4]
        except (AttributeError, Invalid, TypeError, ValueError, UnicodeDecodeError):
            return None

    async def attach(self, parts: tuple[str, str, str, str], ws: Any, *,
                     credential_hash: str = "", identities: IdentityStore | None = None) -> None:
        with self._guard:
            prior = self._sessions.get(parts)
            self._sessions[parts] = ws
            if identities is not None:
                self._credentials[parts] = (credential_hash, identities)
            else:
                self._credentials.pop(parts, None)
        if prior is not None and prior is not ws:
            try:
                await prior.close(code=4409)
            except Exception:
                pass

    def detach(self, parts: tuple[str, str, str, str], ws: Any) -> None:
        with self._guard:
            if self._sessions.get(parts) is ws:
                self._sessions.pop(parts, None)
                self._credentials.pop(parts, None)

    def online(self, stable: StableAddress, session_id: str | None = None) -> bool:
        with self._guard:
            return any(parts[:3] == stable and (session_id is None or parts[3] == session_id)
                       for parts in self._sessions)

    def online_addresses(self) -> set[StableAddress]:
        """One stable address per attached listener, without scanning stored idle sessions."""
        with self._guard:
            return {parts[:3] for parts in self._sessions}

    async def notify(self, stable: StableAddress, frame: dict) -> int:
        """Best-effort live push; callers have already persisted the message."""
        if "body" in frame:
            raise Invalid("canonical notification must use a bounded preview, not message body")
        with self._guard:
            sessions = [(parts, ws, self._credentials.get(parts)) for parts, ws in self._sessions.items()
                        if parts[:3] == stable]
        sent = 0
        for parts, ws, credential in sessions:
            try:
                if credential is not None and not credential[1].active_credential(
                        parts[0], parts[1], credential[0]):
                    self.detach(parts, ws)
                    await ws.close(code=4401)
                    continue
                await ws.send_text(json.dumps(frame, ensure_ascii=False))
                sent += 1
            except Exception:
                self.detach(parts, ws)
        return sent


_hubs: dict[str, CanonicalHub] = {}
_hubs_lock = threading.RLock()


def hub_for(project_dir: Path) -> CanonicalHub:
    scope = os.path.realpath(project_dir)
    with _hubs_lock:
        if scope not in _hubs:
            _hubs[scope] = CanonicalHub(scope)
        return _hubs[scope]


async def websocket_endpoint(ws: Any, project_name: str, project_dir: Path, *,
                             require_auth: bool = True, identities: IdentityStore,
                             db: Database) -> None:
    """Recheck project ACL while connected, and refuse invalid credentials before accept."""
    hub = hub_for(project_dir)
    verified = hub.verify_key(ws.query_params.get("key", ""))
    if verified is None:
        await ws.close(code=4401)
        return
    parts, fingerprint = verified
    def authorized() -> bool:
        return identities.active_credential(parts[0], parts[1], fingerprint) and bus_ws.may_access(
            parts[0], project_dir, project_name, require_auth=require_auth)

    if not authorized():
        await ws.close(code=4401)
        return
    presence = ChatStore(db)
    await asyncio.to_thread(presence.touch, parts[:3], parts[3])
    await ws.accept()
    await hub.attach(parts, ws, credential_hash=fingerprint, identities=identities)
    await ws.send_text(json.dumps({"v": 2, "type": "hello", "peer": "-".join(parts),
                                   "address": parts[:3], "ts": bus_ws._iso()}))
    deadline = time.monotonic() + bus_ws.HEARTBEAT
    try:
        while True:
            if time.monotonic() >= deadline:
                deadline = time.monotonic() + bus_ws.HEARTBEAT
                if not authorized():
                    await ws.close(code=4401)
                    break
                # A still-connected, reauthorized listener is active even when chat is quiet.
                # Refresh before expiry cleanup so online agents never disappear at 24 hours.
                await asyncio.to_thread(presence.touch, parts[:3], parts[3])
            try:
                await asyncio.wait_for(ws.receive_text(),
                                       timeout=min(bus_ws.HEARTBEAT,
                                                   max(0.0, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"v": 2, "type": "ping", "ts": bus_ws._iso()}))
                except Exception:
                    break
            except Exception:
                break
    finally:
        hub.detach(parts, ws)
