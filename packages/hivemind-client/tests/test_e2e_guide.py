"""Live guide: the seeded core section is fetchable over REST + via guide.sh (live then 304),
and the propose->merge firewall bumps guide_version."""
import json, os, socket, subprocess, threading, time
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
    env = dict(os.environ, HIVEMIND_SERVER_URL=base, HIVEMIND_TOKEN=tok,
               HIVEMIND_CACHE_DIR=str(tmp_path / "cache"))
    r1 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                        capture_output=True, text=True, env=env)
    assert r1.returncode == 0 and "live: guide 'core'" in r1.stdout
    assert "Writing to the graph" in r1.stdout
    r2 = subprocess.run(["bash", str(PLUGIN_GUIDE_SH), "--section", "core"],
                        capture_output=True, text=True, env=env)
    assert "unchanged" in r2.stdout            # ETag 304 hit


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
