"""The local inbox: the listener keeps the message it was only able to preview.

A notification is clipped by the host at roughly 512 characters, so `render()` prints at most
`BODY_CAP` characters of the body and points at `bus_message("<id>")` for the rest. The truncation
is entirely client-side — the whole body is in hand at that moment — and dropping the remainder
left one route to it, a route that resolves only if the reading host happens to expose that tool.
When it does not, an agent answers a message having read a ~300-character preview.

So every `message`/`broadcast` frame is appended verbatim to a local JSONL file as it arrives, and
the pointer names that file alongside the tool. Recording is a side effect of `render()` on
purpose: it is the one place that already knows the frame is worth waking an agent for, and
knowing whether the append landed is what lets the pointer promise the local copy only when the
local copy exists.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
LISTENER = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "bus-listen.py"


def _load(path=LISTENER):
    spec = importlib.util.spec_from_file_location("bus_listen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _records(inbox):
    return [json.loads(l) for l in inbox.read_text().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _forget_torn_lines():
    """`_TORN` is module state that outlives a test, and the client module is imported once for
    the whole session. Paths here are unique per test, but leaving entries behind is how a test
    starts depending on the one before it."""
    yield
    _load()._TORN.clear()
    from hivemind import bus as client
    client._TORN.clear()


@pytest.fixture
def permissive_umask():
    """Every mode assertion below must run under a umask that would REVEAL a regression.

    The `os.open(..., 0o600)` decision is only visible if the ambient umask is not already
    masking those bits: restore the builtin `open(..., "a")` it replaced and the file comes out
    0644 at umask 022 — but 0600 at umask 077, where the test would pass while testing nothing.
    0 is the most permissive umask there is, so nothing here can be masked into passing.
    """
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture(params=["plugin", "client"])
def listener(request):
    """Both listeners must behave identically; a fix to one copy only is half a fix."""
    if request.param == "plugin":
        return _load()
    from hivemind import bus as client
    return client


@pytest.fixture
def render(listener):
    return listener.render


def _ids(path):
    return [r["id"] for r in _records(path)]


def _fill(listener, inbox, first, count, body="b" * 100):
    """Write `count` messages with sequential, sortable ids. Returns the ids used."""
    ids = ["%08d" % n for n in range(first, first + count)]
    for i in ids:
        listener.render({"type": "message", "id": i, "from": "p", "body": body}, inbox=inbox)
    return ids


def test_the_listener_persists_the_frame_it_prints(render, tmp_path):
    """The full body is in hand when the line is clipped; discarding it is the bug."""
    inbox = tmp_path / "bus-inbox.jsonl"
    frame = {"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "L" * 900}
    line = render(frame, inbox=inbox)
    assert len(line) <= 512
    rec = _records(inbox)[-1]
    assert rec["body"] == "L" * 900, "the whole body must survive locally"
    assert rec["id"] == "01ABCDEFGH"
    assert rec["from"] == "labbox", "the whole frame is kept, not just its body"


def test_the_pointer_names_the_local_inbox_not_only_a_tool(render, tmp_path):
    inbox = tmp_path / "i.jsonl"
    line = render({"type": "message", "id": "01ABCDEFGH", "from": "x", "body": "L" * 900},
                  inbox=inbox)
    assert "i.jsonl" in line, "the route that always works must be named"
    assert 'bus_message("ABCDEFGH")' in line, "and so must the one that reaches other hosts"
    assert "01ABCDEFGH"[-8:] in line


def test_a_short_message_is_persisted_too(render, tmp_path):
    """Otherwise the local record has holes exactly where the conversation was cheap."""
    inbox = tmp_path / "i.jsonl"
    line = render({"type": "message", "id": "01AB", "from": "x", "body": "short"}, inbox=inbox)
    assert _records(inbox)[-1]["body"] == "short"
    assert "bus_message" not in line, "nothing is missing from a short line; do not send for it"


def test_broadcasts_are_persisted(render, tmp_path):
    inbox = tmp_path / "i.jsonl"
    render({"type": "broadcast", "id": "01AB", "from": "x", "room": "lobby", "body": "all hands"},
           inbox=inbox)
    assert _records(inbox)[-1]["room"] == "lobby"


def test_only_frames_that_carry_a_body_are_persisted(render, tmp_path):
    """Presence, keepalives, the greeting and transport errors say nothing an agent rereads."""
    inbox = tmp_path / "i.jsonl"
    render({"type": "ping"}, inbox=inbox)
    render({"type": "presence", "peer": "x", "event": "connected"}, inbox=inbox)
    render({"type": "hello", "peer": "mac", "peers": ["a"], "queued": 0}, inbox=inbox)
    render({"type": "error", "message": "nope"}, inbox=inbox)
    assert not inbox.exists() or inbox.read_text() == ""


def test_an_unwritable_inbox_never_costs_the_message(render, tmp_path):
    """Printing the line matters more than recording it."""
    line = render({"type": "message", "id": "01AB", "from": "x", "body": "hi"},
                  inbox=tmp_path / "nope" / "deeper" / "i.jsonl")
    assert "hi" in line


def test_the_pointer_promises_the_inbox_only_when_the_frame_landed(render, tmp_path):
    """An agent sent to a file with no such line in it reads the preview and answers anyway."""
    line = render({"type": "message", "id": "01AB", "from": "x", "body": "L" * 900},
                  inbox=tmp_path / "nope" / "deeper" / "i.jsonl")
    assert "i.jsonl" not in line, "do not name a local copy that was not written"
    assert 'bus_message("01AB")' in line, "the tool route is all that is left"
    assert len(line) <= 512


def test_no_inbox_means_no_recording(render, tmp_path, monkeypatch):
    """The default belongs to the process's startup, not to render(): a test — or any caller that
    passes a frame for inspection — must not append to the live inbox of whoever is logged in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    line = render({"type": "message", "id": "01AB", "from": "x", "body": "L" * 900})
    assert "L" in line
    assert not (tmp_path / ".hivemind").exists(), "render() with no inbox must touch no file"


def test_a_recorded_frame_is_one_line_however_hostile_the_body(render, tmp_path):
    """A JSONL reader splits on more than \\n — Python's own splitlines() breaks on U+2028 — so a
    peer choosing its own body must not be able to forge a second record."""
    inbox = tmp_path / "i.jsonl"
    body = "first\nsecond third fourth\rfifth"
    render({"type": "message", "id": "01AB", "from": "x", "body": body}, inbox=inbox)
    recs = _records(inbox)
    assert len(recs) == 1, "one frame, one line"
    assert recs[0]["body"] == body, "and the body is kept exactly, newlines included"


def test_the_two_listeners_record_byte_identically(tmp_path):
    """The plugin script and the client CLI carry separate copies of the rendering rules. The
    recording rules travel with them and must not drift either."""
    from hivemind.bus import render as client_render
    plugin_render = _load().render
    # One inbox for both, because the pointer names the path: two paths would differ there and
    # nowhere else, which is the one difference this test does not want to see.
    inbox = tmp_path / "shared.jsonl"
    frames = [
        {"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "x" * 900},
        {"type": "message", "id": "01ABCDEFGH", "from": 'ev"il]', "body": "a\nb\x1b[31m\x00"},
        {"type": "broadcast", "id": "01ABCDEFGH", "from": "mac", "room": "lobby", "body": "hi"},
        {"type": "presence", "peer": "labbox", "event": "connected"},
        {"type": "ping"},
    ]
    for f in frames:
        assert plugin_render(f, inbox=inbox) == client_render(f, inbox=inbox), f
    lines = inbox.read_text().splitlines()
    assert len(lines) == 6, "three frames carry a body, each written twice — once per half"
    assert lines[0::2] == lines[1::2], "and the two halves must write the same bytes"


def test_both_listeners_expose_an_inbox_override():
    """`--inbox PATH`: one machine may run several sessions, and a test must never write to the
    real one."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, str(LISTENER), "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "--inbox" in out.stdout

    from hivemind.cli import build_parser
    args = build_parser().parse_args(["bus", "listen", "--url", "ws://h/bus/ws"])
    assert args.inbox is None, "unset means 'use the default', resolved at startup"
    args = build_parser().parse_args(["bus", "listen", "--url", "ws://h/bus/ws", "--inbox", "/x/i"])
    assert args.inbox == "/x/i"


def test_the_default_inbox_is_the_documented_path(monkeypatch, tmp_path):
    """SKILL.md tells every agent to read `~/.hivemind/bus-inbox.jsonl`; both halves must resolve
    to exactly that, and from $HOME at run time rather than at import time."""
    from hivemind import bus as client
    plugin = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    want = str(tmp_path / ".hivemind" / "bus-inbox.jsonl")
    assert plugin.default_inbox() == want
    assert client.default_inbox() == want


def test_preparing_the_inbox_creates_the_directory_and_never_raises(tmp_path, permissive_umask):
    """A plugin-only machine may have no ~/.hivemind yet, and the per-message append must stay a
    plain append rather than a directory check."""
    plugin = _load()
    from hivemind import bus as client
    for mod in (plugin, client):
        p = tmp_path / mod.__name__.replace(".", "_") / "deep" / "i.jsonl"
        assert mod.prepare_inbox(p) == str(p)
        assert p.parent.is_dir()
        assert p.is_file(), "created at startup, so an unusable path is found before a message is"
        assert p.stat().st_mode & 0o077 == 0, "peer traffic must not be group/world readable"
        p.write_text('{"kept": true}\n')
        assert mod.prepare_inbox(p) == str(p), "starting again must not disturb what is there"
        assert p.read_text() == '{"kept": true}\n'
    # a location that cannot be written is reported, not raised: every line still prints
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    assert plugin.prepare_inbox(blocked / "sub" / "i.jsonl") is None
    assert client.prepare_inbox(blocked / "sub" / "i.jsonl") is None
    assert plugin.prepare_inbox(tmp_path) is None, "a directory is not an inbox"
    assert client.prepare_inbox(tmp_path) is None
# ── the wiring: render() recording is worth nothing if the receive loop never passes a path ───
def test_the_plugin_listener_records_what_it_receives_end_to_end(tmp_path, monkeypatch, capsys):
    """From argv to the file on disk, with the socket faked out: the chain that actually runs."""
    mod = _load()
    inbox = tmp_path / "fresh" / "bus-inbox.jsonl"      # absent: startup must create the directory
    wire = [
        json.dumps({"type": "hello", "peer": "mac", "peers": [], "queued": 0}),
        json.dumps({"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "L" * 900}),
        "{not json at all",
        json.dumps(["a list is not a frame"]),
        json.dumps({"type": "ping"}),
    ]
    monkeypatch.setattr(mod, "_connect", lambda url, timeout=15: (None, b""))
    monkeypatch.setattr(mod, "_frames", lambda sock, rest=b"": iter(wire))
    monkeypatch.setattr(mod, "_send", lambda *a, **kw: None)

    assert mod.main(["--url", "ws://h/p/x/bus/ws", "--once", "--inbox", str(inbox)]) == 0

    out = capsys.readouterr().out
    assert "[hivemind msg=ABCDEFGH" in out, "the notification is still printed"
    assert "bus-inbox.jsonl" in out, "and it names the local copy"
    recs = _records(inbox)
    assert len(recs) == 1, "one message arrived; the greeting, the junk and the ping are not it"
    assert recs[0]["body"] == "L" * 900


def test_the_client_listener_carries_the_inbox_into_its_receive_loop(tmp_path, monkeypatch):
    from hivemind import bus as client

    seen = {}

    async def fake_once(url, inbox=None):
        seen["inbox"] = inbox
        raise KeyboardInterrupt                 # stop the retry loop deterministically

    monkeypatch.setattr(client, "_once", fake_once)
    inbox = tmp_path / "deep" / "i.jsonl"
    assert client.run_listen("ws://h/bus/ws", retry=False, inbox=str(inbox)) == 0
    assert seen["inbox"] == str(inbox), "the receive loop must know where to record"
    assert inbox.parent.is_dir(), "startup creates the directory so the append stays an append"


@pytest.mark.anyio
async def test_the_client_receive_loop_records_each_frame(tmp_path, monkeypatch):
    websockets = pytest.importorskip("websockets")
    from hivemind import bus as client

    class FakeWS:
        def __init__(self, frames):
            self._frames = frames

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def gen():
                for f in self._frames:
                    yield f
                yield b"binary frames are not part of this protocol"
            return gen()

    wire = [json.dumps({"type": "message", "id": "01ABCDEFGH", "from": "lab", "body": "L" * 900}),
            json.dumps({"type": "presence", "peer": "lab", "event": "connected"})]
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: FakeWS(wire))
    inbox = tmp_path / "i.jsonl"
    assert await client._once("ws://h/bus/ws", str(inbox)) == "closed"
    recs = _records(inbox)
    assert len(recs) == 1 and recs[0]["body"] == "L" * 900
# ── the horizon: an append-only file nobody prunes is how the blob store reached 94 GB ────────
def test_the_inbox_rotates_at_the_cap(listener, tmp_path, monkeypatch, permissive_umask):
    monkeypatch.setattr(listener, "INBOX_MAX_BYTES", 400)
    inbox, rolled = tmp_path / "i.jsonl", tmp_path / "i.jsonl.1"

    _fill(listener, inbox, 1, 1)
    assert not rolled.exists(), "a file under the cap must not rotate"

    n, written = 2, ["00000001"]
    while not rolled.exists():
        written += _fill(listener, inbox, n, 1)
        n += 1
        assert n < 100, "the cap must trigger a rotation, not grow forever"

    assert _ids(rolled) == written[:-1], "everything up to the roll is in the previous generation"
    assert _ids(inbox) == written[-1:], "and the message that found it full opened the new one"
    assert inbox.stat().st_mode & 0o077 == 0, "the new generation must be owner-only too"
    assert rolled.stat().st_mode & 0o077 == 0


def test_a_message_written_right_after_a_rotation_is_readable(listener, tmp_path, monkeypatch):
    """The point of the file is reading a clipped body back; a rotation must not cost the frame
    that happened to arrive next."""
    monkeypatch.setattr(listener, "INBOX_MAX_BYTES", 400)
    inbox, rolled = tmp_path / "i.jsonl", tmp_path / "i.jsonl.1"

    n = 1
    while not rolled.exists():
        _fill(listener, inbox, n, 1)
        n += 1
        assert n < 100

    line = listener.render({"type": "message", "id": "01FRESH", "from": "p", "body": "L" * 900},
                           inbox=inbox)
    assert "bus-inbox" in line or "i.jsonl" in line, "the pointer still names the local copy"
    assert _records(inbox)[-1]["body"] == "L" * 900
    assert len(_records(inbox)) == 2, "one rotation-opening record plus this one"


def test_the_previous_generation_survives_exactly_one_rotation(listener, tmp_path, monkeypatch):
    """One generation is the whole retention policy: a recovery buffer, not an archive."""
    monkeypatch.setattr(listener, "INBOX_MAX_BYTES", 400)
    inbox, rolled = tmp_path / "i.jsonl", tmp_path / "i.jsonl.1"

    n, gen1 = 1, []
    while not rolled.exists():
        gen1 += _fill(listener, inbox, n, 1)
        n += 1
        assert n < 100
    first = list(_ids(rolled))
    assert first == gen1[:-1]

    gen2 = gen1[-1:]
    while _ids(rolled) == first:
        gen2 += _fill(listener, inbox, n, 1)
        n += 1
        assert n < 200, "the second rotation must arrive too"

    assert _ids(rolled) == gen2[:-1], "`.1` is whatever rolled most recently"
    assert not set(first) & set(_ids(rolled) + _ids(inbox)), "the older generation is discarded"
    assert not (tmp_path / "i.jsonl.2").exists(), "one generation, never a chain of them"


def test_a_failed_rotation_never_costs_the_message(listener, tmp_path, monkeypatch):
    """Same discipline as the append itself: recording is best-effort, the message is not."""
    monkeypatch.setattr(listener, "INBOX_MAX_BYTES", 1)      # every further write is over the cap
    inbox = tmp_path / "i.jsonl"
    blocked = tmp_path / "i.jsonl.1"
    blocked.mkdir()
    (blocked / "occupied").write_text("os.replace cannot overwrite a non-empty directory")

    _fill(listener, inbox, 1, 1)                             # creates the file, no rotation yet
    line = listener.render({"type": "message", "id": "01AB", "from": "p", "body": "keep me"},
                           inbox=inbox)
    assert "keep me" in line, "the notification is printed whatever the filesystem says"
    assert _records(inbox)[-1]["body"] == "keep me", "and the append survives a failed rotation"
    assert (blocked / "occupied").exists(), "a rotation that cannot happen changes nothing"


# ── the ceiling: derived end to end, because reasoning about it has gone wrong every round ────
class _RecordingWS:
    """Minimal stand-in for the socket the hub writes to; keeps the exact text it was given."""

    def __init__(self):
        self.text = []

    async def send_text(self, text):
        self.text.append(text)

    async def close(self, code=1000):
        pass


async def _server_frame(sender, body):
    """The envelope `Hub.send` really builds — ids, timestamps and all — not a hand-copied
    literal, and serialised by the server's own encoder. Returns (frame, wire bytes)."""
    from hivemind_server.bus_ws import Hub
    hub = Hub()
    peer, _user = hub.redeem(hub.mint_ticket("\U0001F600" * 64)["ticket"])   # labels: .strip()[:64]
    ws = _RecordingWS()
    await hub.attach(peer, ws)
    out = await hub.send(sender, peer.label, body)        # raises BusError on an illegal body
    assert out["delivered"] is True
    return json.loads(ws.text[-1]), len(ws.text[-1].encode("utf-8"))


def _line_bytes(listener, tmp_path, frame, name):
    """What that frame costs in the inbox, written by the listener's own `_record` rather than by
    a copy of its rules here — so `ensure_ascii`, the newline and the envelope all live in one
    place, and changing any of them moves this number."""
    inbox = tmp_path / ("%s.jsonl" % name)
    assert listener._record(frame, inbox) is True
    return inbox.stat().st_size


def _char_classes():
    """One representative code point of every width UTF-8 has, keyed by that width.

    Derived from the encoding rather than listed by hand, and the assertion is the point: a
    ceiling that simply leaves a width out is how this file came to say "six bytes". Someone who
    believes a narrower character is the worst case has to delete a width here and answer for it.
    """
    out = {}
    for cp in (0x41, 0x80, 0x800, 0x10000):
        out[len(chr(cp).encode("utf-8"))] = chr(cp)
    assert sorted(out) == [1, 2, 3, 4], "UTF-8 has four widths; all four must be measured"
    return out


_CLASS_LINES = {}
_MAX_LINE = {}


async def _class_lines(listener, tmp_path):
    """Per UTF-8 width: what a maximal body of it costs in the inbox, and on the wire."""
    key = listener.__name__
    if key not in _CLASS_LINES:
        from hivemind_server.bus_ws import MAX_BODY
        out = {}
        for width, ch in _char_classes().items():
            frame, wire = await _server_frame("p", ch * MAX_BODY)
            assert len(frame["body"]) == MAX_BODY, "the server takes all of it: the cap is points"
            out[width] = (_line_bytes(listener, tmp_path, frame, "%s-w%d" % (key, width)), wire)
        _CLASS_LINES[key] = out
    return _CLASS_LINES[key]


async def _max_recordable_line(listener, tmp_path):
    """The largest line a listener can be made to write, derived rather than reasoned about.

    The constraint that binds is NOT `MAX_BODY`: `from` is the `agent` argument of `bus_send` and
    nothing caps it, so it can carry a payload of its own. What binds is the wire — a frame over
    `MAX_FRAME` is refused by the listener and never recorded at all. So: fill the body with
    whatever costs most per code point, fill the rest of the wire budget with whatever costs most
    per wire byte, and check that one more code point would not fit.

    Both characters are CHOSEN HERE, by measuring every width. That is the mechanism: a reader who
    believes some other class is the worst case has to change a comparison the tests make, not a
    constant they can edit quietly.
    """
    key = listener.__name__
    if key in _MAX_LINE:
        return _MAX_LINE[key]
    from hivemind_server.bus_ws import MAX_BODY
    classes, lines = _char_classes(), await _class_lines(listener, tmp_path)
    body_ch = classes[max(lines, key=lambda w: lines[w][0])]                     # per code point
    fill_width = max(lines, key=lambda w: lines[w][0] / lines[w][1])             # per wire byte
    fill_ch = classes[fill_width]

    body = body_ch * MAX_BODY
    _, wire_without = await _server_frame("x", body)
    n = (listener.MAX_FRAME - wire_without) // fill_width
    for _ in range(8):
        frame, wire = await _server_frame(fill_ch * n, body)
        if wire <= listener.MAX_FRAME:
            break
        n -= (wire - listener.MAX_FRAME + fill_width - 1) // fill_width
    else:
        raise AssertionError("could not fit a frame to the wire cap")
    assert wire <= listener.MAX_FRAME, "a frame over MAX_FRAME is refused, so it is not a ceiling"

    # Maximality, proved without assuming WHICH limit binds — that assumption is what went wrong
    # before. Nothing more can go in either field: one more code point of body is refused by the
    # server outright, and one more in `from` either does not fit the wire or is dropped by the
    # send path's own cap (in which case the frame is unchanged).
    from hivemind_server.bus_ws import BusError
    with pytest.raises(BusError):
        await _server_frame(fill_ch * n, body + body_ch)
    _, over = await _server_frame(fill_ch * (n + 1), body)
    assert over > listener.MAX_FRAME or over == wire, \
        "either one more code point does not fit the wire, or the field is capped"

    _MAX_LINE[key] = _line_bytes(listener, tmp_path, frame, "max-" + key)
    return _MAX_LINE[key]


@pytest.mark.anyio
async def test_the_widest_characters_are_what_maximise_a_record(tmp_path):
    """The claim every ceiling in this file has rested on, made checkable.

    It was stated as x1, then as x6, and is x12; each time the factor was reasoned out and nothing
    in the suite could contradict it. `MAX_BODY` counts CODE POINTS, so at a fixed number of them
    a 4-byte (astral) character costs the most: `ensure_ascii` writes it as a surrogate PAIR.
    Per WIRE byte — which is what bounds the uncapped `from` field — a 4-byte character ties with
    a 2-byte one, both at 3x, and both beat a 3-byte BMP character.
    """
    from hivemind_server.bus_ws import MAX_BODY
    lines = await _class_lines(_load(), tmp_path)
    per_point = {w: v[0] for w, v in lines.items()}
    per_wire = {w: round(v[0] / v[1], 1) for w, v in lines.items()}

    assert max(per_point, key=per_point.get) == 4, per_point
    assert per_point[1] < per_point[2] == per_point[3] < per_point[4], per_point
    assert per_point[3] - per_point[1] == 5 * MAX_BODY, "a BMP character escapes to six bytes"
    assert per_point[4] - per_point[3] == 6 * MAX_BODY, "and an astral one to twelve"

    assert per_wire[4] == per_wire[2] == 3.0, per_wire
    assert per_wire[3] == 2.0 and per_wire[1] == 1.0, per_wire


@pytest.mark.anyio
async def test_the_ceiling_is_derived_from_a_frame_that_really_fits_the_wire(tmp_path):
    """The number `docs/bus.md` quotes. Every part of it is measured: the envelope comes from
    `Hub.send`, the line from `_record`, and the frame is proven deliverable against `MAX_FRAME`
    — which is the check that matters, because a frame the listener refuses is not a ceiling."""
    from hivemind_server.bus_ws import LABEL_CAP, MAX_BODY
    listener = _load()
    line = await _max_recordable_line(listener, tmp_path)

    # What binds is the body cap — but only because every OTHER caller-controlled field on a
    # frame is capped at LABEL_CAP on the send path. Without that, `from` carries a payload of
    # its own and the wire becomes the constraint instead (measured then: 6.00 MiB, a ceiling of
    # 20.00). This assertion is that cap's tripwire as much as it is the ceiling's: the two
    # labels can contribute at most twelve bytes per code point each, so everything that is not
    # body is a rounding error.
    assert 12 * MAX_BODY <= line <= 12 * MAX_BODY + 2 * 12 * LABEL_CAP + 256, \
        "the body is what binds; the names on a frame are bounded by LABEL_CAP"
    assert round(line / 1048576, 2) == 3.00, "3.00 MiB is the largest recordable line"
    ceiling = 2 * (listener.INBOX_MAX_BYTES + line)
    assert round(ceiling / 1048576, 2) == 14.00, "so 14.00 MiB is the ceiling across both files"


@pytest.mark.anyio
async def test_a_record_larger_than_a_whole_generation_would_still_land(tmp_path):
    """A maximal record fits in a generation today — 3.00 MiB against a 4 MiB cap — but the two
    limits are set independently, in different files, for different reasons, so nothing keeps
    that true. It does not need to be: this file once asserted that a maximal message must fit
    "or it could never land", and that premise was false. The rotation runs first and the append
    is unconditional, so an oversized record opens a generation of its own and rolls the previous
    one away. The cap bounds the file, not the record."""
    listener = _load()
    line = await _max_recordable_line(listener, tmp_path)
    assert line < listener.INBOX_MAX_BYTES, "today it fits — see the comment for why that is not " \
                                            "something to rely on"

    inbox = tmp_path / "oversize.jsonl"
    frame, _wire = await _server_frame("p", "x")
    frame["body"] = "L" * (listener.INBOX_MAX_BYTES + 1024)      # larger than a whole generation
    assert listener._record(frame, inbox) is True, "an oversized record must still be recorded"
    assert inbox.stat().st_size > listener.INBOX_MAX_BYTES
    assert json.loads(inbox.read_text())["body"] == frame["body"], "and it must still parse"


@pytest.mark.anyio
async def test_both_halves_agree_on_the_cap(tmp_path):
    from hivemind import bus as client
    plugin = _load()
    assert plugin.INBOX_MAX_BYTES == client.INBOX_MAX_BYTES == 4 * 1024 * 1024
    assert plugin.MAX_FRAME == client.MAX_FRAME, "both halves must accept the same frame size"
    # ONE guard, not three: the measured line is the check, and the ceiling it implies is what
    # docs/bus.md states. An earlier version asserted `>= 6 * MAX_BODY` "so a maximal message can
    # land", which was weaker than the MAX_FRAME floor it replaced (1.50 MiB against 2.00 MiB)
    # AND rested on a premise that is false: an oversized record lands regardless. What the cap
    # has to be is large enough that ordinary traffic is not rolling constantly, and known — so
    # what is asserted is the ceiling both halves produce, identically.
    for mod in (plugin, client):
        line = await _max_recordable_line(mod, tmp_path)
        assert round(2 * (mod.INBOX_MAX_BYTES + line) / 1048576, 2) == 14.00, \
            "each half's cap and largest line must give the 14.00 MiB ceiling the docs state"
    assert _MAX_LINE[plugin.__name__] == _MAX_LINE[client.__name__], \
        "and the two halves must record a frame identically, byte for byte"


# ── write(2) is allowed to take less than you gave it, and the count is the only way to know ──
class _PartialOS:
    """Stands in for the `os` a listener module imported, shortening every write.

    `chunk` bytes go through per call; after `allowed` calls the device "fills up" — `fails`
    decides whether that shows up as a zero-length write or as ENOSPC, since write(2) is entitled
    to either.
    """

    def __init__(self, chunk, allowed=None, fails="zero"):
        self.chunk, self.allowed, self.fails, self.calls = chunk, allowed, fails, 0

    def __getattr__(self, name):
        return getattr(os, name)            # everything except write is the real thing

    def write(self, fd, data):
        self.calls += 1
        if self.allowed is not None and self.calls > self.allowed:
            if self.fails == "enospc":
                raise OSError(errno.ENOSPC, "No space left on device")
            return 0
        return os.write(fd, data[:self.chunk])


def test_a_short_write_still_lands_the_whole_line(listener, tmp_path, monkeypatch):
    """os.write is write(2), not a buffered writer: it may take a slice. Looping is the fix."""
    monkeypatch.setattr(listener, "os", _PartialOS(chunk=8))
    inbox = tmp_path / "i.jsonl"
    line = listener.render({"type": "message", "id": "01ABCDEFGH", "from": "p", "body": "L" * 900},
                           inbox=inbox)
    assert _records(inbox)[-1]["body"] == "L" * 900, "every slice must be written, not just one"
    assert "i.jsonl" in line, "and the pointer may name the copy, because there is one"


@pytest.mark.parametrize("fails", ["zero", "enospc"])
def test_a_line_that_cannot_be_completed_is_never_vouched_for(listener, tmp_path, monkeypatch,
                                                              fails):
    """The ENOSPC case. A discarded short-write count leaves a truncated, unparseable record while
    render() reports it as landed — so the pointer sends an agent to a corrupt fragment, which is
    this task's original bug with an extra step. It must report False instead."""
    monkeypatch.setattr(listener, "os", _PartialOS(chunk=64, allowed=1, fails=fails))
    inbox = tmp_path / "i.jsonl"
    line = listener.render({"type": "message", "id": "01TRUNC", "from": "p", "body": "L" * 900},
                           inbox=inbox)

    assert "L" in line, "the message is still printed — that was never in question"
    assert "i.jsonl" not in line, "but the local copy must not be promised: it is a fragment"
    assert 'bus_message("01TRUNC")' in line, "the route that is left is named"
    raw = inbox.read_text().splitlines()
    assert len(raw) == 1 and len(raw[0]) < 900, "what landed is a fragment, as expected"
    with pytest.raises(ValueError):
        json.loads(raw[0])

    # ...and the damage must stop at that line: the NEXT message is a complete, parseable record.
    monkeypatch.setattr(listener, "os", os)
    after = listener.render({"type": "message", "id": "01NEXT", "from": "p", "body": "intact"},
                            inbox=inbox)
    assert "i.jsonl" not in after, "a short body needs no pointer at all"
    lines = inbox.read_text().splitlines()
    assert len(lines) == 2, "the fragment was sealed, so the next record starts on its own line"
    assert json.loads(lines[1])["body"] == "intact"


def test_a_tear_left_by_an_earlier_run_is_healed_at_startup(listener, tmp_path):
    """`_TORN` dies with the process; the fragment on disk does not. If the first message of the
    next run is appended onto it, a complete message becomes unreadable — and is reported as
    landed, which is the dishonesty this whole class of fix is about."""
    inbox = tmp_path / "i.jsonl"
    inbox.write_text('{"body": "complete"}\n{"body": "cut off mid-')

    assert listener.prepare_inbox(inbox) == str(inbox)
    listener.render({"type": "message", "id": "01AB", "from": "p", "body": "after the restart"},
                    inbox=inbox)

    lines = inbox.read_text().splitlines()
    assert len(lines) == 3, "the fragment must keep its own line"
    assert json.loads(lines[-1])["body"] == "after the restart"
    assert json.loads(lines[0])["body"] == "complete", "and what was already good is untouched"


def test_an_inherited_wider_mode_is_narrowed_at_startup(tmp_path, permissive_umask):
    """Measured on this machine: ~/.hivemind/bus-inbox.jsonl is 0644, left by a run before the
    mode was set at creation — and on the first roll that mode travels to `.1`. The file is ours,
    holds other agents' message bodies, and nobody chose 0644 for it."""
    from hivemind import bus as client
    plugin = _load()
    for mod in (plugin, client):
        inbox = tmp_path / mod.__name__.replace(".", "_") / "bus-inbox.jsonl"
        inbox.parent.mkdir()
        inbox.write_text('{"body": "from an earlier version"}\n')
        rolled = pathlib.Path(str(inbox) + ".1")
        rolled.write_text('{"body": "and its rolled generation"}\n')
        os.chmod(inbox, 0o644)
        os.chmod(rolled, 0o646)

        assert mod.prepare_inbox(inbox) == str(inbox)
        assert inbox.stat().st_mode & 0o777 == 0o600, "the live inbox is narrowed"
        assert rolled.stat().st_mode & 0o777 == 0o600, "and so is the generation beside it"
        assert json.loads(inbox.read_text())["body"] == "from an earlier version", "content kept"
        assert json.loads(rolled.read_text())["body"] == "and its rolled generation"


def test_a_tear_that_placed_nothing_leaves_no_blank_line(listener, tmp_path, monkeypatch):
    """A write can fail having placed nothing at all, so "torn" does not imply "a fragment is
    there". Prefixing a newline on that assumption puts a blank line at the top of the file, and
    the skill tells agents an unparseable line means a message was cut short — a blank one means
    nothing was."""
    monkeypatch.setattr(listener, "os", _PartialOS(chunk=64, allowed=0))
    inbox = tmp_path / "i.jsonl"
    line = listener.render({"type": "message", "id": "01GONE", "from": "p", "body": "L" * 900},
                           inbox=inbox)
    assert "i.jsonl" not in line, "nothing landed, so nothing is promised"
    assert inbox.read_text() == "", "and nothing is on disk either"
    assert str(inbox) in listener._TORN, "but the next write is told to check"

    monkeypatch.setattr(listener, "os", os)
    listener.render({"type": "message", "id": "01NEXT", "from": "p", "body": "fine"}, inbox=inbox)
    lines = inbox.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "01NEXT", "no blank line above it"


def test_a_rotation_absorbs_a_torn_line_without_leaving_a_blank_one(listener, tmp_path,
                                                                   monkeypatch):
    """The fragment leaves with the generation that rolls away, so the fresh file starts clean."""
    inbox, rolled = tmp_path / "i.jsonl", tmp_path / "i.jsonl.1"
    monkeypatch.setattr(listener, "os", _PartialOS(chunk=64, allowed=1))
    listener.render({"type": "message", "id": "01TORN", "from": "p", "body": "L" * 900},
                    inbox=inbox)
    assert str(inbox) in listener._TORN

    monkeypatch.setattr(listener, "os", os)
    monkeypatch.setattr(listener, "INBOX_MAX_BYTES", 1)      # the next record must roll first
    listener.render({"type": "message", "id": "01FRESH", "from": "p", "body": "clean"},
                    inbox=inbox)

    assert rolled.exists(), "the generation holding the fragment rolled away"
    lines = inbox.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "01FRESH", "no blank first line"


def test_the_rolled_generation_is_not_followed_when_it_is_a_symlink(listener, tmp_path,
                                                                   permissive_umask):
    """Narrowing an inherited mode is a new operation on `.1` — a path this code otherwise only
    ever renames onto, and os.replace does not follow its destination. os.stat/os.chmod do, so a
    link left there would hand the chmod to whatever it points at."""
    victim = tmp_path / "someone-elses-file"
    victim.write_text("not ours")
    os.chmod(victim, 0o666)
    inbox = tmp_path / "i.jsonl"
    inbox.write_text('{"body": "ours"}\n')
    os.chmod(inbox, 0o644)
    os.symlink(str(victim), str(inbox) + ".1")

    assert listener.prepare_inbox(inbox) == str(inbox)
    assert inbox.stat().st_mode & 0o777 == 0o600, "our own file is still narrowed"
    assert victim.stat().st_mode & 0o777 == 0o666, "the link's target must be left alone"
    assert os.path.islink(str(inbox) + ".1"), "and the link itself is not replaced"


def test_a_symlinked_inbox_is_narrowed_because_we_write_through_it(listener, tmp_path,
                                                                   permissive_umask):
    """The other half of not following links at `.1`: the live inbox must still be narrowed.

    Skipping links for both paths looked symmetrical and was wrong — every other operation here
    follows the live path (os.open to create and to append, getsize for the rotation), so a
    symlinked inbox is a file this listener writes peer bodies into. Leaving its mode alone means
    a world-readable transcript, which is the case M5 exists to fix.
    """
    target = tmp_path / "elsewhere.jsonl"
    target.write_text('{"body": "ours, reached through a link"}\n')
    os.chmod(target, 0o666)
    inbox = tmp_path / "i.jsonl"
    os.symlink(str(target), str(inbox))

    assert listener.prepare_inbox(inbox) == str(inbox)
    assert target.stat().st_mode & 0o777 == 0o600, "the file we actually write must be narrowed"

    listener.render({"type": "message", "id": "01AB", "from": "p", "body": "through the link"},
                    inbox=inbox)
    assert json.loads(target.read_text().splitlines()[-1])["body"] == "through the link", \
        "and it is genuinely the file the listener appends to"
