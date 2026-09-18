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


def test_listener_render_matches_the_client_exactly():
    """Two copies of the rendering rules exist because the plugin cannot import the client
    package. This test is what keeps them from drifting apart."""
    from hivemind.bus import render as client_render
    listener_render = _load_listener().render
    for f in FRAMES:
        assert listener_render(f) == client_render(f), f
    assert listener_render({"type": "ping"}) is None


def test_listener_never_emits_a_clippable_line():
    listener_render = _load_listener().render
    for f in FRAMES:
        line = listener_render(f)
        if line is not None:
            assert len(line) <= 512, (len(line), f)
            assert "\n" not in line


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
    k = hub.mint_listen_key("mac")["listen_key"]
    peer = hub.redeem_key(k)
    assert peer is not None and peer.label == "mac"


def test_listen_key_is_reusable(hub):
    """Unlike a ticket. A single-use credential in argv dies on the first network blip."""
    k = hub.mint_listen_key("mac")["listen_key"]
    assert hub.redeem_key(k) is not None
    assert hub.redeem_key(k) is not None


def test_listen_key_survives_a_server_restart(hub, tmp_path):
    """A fresh Hub with the same on-disk secret must still admit an existing listener."""
    path = tmp_path / "bus_secret"
    bus_ws.register_secret("restart", path)
    k = bus_ws.Hub("restart").mint_listen_key("mac")["listen_key"]

    bus_ws._SECRETS.pop("restart", None)          # simulate the process going away
    bus_ws.register_secret("restart", path)       # ...and coming back on the same data dir
    fresh = bus_ws.Hub("restart")
    peer = fresh.redeem_key(k)
    assert peer is not None and peer.label == "mac", "a restart must not orphan every listener"


def test_tampered_or_foreign_listen_key_is_refused(hub, tmp_path):
    k = hub.mint_listen_key("mac")["listen_key"]
    scheme, label, exp, sig = k.split(".")
    assert hub.redeem_key(f"{scheme}.{label}.{int(exp) + 86400}.{sig}") is None, "expiry is signed"
    assert hub.redeem_key(f"{scheme}.{bus_ws._b64(b'root')}.{exp}.{sig}") is None, "label is signed"
    assert hub.redeem_key("garbage") is None
    assert hub.redeem_key("") is None
    assert bus_ws.Hub("other-project").redeem_key(k) is None, "keys must not cross projects"


def test_expired_listen_key_is_refused(hub, monkeypatch):
    k = hub.mint_listen_key("mac")["listen_key"]
    monkeypatch.setattr(bus_ws, "_now", lambda: bus_ws.time.time() + bus_ws.LISTEN_KEY_TTL + 10)
    assert hub.redeem_key(k) is None


def test_secret_file_is_owner_only(tmp_path):
    path = tmp_path / "bus_secret"
    bus_ws.register_secret("perm", path)
    assert path.stat().st_mode & 0o077 == 0, "the bus signing key must not be group/world readable"


def test_bus_connect_returns_a_command_a_plugin_only_machine_can_run():
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

    class FakeProject:
        name = "plugin-only"
        dir = pathlib.Path(__file__).resolve().parent / "_tmp_plugin_only"

    FakeProject.dir.mkdir(exist_ok=True)
    bus_ws_tools.attach(FakeMCP(), FakeProject, type("C", (), {"public_url": "http://box:8787"}))

    out = captured["bus_connect"](label="remote-session")
    cmd = out["monitor_command"]
    assert cmd.startswith("python3 "), cmd
    assert "$HOME/.hivemind/bus-listen.py" in cmd, cmd
    assert "hivemind bus listen" not in cmd, "the CLI is absent on a plugin-only machine"
    assert out["listen_key"].startswith("hk1."), out
    assert "ws://box:8787/p/plugin-only/bus/ws" in cmd, cmd
    # the key travels in argv, so it must be the scoped one, never the API token
    assert "Bearer" not in cmd and len(out["listen_key"]) < 200
