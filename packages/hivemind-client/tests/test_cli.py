"""Drive the `hivemind` CLI in-process against a live server: apply pack, upsert, search,
artifact put, tool search — asserting JSON output.

Twice over, because the CLI has two ways to know which project it is acting in: a project base URL
(the pre-1.2.0 shape, `test_cli_flow`) and the server root plus a project
(`test_cli_flow_against_the_server_root`, the shape plugin 1.2.0 configures)."""
import json, os, socket, threading, time
from pathlib import Path

import pytest
import uvicorn
from hivemind import cli

REPO = Path(__file__).resolve().parents[3]


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    monkeypatch.delenv("HIVEMIND_PROJECT", raising=False)
    monkeypatch.setenv("HIVEMIND_SERVER_URL", f"http://127.0.0.1:{port}/p/{proj.name}")
    monkeypatch.setenv("HIVEMIND_TOKEN", tok)
    from hivemind_server.identity import IdentityStore
    identity = IdentityStore(application.state.cfg.identities_path).mint("cli", "test")
    yield {"root": f"http://127.0.0.1:{port}", "project": proj.name, "identity_token": identity}
    srv.should_exit = True; th.join(timeout=5)


@pytest.fixture()
def root_env(env, monkeypatch):
    """The same server, addressed as the SERVER ROOT — no project anywhere in the URL.

    With a server-level identity token, not the project's legacy one: the neutral endpoint answers
    a legacy token `401`, because that token is pinned to a project this URL has not named. Any
    test here that got a 401 would otherwise be reading it as "no project".
    """
    monkeypatch.setenv("HIVEMIND_SERVER_URL", env["root"])
    monkeypatch.setenv("HIVEMIND_TOKEN", env["identity_token"])
    return env["root"], env["project"]


def _run(capsys, *argv):
    assert cli.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_flow(env, capsys, tmp_path):
    assert _run(capsys, "health")["ok"] is True

    # apply the real security-research pack
    pack = str(REPO / "packs/security-research/schema.json")
    applied = _run(capsys, "schema", "apply", pack)
    assert "finding" in " ".join(applied["created"]["node"])

    # upsert a finding, then search for it
    node = _run(capsys, "node", "upsert", "--type", "finding", "--props",
                '{"title":"heap overflow","severity":"high"}')
    assert node["ok"] and node["created"]
    hits = _run(capsys, "search", "heap")
    assert any("heap overflow" in r["snippet"] for r in hits["results"])

    # subject-versioned component via CLI flags
    _run(capsys, "node", "upsert", "--type", "component", "--props", '{"name":"X"}',
         "--subject-key", "X", "--subject-version", "26.6", "--subject-order", "0266")
    subs = _run(capsys, "node", "subjects", "X")
    assert subs["cells"][0]["subject_version"] == "26.6"

    # artifact put via CLI
    f = tmp_path / "eviden.bin"; f.write_bytes(b"\x00\x01\x02" * 1000)
    art = _run(capsys, "artifact", "put", str(f))
    assert art["digest"].startswith("sha256:")

    # publish + search a tool via CLI
    tool = tmp_path / "t.py"; tool.write_text("# /// script\n# requires-python = '>=3.11'\n# ///\nprint('hi')\n")
    pub = _run(capsys, "tool", "publish", str(tool), "--id", "lab/t", "--version", "0.1.0",
               "--description", "demo")
    assert pub["version"] == "0.1.0"
    found = _run(capsys, "tool", "search", "demo")
    assert any(t["id"] == "lab/t" for t in found["tools"])

    # guide via CLI
    assert "core" in [s["name"] for s in _run(capsys, "guide", "get")["sections"]]


def test_the_project_may_come_from_the_environment_instead_of_the_url(root_env, env, capsys,
                                                                     tmp_path):
    """The shape the plugin configures: HIVEMIND_SERVER_URL is the server, HIVEMIND_PROJECT the
    project — which the SessionStart hook exports from the session pin.

    Every surface the CLI has is exercised, because they need the project in different places: the
    tool calls carry it as an argument to `<root>/mcp`, and `artifact put/get` build
    `/p/<project>/blobs/…`, which does not exist off the root at all.
    """
    root, project = root_env
    os.environ["HIVEMIND_PROJECT"] = project

    assert _run(capsys, "health")["ok"] is True
    applied = _run(capsys, "schema", "apply", str(REPO / "packs/security-research/schema.json"))
    assert "finding" in " ".join(applied["created"]["node"])
    node = _run(capsys, "node", "upsert", "--type", "finding", "--props",
                '{"title":"root-url write","severity":"low"}')
    assert node["ok"] and node["created"]
    assert node["project"] == project, "the reply names the project the argument asked for"
    hits = _run(capsys, "search", "root-url")
    assert any("root-url write" in r["snippet"] for r in hits["results"])

    f = tmp_path / "blob.bin"; f.write_bytes(b"\x07" * 4096)
    art = _run(capsys, "artifact", "put", str(f))
    assert art["digest"].startswith("sha256:")
    back = _run(capsys, "artifact", "get", art["digest"], str(tmp_path / "back.bin"))
    assert back["size"] == 4096 and (tmp_path / "back.bin").read_bytes() == b"\x07" * 4096
    assert "core" in [s["name"] for s in _run(capsys, "guide", "get")["sections"]]


def test_the_project_flag_works_with_no_environment_at_all(root_env, env, capsys):
    """A plain terminal, where nothing exported HIVEMIND_PROJECT.

    `guide get` is the check because it is a project-scoped READ: on the root endpoint it is refused
    for naming no project exactly as a write is, so an answer here can only mean the flag was sent.
    """
    root, project = root_env
    assert "HIVEMIND_PROJECT" not in os.environ
    assert "core" in [s["name"] for s in
                      _run(capsys, "--project", project, "guide", "get")["sections"]]


def test_without_a_project_the_root_url_refuses_and_says_which_argument_is_missing(root_env, env,
                                                                                   capsys,
                                                                                   tmp_path):
    """The point of the root form: a call that names no project is refused, not defaulted.

    Both surfaces refuse, and for different reasons — the server refuses the tool call, the client
    refuses to build a REST path that does not exist — so both messages have to name the project as
    the missing thing rather than blaming the server.
    """
    with pytest.raises(SystemExit) as e:
        cli.main(["search", "anything"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "project" in err, err

    f = tmp_path / "x.bin"; f.write_bytes(b"x")
    with pytest.raises(SystemExit):
        cli.main(["artifact", "put", str(f)])
    err = capsys.readouterr().err
    assert "--project" in err and "HIVEMIND_PROJECT" in err, err


def test_health_answers_on_the_root_url_and_proves_nothing(root_env, env, capsys):
    """Kept working deliberately, and deliberately not trusted: /healthz sits above the project
    prefix and takes no token, so it says ok for a root URL with no project and for a bad token."""
    assert _run(capsys, "health")["ok"] is True
    assert _run(capsys, "--token", "hm_definitely-not-a-token", "health")["ok"] is True
