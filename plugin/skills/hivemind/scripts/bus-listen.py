#!/usr/bin/env python3
"""Hivemind bus listener — the process Claude Code's Monitor runs. No dependencies.

Why this exists as a standalone script rather than the `hivemind bus listen` CLI: a machine that
installed the Claude Code plugin has the MCP tools and nothing else. The CLI ships in the separate
`hivemind-client` package and needs a third-party `websockets` dependency on top, so a plugin-only
agent calling bus_connect got a command its shell could not find. This file is stdlib-only and
runs on any python3 >= 3.7, so the plugin alone is enough to join the bus.

Two other constraints shape it:

* Monitor's own `ws` source refuses private addresses, and the server lives on a LAN address, so
  the agent cannot point Monitor at the server directly. A subprocess carries no such policy:
  this prints one line per frame and Monitor turns each line into a notification.
* A notification is clipped at roughly 512 characters, so a long body must not push the header —
  who sent it, and its id — off the end. The body is capped and the id is how the rest is fetched,
  via the bus_message tool.

Usage (bus_connect returns the exact command):
    python3 bus-listen.py --url ws://host:8787/p/default/bus/ws --key hk1....
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlsplit

BODY_CAP = 300             # leaves room for the prefix AND the "fetch the rest" pointer
RECONNECT_MIN = 0.25
RECONNECT_MAX = 8.0
JITTER = 0.2
MAX_FRAME = 2 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"    # RFC 6455 handshake constant

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ── rendering: must stay byte-identical to hivemind.bus.render (test_listener_parity) ─────────
def _clean(s):
    s = _ANSI.sub("", s or "")
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _CTRL.sub("", s)


def _label(s):
    """A label is rendered inside the header, so it must not contain the header's delimiters."""
    return _clean(s).replace("]", "").replace("[", "").replace('"', "")[:48] or "?"


def render(frame):
    """One frame -> one notification line, or None for frames an agent should not be woken for."""
    kind = frame.get("type")
    if kind in ("ping", "pong"):
        return None
    if kind == "hello":
        peers = frame.get("peers") or []
        n = frame.get("queued") or 0
        extra = ", %d queued" % n if n else ""
        return ("[hivemind bus] connected as %s; peers online: %s%s"
                % (_label(frame.get("peer", "")),
                   ", ".join(_label(p) for p in peers) or "none", extra))
    if kind == "presence":
        return "[hivemind bus] %s %s" % (_label(frame.get("peer", "")), frame.get("event", "?"))
    if kind in ("message", "broadcast"):
        body = _clean(frame.get("body", ""))
        mid = str(frame.get("id", ""))[-8:]
        who = _label(frame.get("from", "?"))
        scope = " room=%s" % _label(frame.get("room") or "") if kind == "broadcast" else ""
        if len(body) > BODY_CAP:
            head = '[hivemind msg=%s from="%s"%s chars=%d]' % (mid, who, scope, len(body))
            tail = '… bus_message("%s") for the rest' % mid
            room = max(0, 500 - len(head) - len(tail) - 2)
            return "%s %s%s" % (head, body[:min(BODY_CAP, room)], tail)
        return '[hivemind msg=%s from="%s"%s] %s' % (mid, who, scope, body)
    if kind == "error":
        return "[hivemind bus] error: %s" % _clean(frame.get("message", ""))[:200]
    return None


# ── a minimal RFC 6455 client: text frames in, pongs out ──────────────────────────────────────
class WSError(Exception):
    pass


class WSRefused(WSError):
    """The server rejected the credential; retrying the same one is pointless."""


def _connect(url, timeout=15):
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    host = parts.hostname
    if not host:
        raise WSError("no host in %r" % url)
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)

    nonce = base64.b64encode(os.urandom(16)).decode()
    hostport = host if port in (80, 443) else "%s:%d" % (host, port)
    req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
           % (path, hostport, nonce))
    sock.sendall(req.encode())

    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("server closed during handshake")
        raw += chunk
        if len(raw) > 65536:
            raise WSError("handshake response too large")
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101" not in status:
        # 4401 is what the server closes with for a bad credential, but a rejected *handshake*
        # surfaces as an HTTP status instead, so both paths have to be recognised as refusals.
        if any(c in status for c in (" 401", " 403", " 404")):
            raise WSRefused(status.strip())
        raise WSError("handshake failed: %s" % status.strip())
    want = base64.b64encode(hashlib.sha1((nonce + GUID).encode()).digest()).decode()
    if want.lower() not in head.decode("latin-1").lower():
        raise WSError("bad Sec-WebSocket-Accept: not a websocket peer")
    sock.settimeout(None)
    return sock, rest


def _send(sock, opcode, payload=b""):
    """Client frames must be masked (RFC 6455 §5.3); only ever short control frames here."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", n)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSError("connection closed")
        buf += chunk
    return buf


def _frames(sock, pending=b""):
    """Yield complete text messages, reassembling continuations and answering pings."""
    buf = bytearray(pending)

    def take(n):
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    frag_op, frag = None, bytearray()
    while True:
        b0, b1 = take(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        if b1 & 0x80:
            raise WSError("server sent a masked frame")
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", take(8))[0]
        if length > MAX_FRAME:
            raise WSError("frame of %d bytes exceeds the cap" % length)
        payload = take(length) if length else b""

        if opcode == 0x8:                            # close
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            if code == 4401:
                raise WSRefused("server closed 4401: credential rejected")
            raise WSError("server closed (%d)" % code)
        if opcode == 0x9:                            # ping -> pong, keeps the link alive
            _send(sock, 0xA, payload)
            continue
        if opcode == 0xA:                            # pong
            continue
        if opcode == 0x2:                            # binary is not part of this protocol
            continue
        if opcode == 0x0:                            # continuation
            if frag_op is None:
                raise WSError("continuation with nothing to continue")
            frag += payload
        elif opcode == 0x1:                          # text
            if frag_op is not None:
                raise WSError("new text frame inside a fragmented message")
            frag_op, frag = 0x1, bytearray(payload)
        else:
            raise WSError("unexpected opcode 0x%x" % opcode)
        if fin and frag_op is not None:
            try:
                yield frag.decode("utf-8")
            except UnicodeDecodeError:
                pass
            frag_op, frag = None, bytearray()


def _once(url):
    sock, rest = _connect(url)
    try:
        for raw in _frames(sock, rest):
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            line = render(frame)
            if line:
                print(line, flush=True)
    finally:
        try:
            _send(sock, 0x8, struct.pack("!H", 1000))
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="stream hivemind bus messages, one line per frame")
    ap.add_argument("--url", required=True, help="ws:// or wss:// bus endpoint")
    ap.add_argument("--key", help="reusable listen key from bus_connect (survives reconnects)")
    ap.add_argument("--ticket", help="single-use ticket; cannot survive a reconnect")
    ap.add_argument("--once", action="store_true", help="do not reconnect")
    args = ap.parse_args(argv)

    url = args.url
    if args.key or args.ticket:
        sep = "&" if "?" in url else "?"
        url += sep + ("key=%s" % args.key if args.key else "ticket=%s" % args.ticket)

    backoff = RECONNECT_MIN
    while True:
        try:
            _once(url)
            backoff = RECONNECT_MIN                 # a clean close is not a failure
        except WSRefused as e:
            # A listen key is reusable, so a refusal means expired or revoked — not a blip.
            print("[hivemind bus] refused (%s); run bus_connect for a fresh key" % e, flush=True)
            return 2
        except (WSError, OSError, ssl.SSLError) as e:
            print("[hivemind bus] disconnected (%s); reconnecting" % type(e).__name__, flush=True)
        if args.once:
            return 0
        delay = backoff + random.uniform(-backoff * JITTER, backoff * JITTER)
        time.sleep(max(0.05, delay))
        backoff = min(backoff * 2, RECONNECT_MAX)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
