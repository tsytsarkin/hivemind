"""The SessionStart hook publishes the plugin's config to the session's shell.

Why this exists at all: `skills/hivemind/scripts/guide.sh`, the bus listener command and the
`hivemind` CLI all read `HIVEMIND_SERVER_URL` / `HIVEMIND_TOKEN` from the environment. On a
plugin-only machine nobody exports them, so the live guide fell back to its cached copy while the
plugin held the URL and the token the whole time.

Two facts about Claude Code make the hook the only possible bridge, and both were measured rather
than assumed (2.1.x):

  * a SessionStart hook is handed the user config as `CLAUDE_PLUGIN_OPTION_<KEY-UPPERCASED>`;
  * `$CLAUDE_ENV_FILE` is a shell fragment that later Bash tool calls in the session source.

Neither is visible to a plain Bash tool call — verified by dumping the environment of one — which is
why the values cannot simply be read where they are needed.

The direction that matters most here is the **override**: an environment variable that is already
set must win, so a machine deliberately pointed at another server or token is never quietly
redirected to the plugin's idea of them.
"""
import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
HOOK = REPO / "plugin" / "hooks" / "session-start"


def run_hook(tmp_path, env=None):
    """Run the hook with an isolated HOME and env file; return (stdout, env-file text, mode)."""
    envfile = tmp_path / "sessionstart-hook-0.sh"
    envfile.write_text("")
    os.chmod(envfile, 0o644)          # Claude Code creates it world-readable; the hook must narrow it
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("HIVEMIND_", "CLAUDE_PLUGIN_OPTION_"))}
    e.update({"HOME": str(tmp_path), "CLAUDE_ENV_FILE": str(envfile),
              "CLAUDE_PLUGIN_ROOT": str(REPO / "plugin")})
    e.update(env or {})
    r = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, env=e,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"a SessionStart hook must never fail: {r.stderr}"
    return r.stdout, envfile.read_text(), oct(envfile.stat().st_mode & 0o777)


def test_the_plugins_config_becomes_the_sessions_environment(tmp_path):
    _, written, _ = run_hook(tmp_path, {
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://box.example:8787/p/proj",
        "CLAUDE_PLUGIN_OPTION_API_TOKEN": "hm_fromplugin"})
    assert "export HIVEMIND_SERVER_URL=http://box.example:8787/p/proj" in written, written
    assert "hm_fromplugin" in written, written


def test_a_real_export_wins_because_the_environment_is_the_override(tmp_path):
    """The whole point: the plugin config is the fallback, never the authority."""
    _, written, _ = run_hook(tmp_path, {
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://plugin.example:8787/p/plugin",
        "CLAUDE_PLUGIN_OPTION_API_TOKEN": "hm_fromplugin",
        "HIVEMIND_SERVER_URL": "http://shell.example:8787/p/shell",
        "HIVEMIND_TOKEN": "hm_fromshell"})
    assert "plugin.example" not in written, f"the plugin config overrode a real export: {written}"
    assert "hm_fromplugin" not in written, "the plugin token overrode a real export"
    assert written.strip() == "", f"nothing should be written when both are already set: {written}"


def test_each_variable_is_decided_on_its_own(tmp_path):
    """A shell that exports only the URL must still be handed the token, and the reverse."""
    _, written, _ = run_hook(tmp_path, {
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://plugin.example:8787/p/plugin",
        "CLAUDE_PLUGIN_OPTION_API_TOKEN": "hm_fromplugin",
        "HIVEMIND_SERVER_URL": "http://shell.example:8787/p/shell"})
    assert "plugin.example" not in written, written
    assert "hm_fromplugin" in written, "the token was withheld because the URL happened to be set"


def test_the_token_is_not_left_in_a_world_readable_file(tmp_path):
    """Claude Code creates the env file 0644. A bearer token in it is worse than the stale guide
    this bridge exists to fix, so the hook narrows the file before writing to it."""
    _, written, mode = run_hook(tmp_path, {
        "CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://box.example:8787/p/proj",
        "CLAUDE_PLUGIN_OPTION_API_TOKEN": "hm_secret"})
    assert "hm_secret" in written, "precondition: the token was written"
    assert mode == "0o600", f"env file holding a token is {mode}, must be 0o600"


def test_nothing_is_written_when_the_plugin_has_no_config(tmp_path):
    _, written, _ = run_hook(tmp_path)
    assert "HIVEMIND_SERVER_URL" not in written and "HIVEMIND_TOKEN" not in written, written


def test_the_hook_still_emits_its_pin_context_and_never_fails(tmp_path):
    """The bridge must not break what the hook was already for."""
    out, _, _ = run_hook(tmp_path, {"CLAUDE_PLUGIN_OPTION_SERVER_URL": "http://b:8787/p/p"})
    assert out.strip(), "the hook printed nothing at all"
    assert "hookSpecificOutput" in out, out


def test_a_missing_env_file_is_survivable(tmp_path):
    """No CLAUDE_ENV_FILE at all — an older host, or another event — must not fail the session."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("HIVEMIND_", "CLAUDE_PLUGIN_OPTION_", "CLAUDE_ENV_FILE"))}
    e.update({"HOME": str(tmp_path), "CLAUDE_PLUGIN_ROOT": str(REPO / "plugin"),
              "CLAUDE_PLUGIN_OPTION_API_TOKEN": "hm_x"})
    r = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, env=e,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert "hookSpecificOutput" in r.stdout
