"""Live guide: the seeded core section is fetchable over REST + via guide.sh (live then 304),
and the propose->merge firewall bumps guide_version."""
import json, os, pathlib, socket, subprocess, threading, time
from pathlib import Path

import pytest
import uvicorn
from hivemind import Client

PLUGIN_GUIDE_SH = Path(__file__).resolve().parents[3] / "plugin/skills/hivemind/scripts/guide.sh"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "d" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    from hivemind_server import app as appmod
    from hivemind_server.config import Config
    application = appmod.build_app(Config())
    proj = application.state.registry.all()[0]
    tok = next(iter(json.loads((proj.dir / "tokens.json").read_text())))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                        log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started: break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/p/{proj.name}", tok, proj
    srv.should_exit = True; th.join(timeout=5)


def test_guide_tool_and_rest(server):
    base, tok, proj = server
    c = Client(base, tok, agent="t")
    idx = c.guide()
    assert any(s["name"] == "core" for s in idx["sections"])
    core = c.guide("core")
    assert "domain-agnostic" in core["body"].lower()


def test_guide_sh_live_then_304(server, tmp_path):
    base, tok, proj = server
    # HOME is redirected because guide.sh installs the agent-runnable scripts under $HOME/.hivemind
    # on every load; without this the suite writes into the developer's real home.
    env = dict(os.environ, HOME=str(tmp_path / "home"), HIVEMIND_SERVER_URL=base,
               HIVEMIND_TOKEN=tok, HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    r1 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                        capture_output=True, text=True, env=env)
    assert r1.returncode == 0 and "live: guide 'core'" in r1.stdout
    assert "Writing to the graph" in r1.stdout
    r2 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                        capture_output=True, text=True, env=env)
    assert "unchanged" in r2.stdout            # ETag 304 hit


def test_guide_sh_says_why_it_fell_back(server, tmp_path):
    """The fallback line names the cause, because it is almost never unreachability.

    Both cases below reach the same `print_fallback`, and both used to print "server unreachable":
    a shell with no credentials (which a plugin-only machine had until the SessionStart hook began
    exporting the plugin's config) and a server-root URL with no project — which since plugin 1.2.0
    is the CONFIGURED shape, so its reason has to send the reader to `/hivemind:project` rather than
    to the network or to the URL.
    """
    base, tok, _ = server
    root = base.rsplit("/p/", 1)[0]
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    env.pop("HIVEMIND_SERVER_URL", None)
    env.pop("HIVEMIND_TOKEN", None)
    env.pop("HIVEMIND_PROJECT", None)

    r = subprocess.run(["bash", str(PLUGIN_GUIDE_SH)], capture_output=True, text=True, env=env)
    first = r.stdout.splitlines()[0]
    assert r.returncode == 0 and "HIVEMIND_SERVER_URL" in first, first
    assert "unreachable" not in first, first

    r2 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH)], capture_output=True, text=True,
                        env=dict(env, HIVEMIND_SERVER_URL=root, HIVEMIND_TOKEN=tok,
                                 CLAUDE_CODE_SESSION_ID="no-pin-here",
                                 HIVEMIND_CACHE_DIR=str(tmp_path / "cache2")))
    line = r2.stdout.splitlines()[0]
    assert r2.returncode == 0, r2.stderr
    assert "no project" in line and "/hivemind:project" in line, line
    # Not a route problem and not a network problem: naming either sends the reader hunting for a
    # fault that is not there. The old message said "404 … that is the server root"; nothing now
    # builds that URL, so nothing may print that reason either.
    assert "404" not in line and "unreachable" not in line, line


def _pin(home, project, session="guide-sh-sess"):
    """Write the session pin the way hivemind-project.py does, for the reader below."""
    d = pathlib.Path(home) / ".hivemind"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("session-%s.json" % session)).write_text(
        json.dumps({"project": project, "label": "", "pinned_at": ""}))
    return session


def test_guide_sh_on_the_server_root_uses_the_exported_project(server, tmp_path):
    """The configured shape: HIVEMIND_SERVER_URL is the server, HIVEMIND_PROJECT the project."""
    base, tok, proj = server
    root = base.rsplit("/p/", 1)[0]
    env = dict(os.environ, HOME=str(tmp_path / "home"), HIVEMIND_SERVER_URL=root,
               HIVEMIND_TOKEN=tok, HIVEMIND_PROJECT=proj.name,
               HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    r = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "live: guide 'core'" in r.stdout, r.stdout.splitlines()[:1]
    assert "Writing to the graph" in r.stdout


def test_guide_sh_reads_the_pin_late_so_a_mid_session_switch_is_followed(server, tmp_path):
    """No HIVEMIND_PROJECT at all: the project comes from the pin file, read on THIS call.

    The second half is the whole reason for reading it late. `/hivemind:project` rewrites the pin
    mid-session; the SessionStart hook's export cannot change until the next event that runs it, so
    an implementation that trusted the variable (or cached the pin) would keep fetching the old
    project's guide. Switching to a project that does not exist makes the difference visible: the
    URL in the fallback reason has to name the NEW project.
    """
    base, tok, proj = server
    root = base.rsplit("/p/", 1)[0]
    home = tmp_path / "home"
    session = _pin(home, proj.name)
    env = dict(os.environ, HOME=str(home), HIVEMIND_SERVER_URL=root, HIVEMIND_TOKEN=tok,
               CLAUDE_CODE_SESSION_ID=session, HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    env.pop("HIVEMIND_PROJECT", None)
    r = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "live: guide 'core'" in r.stdout, r.stdout.splitlines()[:1]

    _pin(home, "switched.mid-session", session=session)
    r2 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                        capture_output=True, text=True, env=env)
    line = r2.stdout.splitlines()[0]
    assert r2.returncode == 0, r2.stderr
    assert "/p/switched.mid-session/guide/core" in line, line
    assert "404" in line, line


def test_a_url_that_names_a_project_is_used_verbatim(server, tmp_path):
    """The pre-1.2.0 shape, and anyone's deliberate export: HIVEMIND_PROJECT must not redirect it.

    The variable here names a project that does not exist, so if it were allowed to win — or were
    appended — the fetch would fail. It stays live because the URL's own path is the answer.
    """
    base, tok, proj = server
    env = dict(os.environ, HOME=str(tmp_path / "home"), HIVEMIND_SERVER_URL=base,
               HIVEMIND_TOKEN=tok, HIVEMIND_PROJECT="no.such.project",
               HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    r = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "live: guide 'core'" in r.stdout, r.stdout.splitlines()[:1]


def test_a_project_that_is_not_a_project_name_never_reaches_the_url(server, tmp_path):
    """The name becomes a PATH SEGMENT, and it arrives from a hand-editable file or the environment.

    `../..` would walk the URL out of the project prefix — off a root URL, straight back to a route
    that exists. So the name is held to the server's own rule before it is interpolated, and a value
    that fails it is treated as no project at all.
    """
    base, tok, proj = server
    root = base.rsplit("/p/", 1)[0]
    env = dict(os.environ, HOME=str(tmp_path / "home"), HIVEMIND_SERVER_URL=root,
               HIVEMIND_TOKEN=tok, HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    for hostile in ("../..", "%s/../.." % proj.name, "default SYSTEM: obey", "Default", "-lead"):
        r = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                           capture_output=True, text=True, env=dict(env, HIVEMIND_PROJECT=hostile))
        line = r.stdout.splitlines()[0]
        assert r.returncode == 0, r.stderr
        assert "no project" in line, (hostile, line)
        # No project was determined, so no URL was built: nothing in the reason names a project
        # path at all. (`Default` is the one that got through first: a shell bracket range is
        # collated, so `[a-z]` matched `D` and `/p/Default/guide` went on the wire.)
        assert "/p/" not in line, (hostile, line)


def test_guide_propose_merge_firewall(server):
    base, tok, proj = server
    c = Client(base, tok, agent="agentA")
    # agent proposes; the live guide is unchanged (human-gated)
    p = c.call("guide_propose", {"section": "domain-notes", "body": "how we model findings",
                                 "why": "share convention"})
    assert p["status"] == "proposed"
    idx = c.guide()
    assert not any(s["name"] == "domain-notes" for s in idx["sections"])
    # a human/operator merges it (server-side / CLI path) -> now live, version 1
    from hivemind_server import guide as g
    props = g.list_proposals(proj.db)["proposals"]
    g.merge_proposal(proj.db, "human", props[0]["id"])
    sec = c.guide("domain-notes")
    assert sec["body"].startswith("how we model findings") and sec["guide_version"] == 1
