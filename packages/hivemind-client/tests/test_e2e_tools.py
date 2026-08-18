"""End-to-end tool registry over a live server: publish a PEP 723 tool, then fetch it into a
fresh directory (simulating another machine), verifying integrity + generated RUN.md + run cmd."""
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hivemind import Client


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
    yield f"http://127.0.0.1:{port}/p/{proj.name}", tok
    srv.should_exit = True; th.join(timeout=5)


PEP723_TOOL = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["capstone>=5", "rich"]
# ///
import sys
print("disassembled", sys.argv[1:])
'''


def test_publish_then_fetch_on_another_machine(server, tmp_path):
    base, tok = server
    author = Client(base, tok, agent="author")

    tool_src = tmp_path / "disasm.py"
    tool_src.write_text(PEP723_TOOL)
    pub = author.tool_publish(str(tool_src), id="lab/disasm", version="1.0.0",
                              description="disassemble a blob",
                              examples=[{"cmd": "disasm.py a.bin", "expect": "prints insns"}])
    assert pub["version"] == "1.0.0"

    # a second, newer version
    author.tool_publish(str(tool_src), id="lab/disasm", version="1.1.0",
                        description="disassemble a blob (faster)",
                        examples=[{"cmd": "disasm.py a.bin", "expect": "prints insns"}])

    # "another machine": fresh client + fresh dest dir
    consumer = Client(base, tok, agent="consumer")
    found = consumer.tool_search("disassemble")
    assert any(t["id"] == "lab/disasm" for t in found["tools"])

    dest = tmp_path / "downloaded"
    got = consumer.tool_get("lab/disasm", constraint="^1.0", dest_dir=str(dest))
    assert got["version"] == "1.1.0"
    assert got["run"] == "uv run --script disasm.py"

    # the fetched artifact is byte-identical (integrity verified during download)
    fetched = Path(got["entrypoint"])
    assert fetched.read_text() == PEP723_TOOL
    # RUN.md carries the run command, deps from the PEP 723 block, and the example
    runmd = (Path(got["dir"]) / "RUN.md").read_text()
    assert "uv run --script disasm.py" in runmd
    assert "capstone>=5" in runmd and "prints insns" in runmd
    # a pin lockfile was written
    assert (dest / "hivemind-tools.lock").read_text().startswith("lab/disasm@1.1.0 sha256:")
    author.close(); consumer.close()
