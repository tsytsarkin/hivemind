"""`hivemind bus listen` — the process Claude Code's Monitor runs.

Monitor's own `ws` source refuses private addresses, and the Hivemind server lives on a LAN
address, so the agent cannot point Monitor at the server directly. A subprocess has no such
policy: this connects to the server's WebSocket and prints one line per frame, which Monitor
turns into a notification.

Two constraints shape the output format:

* Claude Code clips a notification at roughly 512 characters. A long body must not push the
  header — who sent it, and its id — off the end, so the body is capped. Because that truncation
  happens here — with the whole frame in hand — every message is first appended verbatim to
  `~/.hivemind/bus-inbox.jsonl`. The remainder used to be dropped on the floor, leaving
  `bus_message("<id>")` as its only route, which resolves only if the reading host exposes that
  tool; when it does not, an agent answers a message having read a ~300-character preview.
* The sender's label and body are attacker-controlled in the sense that any agent can set them,
  and an LLM reads the result as text. Control characters are stripped and the delimiters used by
  the header are removed from the label, so a peer cannot forge a second header or inject a
  trailing directive that looks like it came from the bus itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from typing import Optional

BODY_CAP = 300            # leaves room for the prefix AND the "fetch the rest" pointer
INBOX_HINT_CAP = 64       # the inbox path shares the line's budget with the body
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


def default_inbox() -> str:
    """Where a listener keeps every message it received.

    A fixed $HOME path, for the same reason the plugin's listener is one: a Monitor command runs in
    a plain shell where no plugin variable is set. The skill tells every agent this exact path.
    Resolved per call rather than at import so a changed HOME is honoured.
    """
    return os.path.join(os.path.expanduser("~"), ".hivemind", "bus-inbox.jsonl")


def prepare_inbox(path) -> Optional[str]:
    """Create the inbox's directory once, at startup, so appending a frame stays a plain append.

    Returns the path to record to, or None when the location cannot be written — keeping a local
    copy is best-effort and never worth refusing to carry messages over.
    """
    try:
        parent = os.path.dirname(os.path.abspath(str(path)))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Create it now, owner-only: peer bodies are coordination traffic between agents and have
        # no business being world-readable on a shared box. Creating it here also means an
        # unusable location is discovered at startup, where it can be reported once, rather than
        # silently per message. The mode applies to a file we create; an existing one is left as
        # the operator set it.
        os.close(os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600))
        return str(path)
    except OSError:
        return None


def _inbox_hint(inbox) -> str:
    """How the inbox is named inside a notification: bounded, because it shares the line's budget
    with the body, and cleaned, because one frame must stay one line."""
    s = _clean(str(inbox))
    home = os.path.expanduser("~")
    if home and s.startswith(home + os.sep):
        s = "~" + s[len(home):]
    return s if len(s) <= INBOX_HINT_CAP else "…" + s[-(INBOX_HINT_CAP - 1):]


def _record(frame: dict, inbox) -> bool:
    """Append the whole frame as one JSON line; returns whether it landed.

    ensure_ascii is deliberate: a JSONL reader splits on line breaks, and Python's own
    splitlines() breaks on U+2028/U+2029 — which a peer could put in a body to forge a second
    record. Escaping every non-ASCII character makes one frame one line by construction.
    """
    try:
        line = json.dumps(frame, ensure_ascii=True)
    except (TypeError, ValueError):
        return False
    try:
        with open(str(inbox), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False        # silent: a message that printed but was not recorded beats neither


def render(frame: dict, inbox=None) -> Optional[str]:
    """One frame -> one notification line, or None for frames an agent should not be woken for.

    Recording is a side effect on purpose. This is the one place that already knows a frame is
    worth waking an agent for and holds the body it is about to abbreviate, and knowing whether
    the append landed is what lets the pointer name the local copy only when there is one.
    `inbox=None` records nothing: the default path belongs to a listener's startup, not to any
    caller that passes a frame through for inspection.
    """
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
        kept = _record(frame, inbox) if inbox else False
        if len(body) > BODY_CAP:
            # The notification is clipped around 512 chars, so a long body cannot travel in it.
            # Send a pointer instead: to the line just appended to the local inbox, and to the id,
            # which resolves to the full text via bus_message over the normal tool channel.
            head = f'[hivemind msg={mid} from="{who}"{scope} chars={len(body)}]'
            if kept:
                # Two routes, because they fail differently: the local file is always readable but
                # only on this machine, and bus_message needs the host to expose that tool.
                tail = f'… full text: grep {mid} {_inbox_hint(inbox)} · or bus_message("{mid}")'
            else:
                tail = f'… bus_message("{mid}") for the rest'
            room = max(0, 500 - len(head) - len(tail) - 2)
            return f"{head} {body[:min(BODY_CAP, room)]}{tail}"
        return f'[hivemind msg={mid} from="{who}"{scope}] {body}'
    if kind == "error":
        return f"[hivemind bus] error: {_clean(frame.get('message',''))[:200]}"
    return None


async def _once(url: str, inbox=None) -> str:
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
            line = render(frame, inbox)
            if line:
                print(line, flush=True)
    return "closed"


async def listen(url: str, *, retry: bool = True, remint=None, inbox=None) -> int:
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
            await _once(current, inbox)
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


def run_listen(url: str, retry: bool = True, remint=None, inbox=None) -> int:
    resolved = prepare_inbox(inbox or default_inbox())
    if resolved is None:
        # Said once, at startup, rather than per message: an agent that is going to be pointed at
        # `bus_message` alone for every long body should know why.
        print(f"[hivemind bus] cannot open a local inbox at {inbox or default_inbox()}; "
              "messages will print but not be kept", flush=True)
    try:
        return asyncio.run(listen(url, retry=retry, remint=remint, inbox=resolved))
    except KeyboardInterrupt:
        return 0
