"""Drive the `hivemind` CLI in-process against a live server: apply pack, upsert, search,
artifact put, tool search — asserting JSON output."""
import json, socket, threading, time
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
    monkeypatch.setenv("HIVEMIND_SERVER_URL", f"http://127.0.0.1:{port}/p/{proj.name}")
    monkeypatch.setenv("HIVEMIND_TOKEN", tok)
    yield tmp_path
    srv.should_exit = True; th.join(timeout=5)


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
