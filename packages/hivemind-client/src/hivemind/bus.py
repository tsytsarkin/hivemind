"""`hivemind bus listen` — the process Claude Code's Monitor runs.

Monitor's own `ws` source refuses private addresses, and the Hivemind server lives on a LAN
address, so the agent cannot point Monitor at the server directly. A subprocess has no such
policy: this connects to the server's WebSocket and prints one line per frame, which Monitor
turns into a notification.

Two constraints shape the output format:

* Claude Code clips a notification at roughly 512 characters. A long body must not push the
  header — who sent it, and its id — off the end, so the body is capped and the id is how you
  retrieve the rest.
* The sender's label and body are attacker-controlled in the sense that any agent can set them,
  and an LLM reads the result as text. Control characters are stripped and the delimiters used by
  the header are removed from the label, so a peer cannot forge a second header or inject a
  trailing directive that looks like it came from the bus itself.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Optional

BODY_CAP = 300            # leaves room for the prefix AND the "fetch the rest" pointer
NOTIFICATION_BUDGET = 512 # measured clip point in Claude Code; a longer line loses its tail
RECONNECT_MIN = 0.25
RECONNECT_MAX = 8.0
JITTER = 0.2

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(s: str) -> str:
    """Strip escapes and fold newlines so one frame stays one line."""
    s = _ANSI.sub("", s or "")
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _CTRL.sub("", s)


def _label(s: str) -> str:
    """A label is rendered inside the header, so it must not contain the header's delimiters."""
    return _clean(s).replace("]", "").replace("[", "").replace('"', "")[:48] or "?"


def render(frame: dict) -> Optional[str]:
    """One frame -> one notification line, or None for frames an agent should not be woken for."""
    kind = frame.get("type")
    if kind in ("ping", "pong"):
        return None
    if kind == "hello":
        peers = frame.get("peers") or []
        n = frame.get("queued") or 0
        extra = f", {n} queued" if n else ""
        return (f"[hivemind bus] connected as {_label(frame.get('peer',''))}; "
                f"peers online: {', '.join(_label(p) for p in peers) or 'none'}{extra}")
    if kind == "presence":
        return f"[hivemind bus] {_label(frame.get('peer',''))} {frame.get('event','?')}"
    if kind in ("message", "broadcast"):
        body = _clean(frame.get("body", ""))
        mid = str(frame.get("id", ""))[-8:]
        who = _label(frame.get("from", "?"))
        scope = f" room={_label(frame.get('room') or '')}" if kind == "broadcast" else ""
        if len(body) > BODY_CAP:
            # The notification is clipped around 512 chars, so a long body cannot travel in it.
            # Send a pointer instead: the id resolves to the full text via bus_message, which
            # goes over the normal tool channel and has no such limit.
            head = f'[hivemind msg={mid} from="{who}"{scope} chars={len(body)}]'
            tail = f'… bus_message("{mid}") for the rest'
            room = max(0, 500 - len(head) - len(tail) - 2)
            return f"{head} {body[:min(BODY_CAP, room)]}{tail}"
        return f'[hivemind msg={mid} from="{who}"{scope}] {body}'
    if kind == "error":
        return f"[hivemind bus] error: {_clean(frame.get('message',''))[:200]}"
    return None


async def _once(url: str) -> str:
    """One connection. Returns why it ended, so the caller can decide whether to retry."""
    import websockets

    async with websockets.connect(url, open_timeout=15, max_size=2 * 1024 * 1024) as ws:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue                      # binary frames are not part of this protocol
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            line = render(frame)
            if line:
                print(line, flush=True)
    return "closed"


async def listen(url: str, *, retry: bool = True, remint=None) -> int:
    """Hold the connection for the life of the process, reconnecting through outages.

    Connect tickets are single-use, so a reconnect cannot replay the URL it was given: the second
    attempt would be refused and the listener would die on the first network blip — the exact
    failure mode of the v1 sidecar. `remint` mints a fresh ticket from the caller's API
    credentials, which is what makes this self-healing rather than one-shot.
    """
    backoff = RECONNECT_MIN
    current = url
    while True:
        refused = False
        try:
            await _once(current)
            backoff = RECONNECT_MIN          # a clean close is not a failure
        except Exception as e:
            msg = str(e)
            refused = any(code in msg for code in ("4401", "403", "401"))
            if refused and remint is None:
                print("[hivemind bus] connection refused and no credentials to re-mint a "
                      "ticket — call bus_connect for a fresh URL", flush=True)
                return 2
            if not refused:
                print(f"[hivemind bus] disconnected ({type(e).__name__}); reconnecting",
                      flush=True)
        if not retry:
            return 0
        if remint is not None:
            # Always re-mint: the ticket just used is spent whether the socket closed cleanly or
            # not, so reusing `current` would fail on the next attempt.
            try:
                current = remint()
            except Exception as e:
                print(f"[hivemind bus] cannot re-mint a ticket ({type(e).__name__}); retrying",
                      flush=True)
        delay = backoff + random.uniform(-backoff * JITTER, backoff * JITTER)
        await asyncio.sleep(max(0.05, delay))
        backoff = min(backoff * 2, RECONNECT_MAX)


def run_listen(url: str, retry: bool = True, remint=None) -> int:
    try:
        return asyncio.run(listen(url, retry=retry, remint=remint))
    except KeyboardInterrupt:
        return 0
