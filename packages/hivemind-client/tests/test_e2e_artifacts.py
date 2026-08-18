"""End-to-end: boot the real server on a socket, drive it with the real hivemind Client.
Exercises MCP tool calls + REST artifact upload/attach/download+verify over HTTP (the
'two-client round-trip' path, minus the Tailscale hop)."""
import json
import os
import socket
import threading
import time

import pytest
import uvicorn

from hivemind import Client, HivemindError


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(data))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(data / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    from hivemind_server import app as appmod
    from hivemind_server.config import Config
    application = appmod.build_app(Config())
    proj = application.state.registry.all()[0]
    tok = next(iter(json.loads((proj.dir / "tokens.json").read_text())))
    port = _free_port()
    cfg = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/p/{proj.name}", tok
    srv.should_exit = True
    th.join(timeout=5)


def test_artifact_round_trip(server, tmp_path):
    base, tok = server
    c = Client(base, tok, agent="e2e")
    assert c.health()["ok"] is True

    # define a type and create a node
    c.call("schema_propose", {"kind": "node", "name": "artifact", "json_schema": {"type": "object"}})
    node = c.upsert("artifact", {"name": "kernelcache"})
    vid = node["version_id"]

    # make a 6 MB pseudo-binary and upload it (streaming, hashed)
    blob = tmp_path / "big.bin"
    blob.write_bytes(os.urandom(6 * 1024 * 1024))
    up = c.artifacts.put(str(blob), media_type="application/octet-stream")
    digest = up["digest"]
    assert up["size"] == 6 * 1024 * 1024

    # attach it to the node version, then resolve the ref
    c.call("artifact_attach", {"digest": digest, "version_id": vid, "role": "binary",
                               "filename": "big.bin"})
    ref = c.call("artifact_ref", {"digest": digest})
    assert ref["size"] == up["size"] and ref["resource_link"].endswith(
        digest.replace(":", "/", 1))

    # re-upload is deduplicated (HEAD short-circuits)
    up2 = c.artifacts.put(str(blob))
    assert up2.get("deduplicated") is True

    # download to a new path and verify bytes + digest
    out = tmp_path / "roundtrip.bin"
    got = c.artifacts.get(digest, str(out))
    assert got["digest"] == digest
    assert out.read_bytes() == blob.read_bytes()

    # tamper detection: asking for a wrong digest fails integrity
    with pytest.raises(HivemindError):
        c.artifacts.get("sha256:" + "0" * 64, str(tmp_path / "x"))
    c.close()
