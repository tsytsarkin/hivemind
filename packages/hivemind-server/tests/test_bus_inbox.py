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


def test_both_halves_agree_on_the_cap():
    from hivemind import bus as client
    from hivemind_server.bus_ws import MAX_BODY
    plugin = _load()
    assert plugin.INBOX_MAX_BYTES == client.INBOX_MAX_BYTES == 4 * 1024 * 1024
    assert plugin.MAX_FRAME == client.MAX_FRAME, "both halves must accept the same frame size"
    # The binding floor is the largest record the SERVER will ever hand a listener: MAX_BODY is
    # counted in characters, and ensure_ascii turns a BMP character into six bytes.
    for mod in (plugin, client):
        assert mod.INBOX_MAX_BYTES >= 6 * MAX_BODY, \
            "a maximal server-legal message must fit in one generation, or it could never land"
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
