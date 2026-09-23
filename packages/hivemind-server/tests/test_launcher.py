"""`scripts/hivemind-claude` — run Claude Code with the plugin, without installing it.

Every test here drives the script's `--dry-run --no-check` path, so nothing touches the network and
nothing launches an editor. What is worth pinning is the handful of facts that were established by
measurement against Claude Code and would fail *silently* if they drifted:

  * the `pluginConfigs` key for a `--plugin-dir` plugin is the bare plugin name. The
    `name@marketplace` forms are accepted without complaint and then ignored, leaving the
    `userConfig` defaults in place — so the session would quietly talk to the default address with
    no token instead of erroring.
  * the token must never reach the `claude` argv, where `ps` shows it to every user on the host.
  * the script must not `exec`, because the EXIT trap that removes the token file cannot fire
    afterwards.
"""
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "scripts" / "hivemind-claude"


def run(*args, env=None):
    e = dict(os.environ)
    # Never let the developer's own values leak in and make a test pass for the wrong reason.
    e.pop("HIVEMIND_SERVER_URL", None)
    e.pop("HIVEMIND_TOKEN", None)
    e.update(env or {})
    # stdin closed, always: the script prompts only on a tty, and pytest may or may not give it
    # one. Pinning it here is what makes the "a missing token fails" test mean the same thing
    # under `pytest` as it does under `pytest -s`.
    return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, env=e,
                          stdin=subprocess.DEVNULL)


def dry(*args, env=None):
    r = run("--dry-run", "--no-check", *args, env=env)
    assert r.returncode == 0, f"exit {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return dict(
        line.split("=", 1) for line in r.stdout.splitlines() if "=" in line
    )


def test_the_launcher_ships_and_is_executable():
    assert LAUNCHER.is_file(), f"{LAUNCHER} is missing"
    assert os.access(LAUNCHER, os.X_OK), "the launcher must be executable to be a launcher"


def test_it_parses_under_the_system_shell():
    """macOS ships bash 3.2, so a bashism from a newer release is a real portability failure."""
    r = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_plugin_config_key_is_the_bare_plugin_name():
    """Measured: `hivemind@local` and `hivemind@hivemind-marketplace` are ignored for a
    --plugin-dir plugin and the userConfig defaults win instead. A rename here does not raise —
    it silently sends the session to the default address with no token."""
    out = dry("--url", "h.example:8787", "--token", "T")
    assert '"pluginConfigs":{"hivemind":{' in out["settings"], out["settings"]


def test_the_settings_file_is_not_readable_by_other_users():
    out = dry("--url", "h.example:8787", "--token", "T")
    assert out["settings_mode"] == "600", f"token file mode {out['settings_mode']}"


def test_the_token_never_reaches_the_claude_argv():
    """`ps` is world-readable. The token goes in the 0600 file or nowhere."""
    out = dry("--url", "h.example:8787", "--token", "hunter2-do-not-leak")
    assert "hunter2-do-not-leak" not in out["argv"], out["argv"]
    assert "--settings" in out["argv"] and "--plugin-dir" in out["argv"]


def test_the_real_invocation_passes_nothing_but_the_built_argv():
    """The test above only reads what --dry-run *prints*. That is a different code path from the
    launch itself, so on its own it is satisfied by a script that prints a clean argv and then
    hands the token to claude anyway — measured: adding `--settings-token "$TOKEN"` to the real
    invocation passed the whole suite. Pin the call site itself.
    """
    body = LAUNCHER.read_text()
    calls = re.findall(r"^\s*claude\s+(.*)$", body, re.M)
    assert calls, "no claude invocation found — did the launch line move?"
    for args in calls:
        assert args.strip().startswith('"$@"'), (
            f'claude must be invoked with "$@" and nothing prepended, got: claude {args}')
        assert "TOKEN" not in args, f"the token must not appear at the call site: claude {args}"


def test_a_token_containing_regex_metacharacters_is_still_redacted():
    r"""The redaction emits a placeholder rather than rewriting the file, because a regex over a
    secret fails open: `p.*$[x]\` would not match itself and would print in full."""
    out = dry("--url", "h.example:8787", "--token", r"p.*$[x]\ ")
    assert '"api_token":"***"' in out["settings"], out["settings"]
    assert "p.*$" not in out["settings"], out["settings"]


def test_a_bare_host_and_port_becomes_a_full_project_url():
    out = dry("--url", "h.example:8787", "--token", "T")
    assert out["project_url"] == "http://h.example:8787/p/default"


def test_a_url_that_already_names_a_project_is_left_alone():
    out = dry("--url", "https://h.example:9000/p/myproj", "--token", "T")
    assert out["project_url"] == "https://h.example:9000/p/myproj"


def test_the_project_flag_applies_and_a_trailing_slash_does_not_double_up():
    out = dry("--url", "http://h.example:8787/", "--project", "scratch", "--token", "T")
    assert out["project_url"] == "http://h.example:8787/p/scratch"


def test_unrecognised_arguments_are_forwarded_to_claude():
    """`hivemind-claude --resume` has to just work, or people will stop using the wrapper."""
    out = dry("--url", "h.example:8787", "--token", "T", "--resume", "--model", "sonnet")
    assert out["argv"].endswith("--resume --model sonnet"), out["argv"]


def test_arguments_after_a_double_dash_are_forwarded_verbatim():
    out = dry("--url", "h.example:8787", "--token", "T", "--", "--model", "opus")
    assert out["argv"].endswith("--model opus"), out["argv"]


def test_the_environment_supplies_both_values_so_it_can_run_unattended():
    out = dry(env={"HIVEMIND_SERVER_URL": "env.example:7777", "HIVEMIND_TOKEN": "E"})
    assert out["project_url"] == "http://env.example:7777/p/default"
    assert '"api_token":"***"' in out["settings"]


def test_a_missing_token_fails_before_launching_rather_than_401ing_every_call():
    # stdin is closed by `run`, so the prompt is skipped and the requirement bites.
    r = run("--dry-run", "--no-check", "--url", "h.example:8787")
    assert r.returncode != 0
    assert "token is required" in r.stderr, r.stderr


def test_it_does_not_exec_because_the_cleanup_trap_must_still_fire():
    """A trap cannot run after exec, and the file it removes holds a bearer token."""
    body = LAUNCHER.read_text()
    assert not re.search(r"^\s*exec\s+claude\b", body, re.M), \
        "exec'ing claude orphans the token file in TMPDIR for the rest of the session"
    assert "trap cleanup EXIT" in body


def test_an_unknown_plugin_directory_is_refused_with_the_path_it_looked_at():
    r = run("--dry-run", "--no-check", "--url", "h.example:8787", "--token", "T",
            "--plugin-dir", "/nonexistent/hivemind-plugin")
    assert r.returncode != 0
    assert "/nonexistent/hivemind-plugin" in r.stderr, r.stderr
