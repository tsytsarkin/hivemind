"""The pin file and the hook that re-injects it. Both must be stdlib-only and never block."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
HELPER = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "hivemind-project.py"
HOOK = ROOT / "plugin" / "hooks" / "session-start"
HOOKS_JSON = ROOT / "plugin" / "hooks" / "hooks.json"
MANIFEST = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
COMMAND = ROOT / "plugin" / "commands" / "project.md"
SKILL = ROOT / "plugin" / "skills" / "hivemind" / "SKILL.md"
GUIDE_SH = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "guide.sh"


def test_the_helper_and_hook_ship_in_the_plugin():
    assert HELPER.is_file() and HOOK.is_file()


def test_the_helper_is_stdlib_only():
    import ast
    std = getattr(sys, "stdlib_module_names", None)
    if not std:
        pytest.skip("need python 3.10+")
    mods = set()
    for n in ast.walk(ast.parse(HELPER.read_text())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods - set(std))


def _run(args, home, session="sess-1"):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if session is not None:                       # session=None means "the harness set no id"
        env["CLAUDE_CODE_SESSION_ID"] = session
    return subprocess.run([sys.executable, str(HELPER)] + args, capture_output=True, text=True,
                          env=env)


def test_pin_then_show_round_trips(tmp_path):
    assert _run(["--pin", "nik.private"], tmp_path).returncode == 0
    out = json.loads(_run(["--show"], tmp_path).stdout)
    assert out["project"] == "nik.private"


def test_a_different_session_has_its_own_pin(tmp_path):
    _run(["--pin", "nik.private"], tmp_path, session="sess-1")
    out = json.loads(_run(["--show"], tmp_path, session="sess-2").stdout)
    assert out.get("project") is None


def test_the_same_session_id_resumes_onto_the_same_project(tmp_path):
    _run(["--pin", "nik.s-abc"], tmp_path, session="sess-9")
    out = json.loads(_run(["--show"], tmp_path, session="sess-9").stdout)
    assert out["project"] == "nik.s-abc"


def _hook(home, session="sess-1", plugin_root=ROOT / "plugin", helper=None):
    env = {"PATH": "/usr/bin:/bin"}
    if home is not None:                          # home=None means "the harness set no HOME"
        env["HOME"] = str(home)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    if helper is not None:
        env["HIVEMIND_PIN_HELPER"] = str(helper)
    return subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, env=env)


def test_the_hook_emits_valid_json_with_the_pin(tmp_path):
    _run(["--pin", "nik.private"], tmp_path)
    r = _hook(tmp_path)
    assert r.returncode == 0
    body = json.loads(r.stdout)
    ctx = body["hookSpecificOutput"]["additionalContext"]
    assert "nik.private" in ctx and "project=nik.private" in ctx


def test_the_hook_asks_for_a_choice_when_nothing_is_pinned(tmp_path):
    r = _hook(tmp_path)
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "project_list" in ctx and "ask" in ctx.lower()


def test_the_hook_survives_an_unwritable_home(tmp_path):
    """It must never block a session."""
    missing = tmp_path / "nope" / "deeper"
    r = _hook(missing)
    assert r.returncode == 0
    json.loads(r.stdout)


def test_a_hostile_label_cannot_break_the_injected_json(tmp_path):
    """Review Focus 5: the label is interpolated into JSON; a raw quote or newline would drop it."""
    nasty = 'broke"n\nlabel\\with\ttabs' + "\x1b[31m" + "x" * 500
    _run(["--pin", "nik.private", "--label", nasty], tmp_path)
    r = _hook(tmp_path)
    body = json.loads(r.stdout)                       # must parse
    ctx = body["hookSpecificOutput"]["additionalContext"]
    assert "nik.private" in ctx
    assert "\n" not in ctx.split("project=")[0][-80:]
    stored = json.loads((tmp_path / ".hivemind" / "session-sess-1.json").read_text())
    assert len(stored["label"]) <= 200 and "\x1b" not in stored["label"]


# ── the rest of the hook's failure paths: every one still yields JSON and exit 0 ───────────────
def test_the_hook_survives_the_helper_being_absent(tmp_path):
    """A session on a machine where the plugin root is not exported and the skill never loaded."""
    r = _hook(tmp_path, plugin_root=None)
    assert r.returncode == 0
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "project_list" in ctx, "with no helper it must still ask for a choice"


def test_the_hook_uses_the_copy_the_skill_installed(tmp_path):
    """The production path: the skill has loaded, so $HOME holds the helper and the plugin
    directory is not needed (nor nameable by anything the agent itself runs)."""
    installed = tmp_path / ".hivemind" / "hivemind-project.py"
    installed.parent.mkdir(parents=True)
    installed.write_text(HELPER.read_text())
    _run(["--pin", "nik.private"], tmp_path)
    ctx = json.loads(_hook(tmp_path, plugin_root=None).stdout)
    ctx = ctx["hookSpecificOutput"]["additionalContext"]
    assert "project=nik.private" in ctx


def test_the_hook_survives_a_malformed_pin_file(tmp_path):
    """Hand-edited, half-written, or written by a future version with a different shape."""
    pin = tmp_path / ".hivemind" / "session-sess-1.json"
    pin.parent.mkdir(parents=True)
    for junk in ("{not json", "[]", '{"project": {"nested": 1}}', '{"label": 5}', ""):
        pin.write_text(junk)
        shown = _run(["--show"], tmp_path)
        assert shown.returncode == 0, (junk, shown.stderr)
        assert json.loads(shown.stdout).get("project") is None, junk
        r = _hook(tmp_path)
        assert r.returncode == 0, junk
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "project_list" in ctx, junk


def test_the_hook_and_the_helper_survive_no_session_id(tmp_path):
    """CLAUDE_CODE_SESSION_ID is measured present, but its absence must not be a crash."""
    assert _run(["--pin", "nik.private"], tmp_path, session=None).returncode == 0
    r = _hook(tmp_path, session=None)
    assert r.returncode == 0
    json.loads(r.stdout)


def test_the_hook_does_not_trust_what_the_helper_prints(tmp_path):
    """The $HOME copy is refreshed only when the skill loads, so it can be an older version than
    this hook — or a python that failed halfway through a line. Either way: valid JSON, exit 0."""
    fake = tmp_path / "fake-helper.py"
    for body in ("print('not json at all')", "print('[]')", "raise SystemExit('boom')", "",
                 "print('{\"project\": {\"a\": 1}, \"label\": 5}')"):
        fake.write_text(body + "\n")
        r = _hook(tmp_path, helper=fake)
        assert r.returncode == 0, (body, r.stderr)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "project_list" in ctx, body
    # and a value of the wrong type is dropped rather than stringified into the prose
    fake.write_text('print(\'{"project": "nik.private", "label": 5}\')\n')
    ctx = json.loads(_hook(tmp_path, helper=fake).stdout)
    ctx = ctx["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Hivemind project for this session: nik.private."), ctx


def test_the_hook_survives_a_machine_with_no_python3(tmp_path):
    """python3 is what builds the JSON, so without it there is no context to add — but the hook
    still has to exit 0, or a SessionStart failure surfaces over a missing aide-memoire."""
    import shutil
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "bash").symlink_to(shutil.which("bash"))
    r = subprocess.run([str(bindir / "bash"), str(HOOK)], capture_output=True, text=True,
                       env={"HOME": str(tmp_path), "PATH": str(bindir),
                            "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin"),
                            "CLAUDE_CODE_SESSION_ID": "sess-1"})
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_survives_an_unset_home():
    """`set -u` plus a bare $HOME would abort before anything was printed. Read-only: --show never
    writes, and no session is pinned under this id."""
    r = _hook(None, session="unset-home-probe")
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout)


def test_a_hostile_session_id_cannot_write_outside_the_pin_directory(tmp_path):
    """The id becomes part of a filename, so it is slugged and not merely cleaned."""
    home = tmp_path / "home"
    home.mkdir()
    _run(["--pin", "nik.private"], home, session="../../escaped")
    assert not (tmp_path / "escaped.json").exists()
    assert list((home / ".hivemind").glob("session-*.json"))


def test_a_pin_that_cannot_be_written_says_so(tmp_path):
    """The hook treats a missing pin as "not pinned", so --pin must not report a success it did not
    have: the agent would otherwise believe a choice was recorded that no later session can read."""
    import os
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode")
    d = tmp_path / ".hivemind"
    d.mkdir()
    d.chmod(0o500)
    try:
        r = _run(["--pin", "nik.private"], tmp_path)
    finally:
        d.chmod(0o700)
    assert r.returncode == 1 and "error" in json.loads(r.stdout)


def test_the_helper_reports_the_session_id():
    r = subprocess.run([sys.executable, str(HELPER), "--session-id"], capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "CLAUDE_CODE_SESSION_ID": "sid-7"})
    assert r.returncode == 0 and r.stdout.strip() == "sid-7"


# ── registration: an unregistered hook is a hook that never runs ──────────────────────────────
def test_the_hook_is_registered_for_startup_clear_and_compact():
    cfg = json.loads(HOOKS_JSON.read_text())
    entries = cfg["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "startup|clear|compact"
    cmds = [h["command"] for h in entries[0]["hooks"]]
    assert cmds == ['bash "${CLAUDE_PLUGIN_ROOT}/hooks/session-start"'], cmds


def test_the_manifest_points_at_the_hooks_file_and_the_versions_agree():
    manifest = json.loads(MANIFEST.read_text())
    hooks_rel = manifest["hooks"]
    assert (MANIFEST.parent.parent / hooks_rel).resolve() == HOOKS_JSON.resolve()
    front = SKILL.read_text().split("---")[1]
    assert f'version: "{manifest["version"]}"' in front, "SKILL.md metadata must not drift"


# ── the agent's own entry points name a path a plain shell can expand ──────────────────────────
def test_guide_sh_installs_the_pin_helper(tmp_path):
    """The hook and the slash command both name $HOME/.hivemind, so a skill load must put it
    there — neither CLAUDE_PLUGIN_ROOT nor CLAUDE_SKILL_DIR is set in a plain shell."""
    dst = tmp_path / "nested" / "hivemind-project.py"
    subprocess.run(["bash", str(GUIDE_SH), "--section", "core"], capture_output=True,
                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                        "HIVEMIND_PIN_HELPER": str(dst)}, check=False)
    assert dst.is_file(), "a skill load must install the pin helper"
    assert dst.read_text() == HELPER.read_text()


def test_the_slash_command_pins_through_the_home_copy():
    body = COMMAND.read_text()
    assert '$HOME/.hivemind/hivemind-project.py' in body
    assert "CLAUDE_PLUGIN_ROOT" not in body and "CLAUDE_SKILL_DIR" not in body, \
        "the agent runs this in a plain shell, where neither variable is set"
    assert "project_list" in body and "project_create" in body
