"""The plugin's standalone listener: it is what a plugin-only machine actually runs.

Before this existed, bus_connect returned `hivemind bus listen …` — a CLI that ships in a separate
package and needs a third-party websockets dependency. A machine holding only the Claude Code
plugin had neither, so the bus was unusable there and the agent just saw "command not found".
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from hivemind_server import bus_ws

ROOT = pathlib.Path(__file__).resolve().parents[3]
LISTENER = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "bus-listen.py"
GUIDE_SH = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "guide.sh"


def _load_listener():
    spec = importlib.util.spec_from_file_location("bus_listen", LISTENER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_plugin_ships_the_listener():
    assert LISTENER.is_file(), "the plugin must carry the listener; the CLI is not installed there"


def test_listener_has_no_third_party_imports():
    """Its whole reason to exist is running where nothing has been pip-installed."""
    std = getattr(sys, "stdlib_module_names", None)
    if not std:
        pytest.skip("need python 3.10+ to enumerate the stdlib")
    mods = set()
    for n in ast.walk(ast.parse(LISTENER.read_text())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods - set(std)), "listener must be stdlib-only"


def test_listener_runs_under_the_oldest_supported_interpreter():
    r = subprocess.run([sys.executable, str(LISTENER), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--key" in r.stdout


FRAMES = [
    {"type": "hello", "peer": "mac", "peers": ["labbox", "laptop"], "queued": 0},
    {"type": "hello", "peer": "mac", "peers": [], "queued": 3},
    {"type": "presence", "peer": "labbox", "event": "connected"},
    {"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "short one"},
    {"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "x" * 900},
    {"type": "broadcast", "id": "01ABCDEFGH", "from": "mac", "room": "lobby", "body": "hi all"},
    {"type": "error", "message": "nope"},
    {"type": "ping"},
    # hostile input: a peer controls its own label and body, and an LLM reads the result
    {"type": "message", "id": "01ABCDEFGH", "from": 'ev"il]', "body": "a\nb\x1b[31m\x00"},
]


def test_listener_render_matches_the_client_exactly(tmp_path):
    """Two copies of the rendering rules exist because the plugin cannot import the client
    package. This test is what keeps them from drifting apart."""
    from hivemind.bus import render as client_render
    listener_render = _load_listener().render
    for f in FRAMES:
        assert listener_render(f) == client_render(f), f
    assert listener_render({"type": "ping"}) is None

    # The local inbox travels with the rendering rules: a clipped line names the file it was
    # appended to, so an implementation that forgot to record would say so in its own output.
    # One shared path, since the pointer names it and two paths would differ only there.
    inbox = tmp_path / "bus-inbox.jsonl"
    for f in FRAMES:
        assert listener_render(f, inbox=inbox) == client_render(f, inbox=inbox), f
    lines = inbox.read_text().splitlines()
    assert lines and lines[0::2] == lines[1::2], "both halves must record the same bytes"


def test_listener_never_emits_a_clippable_line(tmp_path):
    listener_render = _load_listener().render
    inbox = tmp_path / "bus-inbox.jsonl"
    for f in FRAMES:
        # With an inbox the pointer names two routes, so the tail is longer and `room` smaller;
        # the budget is what must hold, whichever way the line was built.
        for line in (listener_render(f), listener_render(f, inbox=inbox)):
            if line is not None:
                assert len(line) <= 512, (len(line), f)
                assert "\n" not in line


def test_durable_notification_directs_receiver_to_server_history(tmp_path):
    render = _load_listener().render
    inbox = tmp_path / "notifications.jsonl"
    mid = "01ABCDEFGHABCDEFGHABCDEFG"
    dm = {"v": 2, "type": "chat", "channel": "dm", "id": mid, "from": "nik-mac-codex",
          "preview": "check parser", "ts": 1700000000.0}
    room = {**dm, "channel": "room", "room": "parser"}
    dm_line = render(dm, inbox=inbox)
    room_line = render(room, inbox=inbox)
    assert mid in dm_line and "chat_inbox" in dm_line
    assert mid in room_line and "chat_room_history" in room_line
    assert "bus_message" not in (dm_line + room_line)
    assert [json.loads(line)["id"] for line in inbox.read_text().splitlines()] == [mid, mid]


def test_revoked_canonical_listener_requests_canonical_reconnect(tmp_path, monkeypatch, capsys):
    mod = _load_listener()

    def refused(*args, **kwargs):
        raise mod.WSRefused("revoked")

    monkeypatch.setattr(mod, "_once", refused)
    code = mod.main(["--url", "ws://localhost/p/demo/chat/ws", "--key", "hk2.revoked",
                     "--once", "--inbox", str(tmp_path / "inbox.jsonl")])
    assert code == 2
    assert "chat_connect" in capsys.readouterr().out


def test_guide_sh_installs_the_listener(tmp_path, monkeypatch):
    """The Monitor command names a fixed $HOME path, so loading the skill must put it there."""
    dst = tmp_path / "nested" / "bus-listen.py"
    subprocess.run(["bash", str(GUIDE_SH), "--section", "core"], capture_output=True,
                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                        "HIVEMIND_LISTENER": str(dst)}, check=False)
    assert dst.is_file(), "a skill load must install the listener"
    assert dst.read_text() == LISTENER.read_text()


# ── listen keys: the credential that makes reconnect-forever possible ────────────────────────
@pytest.fixture
def hub():
    return bus_ws.Hub("keytest")


def test_listen_key_round_trips(hub):
    k = hub.mint_listen_key("mac", user="nik")["listen_key"]
    peer, user = hub.redeem_key(k)
    assert peer is not None and peer.label == "mac" and user == "nik"


def test_listen_key_is_reusable(hub):
    """Unlike a ticket. A single-use credential in argv dies on the first network blip."""
    k = hub.mint_listen_key("mac")["listen_key"]
    assert hub.redeem_key(k) is not None
    assert hub.redeem_key(k) is not None


def test_listen_key_survives_a_server_restart(tmp_path):
    """A fresh Hub with the same on-disk secret must still admit an existing listener."""
    scope = bus_ws._scope(tmp_path)
    bus_ws.register_secret(tmp_path)
    k = bus_ws.Hub(scope).mint_listen_key("mac", user="nik")["listen_key"]

    bus_ws._SECRETS.pop(scope, None)              # simulate the process going away
    bus_ws.register_secret(tmp_path)              # ...and coming back on the same data dir
    fresh = bus_ws.Hub(scope)
    peer, user = fresh.redeem_key(k)
    assert peer is not None and peer.label == "mac", "a restart must not orphan every listener"
    assert user == "nik", "and it must still know whose access to check"


def test_tampered_or_foreign_listen_key_is_refused(hub, tmp_path):
    k = hub.mint_listen_key("mac", user="nik")["listen_key"]
    scheme, label, user, exp, sig = k.split(".")
    assert hub.redeem_key(f"{scheme}.{label}.{user}.{int(exp) + 86400}.{sig}") is None, \
        "expiry is signed"
    assert hub.redeem_key(f"{scheme}.{bus_ws._b64(b'root')}.{user}.{exp}.{sig}") is None, \
        "label is signed"
    assert hub.redeem_key(f"{scheme}.{label}.{bus_ws._b64(b'root')}.{exp}.{sig}") is None, \
        "user is signed"
    assert hub.redeem_key(f"{scheme}.{label}.{exp}.{sig}") is None, \
        "a pre-user key names nobody, so there is no access to re-check"
    assert hub.redeem_key("garbage") is None
    assert hub.redeem_key("") is None
    assert bus_ws.Hub("other-project").redeem_key(k) is None, "keys must not cross projects"


def test_expired_listen_key_is_refused(hub, monkeypatch):
    k = hub.mint_listen_key("mac")["listen_key"]
    monkeypatch.setattr(bus_ws, "_now", lambda: bus_ws.time.time() + bus_ws.LISTEN_KEY_TTL + 10)
    assert hub.redeem_key(k) is None


def test_secret_file_is_owner_only(tmp_path):
    bus_ws.register_secret(tmp_path)
    path = tmp_path / "bus_secret"
    assert path.stat().st_mode & 0o077 == 0, "the bus signing key must not be group/world readable"


def test_bus_connect_returns_a_command_a_plugin_only_machine_can_run(tmp_path):
    """The regression this file exists for. The command must run with python3 against a path built
    from $HOME, and must NOT name the `hivemind` CLI: that ships in hivemind-client, which a
    machine holding only the Claude Code plugin has not installed."""
    from hivemind_server import bus_ws_tools

    captured = {}

    class FakeMCP:
        def tool(self, **_kw):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    # Under tmp_path, NOT beside this file: bus_connect calls register_secret(dir), which mints a
    # real 32-byte HMAC key into whatever directory it is handed. Pointed at the source tree it
    # wrote a live secret into the working copy on every run — one such key has already reached
    # origin that way. .gitignore covers it, but an ignore rule is the second line of defence and
    # this is the first.
    class FakeProject:
        name = "plugin-only"
        dir = tmp_path / "plugin-only"

    FakeProject.dir.mkdir()
    # build_app does this for every project it mounts; this test stands in for build_app, and
    # bus_connect now refuses a project with no /p/<name>/ prefix rather than handing back a URL
    # that 404s. test_bus_connect_refuses_a_project_that_has_no_routes_yet covers the other side.
    bus_ws.register_mount(FakeProject.dir)
    bus_ws_tools.attach(FakeMCP(), type("C", (), {"public_url": "http://box:8787"}))

    # The hub and the ws URL are resolved per CALL now, from the project of the call in flight —
    # published by envelope.with_project on the real path, which this FakeMCP stands in for.
    from hivemind_server import envelope
    tok = envelope._PROJECT.set(FakeProject)
    try:
        out = captured["bus_connect"](label="remote-session")
    finally:
        envelope._PROJECT.reset(tok)
    cmd = out["monitor_command"]
    assert cmd.startswith("python3 "), cmd
    assert "$HOME/.hivemind/bus-listen.py" in cmd, cmd
    assert "hivemind bus listen" not in cmd, "the CLI is absent on a plugin-only machine"
    assert out["listen_key"].startswith("hk1."), out
    assert "ws://box:8787/p/plugin-only/bus/ws" in cmd, cmd
    # the key travels in argv, so it must be the scoped one, never the API token
    assert "Bearer" not in cmd and len(out["listen_key"]) < 200
