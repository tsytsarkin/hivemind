"""bus-autojoin: owning a listener, keeping the read cursor, and reporting the truth about it.

Everything here is about the same underlying fact — TWO copies of every script exist (the plugin
tree's and the one the skill installs in $HOME/.hivemind), the hooks run one and the documented
agent command runs the other — plus the state file that has to survive between them.
"""
import ast
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
CLAUDE = ROOT / "plugin" / "skills" / "hivemind" / "scripts"
CODEX = ROOT / "plugins" / "hivemind" / "skills" / "hivemind" / "scripts"
AUTOJOIN = CLAUDE / "bus-autojoin.py"
LAUNCHER = ROOT / "scripts" / "hivemind-codex"

SLEEPER = "import sys, time\ntime.sleep(60)\n"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("bus_autojoin_under_test", AUTOJOIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def listener_process(tmp_path):
    """A stand-in for a running bus-listen.py, spawned from a path of the test's choosing."""
    started = []

    def spawn(directory, marker):
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "bus-listen.py"
        script.write_text(SLEEPER)
        proc = subprocess.Popen([sys.executable, str(script), "--instance-id", marker],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started.append(proc)
        for _ in range(50):                     # ps must be able to see it before we assert on it
            out = subprocess.run(["ps", "-ww", "-p", str(proc.pid), "-o", "command="],
                                 capture_output=True, text=True).stdout
            if marker in out:
                break
            time.sleep(0.02)
        return proc, script

    yield spawn
    for proc in started:
        try:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


# ── finding 1: the two copies must recognise each other's listener ────────────────────────────
def test_a_listener_started_by_the_other_copy_is_still_ours(mod, tmp_path, listener_process):
    """The hooks run ${CLAUDE_PLUGIN_ROOT}/…/bus-autojoin.py; project.md, SKILL.md, user-guide.md
    and bus.md all tell the agent to run $HOME/.hivemind/bus-autojoin.py. Each spawns the
    bus-listen.py sitting beside IT.

    Matching the listener by resolved path made each blind to the other's process: _owned_process
    returned False, so _stop did nothing, a second listener attached to the same inbox (every
    message recorded and counted twice), and the first was orphaned with its pid overwritten in
    listener.json — unreachable even by --mode stop at SessionEnd. It then recurred every
    RETRY_SECONDS for the rest of the session.
    """
    marker = "a" * 24
    proc, plugin_copy = listener_process(tmp_path / "plugin-tree", marker)
    state = {"pid": proc.pid, "marker": marker}
    home_copy = tmp_path / "home" / ".hivemind" / "bus-listen.py"
    home_copy.parent.mkdir(parents=True)
    home_copy.write_text(SLEEPER)

    assert mod._owned_process(state, plugin_copy), "the copy that spawned it must own it"
    assert mod._owned_process(state, home_copy), \
        "the other installed copy must own it too, or it spawns a duplicate and orphans this one"


def test_ownership_still_requires_the_marker_and_a_listener(mod, tmp_path, listener_process):
    """Loosening the path match must not loosen the identity check: the marker is what makes this
    process ours, and the basename is what rejects a recycled pid."""
    proc, script = listener_process(tmp_path / "tree", "b" * 24)
    listener = script.with_name("bus-listen.py")
    assert not mod._owned_process({"pid": proc.pid, "marker": "c" * 24}, listener)
    assert not mod._owned_process({"pid": proc.pid, "marker": ""}, listener)
    assert not mod._owned_process({"pid": proc.pid}, listener)
    # a live process that is not a listener at all
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert not mod._owned_process({"pid": other.pid, "marker": "b" * 24}, listener)
    finally:
        other.kill()
        other.wait(timeout=5)


# ── finding 7: one install path, so the copies must not drift ─────────────────────────────────
def test_the_two_autojoin_copies_are_byte_identical():
    assert (CODEX / "bus-autojoin.py").read_bytes() == AUTOJOIN.read_bytes(), \
        "both plugins install this to $HOME/.hivemind/bus-autojoin.py; the last one loaded wins"


def _code(path):
    """The module with every docstring removed — comments are not in the AST to begin with."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(tree))


def test_the_two_listener_copies_behave_identically():
    """bus-listen.py also installs to one shared path, but its two copies deliberately differ in
    prose — one explains Monitor, the other explains a Codex shell. So the CODE is what must
    match: a one-sided behavioural edit is the bug this whole file is about."""
    assert _code(CODEX / "bus-listen.py") == _code(CLAUDE / "bus-listen.py"), \
        "the two bus-listen.py copies differ in behaviour, not just in their host-specific prose"


# ── finding 4: the manual install sits outside the plugin tree ────────────────────────────────
def test_a_shallow_install_path_still_finds_the_saved_codex_server(mod, tmp_path, monkeypatch):
    """parents[3] exists only inside the plugin tree. The manual install is
    $HOME/.hivemind/bus-autojoin.py, which has exactly three parents when $HOME is one level below
    root (/root, as in a container or CI) — so indexing raised IndexError, the except swallowed it
    as "no endpoint", and the ~/.hivemind/codex-server.json branch it exists for never ran."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HIVEMIND_SERVER_URL", raising=False)
    state = tmp_path / ".hivemind"
    state.mkdir()
    (state / "codex-server.json").write_text(json.dumps({"server_url": "http://127.0.0.1:8787"}))
    monkeypatch.setattr(mod, "__file__", "/root/.hivemind/bus-autojoin.py")
    assert mod._endpoint("codex") == "http://127.0.0.1:8787/mcp"


# ── finding 6 / 3: the read cursor ─────────────────────────────────────────────────────────────
def _write_frames(path, ids, mode="a"):
    with path.open(mode) as out:
        for mid in ids:
            out.write(json.dumps({"type": "message", "id": mid, "body": "x"}) + "\n")


def test_only_unread_messages_are_announced_and_the_offset_advances(mod, tmp_path):
    inbox = tmp_path / "inbox-p.jsonl"
    state_path = tmp_path / "listener.json"
    state = {}
    _write_frames(inbox, ["m1", "m2", "m3"])
    assert "3 new message(s)" in mod._notice(inbox, state_path, state)
    assert state["seen_offset"] == inbox.stat().st_size
    assert mod._notice(inbox, state_path, state) == "", "nothing new must say nothing"
    _write_frames(inbox, ["m4", "m5"])
    assert "2 new message(s)" in mod._notice(inbox, state_path, state)
    assert state["seen_message"] == "m5"


def test_durable_chat_notification_points_to_server_catchup_not_legacy_bus(mod, tmp_path):
    inbox = tmp_path / "inbox-p.jsonl"
    state_path = tmp_path / "listener.json"
    inbox.write_text(json.dumps({"v": 2, "type": "chat", "id": "m1", "channel": "dm"}) + "\n")
    note = mod._notice(inbox, state_path, {})
    assert "chat_inbox" in note and "chat_room_history" in note
    assert "bus_send" not in note and "bus_message" not in note


def test_autojoin_chooses_canonical_chat_and_reminds_server_catchup(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session = "abcd-efgh"
    pin = tmp_path / ".hivemind" / ("session-%s.json" % session)
    pin.parent.mkdir()
    pin.write_text(json.dumps({"project": "demo"}))
    monkeypatch.setattr(mod, "_endpoint", lambda platform: "http://127.0.0.1:8787/mcp")
    monkeypatch.setattr(mod, "_token", lambda platform, endpoint: "token")
    seen = []

    def fake_rpc(endpoint, token, name, args):
        seen.append((name, args))
        return {"ws_url": "ws://127.0.0.1:8787/p/demo/chat/ws", "listen_key": "hk2.test"}

    class Child:
        pid = 12345

    monkeypatch.setattr(mod, "_rpc", fake_rpc)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: Child())
    note = mod.run({"session_id": session}, "codex", "ensure")
    assert seen[0] == ("chat_connect", {"project": "demo", "client": "codex",
                                           "session_id": session})
    assert "chat_inbox" in note and "24-hour" in note


def test_canonical_session_slug_cannot_collapse_distinct_long_host_ids(mod):
    left = "session-" + "a" * 80 + "left"
    right = "session-" + "a" * 80 + "right"
    one, two = mod._chat_session(left), mod._chat_session(right)
    assert one != two
    assert all(1 <= len(value) <= 64 and value.isascii() and value.lower() == value
               for value in (one, two))


def test_long_host_session_ids_have_distinct_pin_and_listener_paths(mod, tmp_path, monkeypatch):
    helper = CLAUDE / "hivemind-project.py"
    monkeypatch.setenv("HOME", str(tmp_path))
    first = "s" * 110 + "left"
    second = "s" * 110 + "right"
    one_pin, one_dir = mod._paths(first, "claude")
    two_pin, two_dir = mod._paths(second, "claude")
    assert one_pin != two_pin and one_dir != two_dir
    for sid, expected in ((first, one_pin), (second, two_pin)):
        monkeypatch.setenv("HIVEMIND_SESSION_ID", sid)
        call = subprocess.run([sys.executable, str(helper), "--pin", "demo"],
                              capture_output=True, text=True, check=True)
        assert call.returncode == 0 and expected.is_file()


def test_the_count_survives_a_rotation_without_re_announcing_it(mod, tmp_path):
    """The offset addresses one file. A rotation replaces the inbox with a shorter one, so the
    offset stops meaning anything and the id scan has to take over — otherwise every message in
    both generations is announced again."""
    inbox = tmp_path / "inbox-p.jsonl"
    state_path = tmp_path / "listener.json"
    state = {}
    _write_frames(inbox, ["m1", "m2", "m3"])
    mod._notice(inbox, state_path, state)
    inbox.rename(inbox.with_name(inbox.name + ".1"))          # what the listener does at the cap
    _write_frames(inbox, ["m4"], mode="w")
    assert "1 new message(s)" in mod._notice(inbox, state_path, state)


def test_a_half_written_frame_is_not_skipped(mod, tmp_path):
    """The listener appends while this runs, so the last line can be incomplete. Counting it would
    consume it: the offset would move past a frame that was never announced."""
    inbox = tmp_path / "inbox-p.jsonl"
    state_path = tmp_path / "listener.json"
    state = {}
    _write_frames(inbox, ["m1"])
    with inbox.open("a") as out:
        out.write('{"type": "message", "id": "m2", "bo')        # torn
    assert "1 new message(s)" in mod._notice(inbox, state_path, state)
    with inbox.open("a") as out:
        out.write('dy": "x"}\n')                                 # completed
    assert "1 new message(s)" in mod._notice(inbox, state_path, state), \
        "the frame that was torn last time must be announced once it is whole"


def _pin_and_state(tmp_path, session, project, extra):
    home = tmp_path
    hive = home / ".hivemind"
    (hive / "claude-bus" / session).mkdir(parents=True)
    (hive / ("session-%s.json" % session)).write_text(json.dumps({"project": project}))
    state_path = hive / "claude-bus" / session / "listener.json"
    state_path.write_text(json.dumps(extra))
    return state_path


def test_stopping_the_listener_keeps_where_this_session_had_read_to(mod, tmp_path, monkeypatch):
    """SessionEnd stops the listener but does not delete the inbox, and a --resume reuses the same
    session id and state directory. Clearing the whole state file discarded the cursor, so the
    next `ensure` announced the entire backlog — "412 new message(s) … read every full message
    now, work on its request"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session = "sess-stop"
    state_path = _pin_and_state(tmp_path, session, "demo", {
        "project": "demo", "endpoint": "http://127.0.0.1:8787/mcp", "pid": 999999,
        "marker": "d" * 24, "seen_message": "m9", "seen_inbox": "inbox-demo.jsonl",
        "seen_offset": 4096})
    mod.run({"session_id": session}, "claude", "stop")
    saved = json.loads(state_path.read_text())
    assert saved["seen_message"] == "m9" and saved["seen_offset"] == 4096
    assert saved["seen_inbox"] == "inbox-demo.jsonl" and saved["project"] == "demo"
    assert "pid" not in saved and "marker" not in saved, "the stopped process must not be claimed"


# ── finding 5: do not report a live listener as offline ───────────────────────────────────────
def test_a_live_listener_is_not_reported_as_offline_when_the_shell_lacks_the_url(
        mod, tmp_path, monkeypatch, listener_process):
    """The plugin publishes HIVEMIND_SERVER_URL through CLAUDE_ENV_FILE and
    CLAUDE_PLUGIN_OPTION_SERVER_URL is hook-only, so the agent running this command by hand can
    have neither while its listener is happily online. SKILL.md tells the agent to report any
    failure, so "not joined" here tells the user they are offline when they are not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("HIVEMIND_SERVER_URL", "CLAUDE_PLUGIN_OPTION_SERVER_URL"):
        monkeypatch.delenv(var, raising=False)
    session = "sess-live"
    marker = "e" * 24
    proc, _ = listener_process(tmp_path / "elsewhere", marker)
    _pin_and_state(tmp_path, session, "demo", {
        "project": "demo", "endpoint": "http://127.0.0.1:8787/mcp",
        "pid": proc.pid, "marker": marker})
    result = mod.run({"session_id": session}, "claude", "ensure")
    assert "not joined" not in result, result


# ── finding 8: the launcher's last line ────────────────────────────────────────────────────────
def test_the_launcher_explains_a_missing_codex_instead_of_a_traceback(tmp_path):
    """Every other failure in launch() is one sentence; the exec was not, so a user who HAS
    configured Hivemind but has no codex on PATH got a raw traceback that reads as our fault."""
    state = tmp_path / ".hivemind"
    state.mkdir()
    (state / "codex-server.json").write_text(json.dumps({"server_url": "http://127.0.0.1:8787"}))
    token = state / "codex-token"
    token.write_text("t\n")
    token.chmod(0o600)
    empty = tmp_path / "bin"
    empty.mkdir()
    r = subprocess.run([sys.executable, str(LAUNCHER)], capture_output=True, text=True,
                       env={"HOME": str(tmp_path), "PATH": str(empty)})
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    assert "codex" in r.stderr.lower() and "path" in r.stderr.lower(), r.stderr
