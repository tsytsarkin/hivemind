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

The address it hands the plugin is the SERVER ROOT, matching the plugin's own default since 1.2.0.
That is not cosmetic: on the root a call naming no project is refused, and on a project URL it
silently acts in whatever the URL named — so a launcher that kept appending `/p/default` would hand a
borrowed machine the looser mode while an installed one failed closed. `--project` opts back into the
older shape, and what it costs to omit it is stated in the usage text and on stderr at launch.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

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


def test_a_bare_host_and_port_becomes_the_server_root():
    """No `/p/...` appended: the plugin's own default is the root, and this is the entry point that
    has no install to inherit it from."""
    out = dry("--url", "h.example:8787", "--token", "T")
    assert out["server_url"] == "http://h.example:8787"


def test_a_url_that_already_names_a_project_is_left_alone():
    out = dry("--url", "https://h.example:9000/p/myproj", "--token", "T")
    assert out["server_url"] == "https://h.example:9000/p/myproj"


def test_a_url_that_names_a_project_wins_over_the_flag():
    """The flag selects a SHAPE; a URL that already has one is not reshaped by it."""
    out = dry("--url", "https://h.example:9000/p/myproj", "--project", "other", "--token", "T")
    assert out["server_url"] == "https://h.example:9000/p/myproj"


def test_the_project_flag_selects_the_older_shape_and_a_trailing_slash_does_not_double_up():
    out = dry("--url", "http://h.example:8787/", "--project", "scratch", "--token", "T")
    assert out["server_url"] == "http://h.example:8787/p/scratch"


def test_launching_on_the_root_says_what_having_no_project_costs():
    """The intended shape, and a refusal on the first Hivemind call is a confusing way to learn it.

    Stated on stderr where someone meets it, not only in the docs: no project until
    /hivemind:project pins one, calls refused until then, and the flag that opts out.
    """
    r = run("--dry-run", "--no-check", "--url", "h.example:8787", "--token", "T")
    assert r.returncode == 0, r.stderr
    assert "server root" in r.stderr and "/hivemind:project" in r.stderr, r.stderr
    assert "refused" in r.stderr and "--project" in r.stderr, r.stderr


def test_the_older_shape_gets_no_such_notice_because_it_has_a_project():
    r = run("--dry-run", "--no-check", "--url", "h.example:8787", "--project", "scratch",
            "--token", "T")
    assert r.returncode == 0, r.stderr
    assert "/hivemind:project" not in r.stderr, r.stderr


def test_the_usage_text_says_what_the_project_flag_does_and_what_omitting_it_costs():
    """`--project NAME` used to read "project to use", which is no longer what it does: it changes
    the SHAPE of the address, and with it whether an omitted project= is refused or defaulted.

    Scoped to the flag's OWN entry, not to the whole help text: the mutation that deletes the
    consequence paragraph left "/hivemind:project" and "refused" elsewhere on the page — in the
    examples and in the other half of this entry — so a whole-page assertion passed with the thing
    it was meant to pin already gone.
    """
    r = run("--help")
    assert r.returncode == 0, r.stderr
    body = r.stdout
    assert "server root" in body.lower(), body
    # the block belonging to --project: up to the next option at the same indent
    entry = body.split("  --project NAME", 1)
    assert len(entry) == 2, body
    entry = re.split(r"\n  --\w", entry[1], maxsplit=1)[0]
    assert "/hivemind:project" in entry, entry
    assert "refused" in entry and "no project" in entry.lower(), entry


def test_unrecognised_arguments_are_forwarded_to_claude():
    """`hivemind-claude --resume` has to just work, or people will stop using the wrapper."""
    out = dry("--url", "h.example:8787", "--token", "T", "--resume", "--model", "sonnet")
    assert out["argv"].endswith("--resume --model sonnet"), out["argv"]


def test_arguments_after_a_double_dash_are_forwarded_verbatim():
    out = dry("--url", "h.example:8787", "--token", "T", "--", "--model", "opus")
    assert out["argv"].endswith("--model opus"), out["argv"]


def test_the_environment_supplies_both_values_so_it_can_run_unattended():
    out = dry(env={"HIVEMIND_SERVER_URL": "env.example:7777", "HIVEMIND_TOKEN": "E"})
    assert out["server_url"] == "http://env.example:7777"
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


# ── the preflight probes the surface the URL actually has ────────────────────────────────────────
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A real server, because the preflight is HTTP and the bug it can have is choosing the wrong
    path. `/guide` exists only under `/p/<project>/` and `/projects` only off the root, so a probe
    aimed at the wrong one warns about a correctly configured launch — which no dry-run test can
    see."""
    import uvicorn
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "d" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    from hivemind_server import app as appmod
    from hivemind_server.config import Config
    from hivemind_server.identity import IdentityStore
    application = appmod.build_app(Config())
    proj = application.state.registry.all()[0]
    legacy = next(iter(json.loads((proj.dir / "tokens.json").read_text())))
    identity = IdentityStore(application.state.cfg.identities_path).mint("launcher", "test")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                        log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started: break
        time.sleep(0.05)
    yield {"root": f"http://127.0.0.1:{port}", "project": proj.name,
           "identity": identity, "legacy": legacy}
    srv.should_exit = True; th.join(timeout=5)


def _warnings(r):
    return [ln for ln in r.stderr.splitlines() if "warning:" in ln]


def test_a_root_url_with_a_good_token_preflights_clean(live):
    """Off the root the probe has to be /projects. Aimed at /guide it would 404 for every correctly
    configured root URL — a warning about nothing, which is worse than no preflight."""
    r = run("--dry-run", "--url", live["root"], "--token", live["identity"])
    assert r.returncode == 0, r.stderr
    assert _warnings(r) == [], r.stderr


def test_a_project_url_with_its_own_token_preflights_clean(live):
    """And the reverse: under /p/<project> the probe has to stay /guide. A legacy per-project token
    is 401 on /projects however valid it is, so probing the root here would reject a working setup."""
    r = run("--dry-run", "--url", live["root"] + "/p/" + live["project"],
            "--token", live["legacy"])
    assert r.returncode == 0, r.stderr
    assert _warnings(r) == [], r.stderr


def test_a_rejected_token_is_named_on_the_root_with_the_one_hint_that_explains_it(live):
    """A legacy per-project token cannot be used with a root URL at all — 401 there however valid it
    is, because it is pinned to a project the URL has not named. That is the misconfiguration this
    change makes possible, so the warning names it rather than leaving "401" to be interpreted."""
    r = run("--dry-run", "--url", live["root"], "--token", live["legacy"])
    assert r.returncode == 0, r.stderr
    warns = _warnings(r)
    assert warns and "401" in warns[0], r.stderr
    assert "per-project token" in warns[0], warns


def test_a_dead_address_still_blames_the_address(live):
    r = run("--dry-run", "--url", "127.0.0.1:%d" % _free_port(), "--token", "T")
    assert r.returncode == 0, r.stderr
    assert any("healthz did not answer" in w for w in _warnings(r)), r.stderr
