"""Shared fixtures and the in-process ASGI/MCP test kit.

The HTTP helpers live here rather than in test_server.py because pytest fixtures do not cross
modules: every test module that boots the real app needs `env`, so a helper kit kept in one test
module made the second one import from the first (or fail at collection).
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest
from hivemind_server import schemas
from hivemind_server.config import Config
from hivemind_server.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "hm.db")
    # register a couple of node/edge types (as a domain pack would)
    with d.write("test-setup", "seed types") as tx:
        cur = tx.cur
        obj = {"type": "object", "additionalProperties": True}
        schemas.define_type(cur, tx, "node", "component", obj, status="active")
        schemas.define_type(cur, tx, "node", "finding", obj, status="active")
        schemas.define_type(cur, tx, "node", "function", obj, status="active")
        schemas.define_type(cur, tx, "edge", "refines", obj, status="active",
                            traits={"versioned": True})
        schemas.define_type(cur, tx, "edge", "contradicts", obj, status="active",
                            traits={"versioned": True, "assertive": True, "symmetric": True})
        schemas.define_type(cur, tx, "edge", "calls", obj, status="active",
                            traits={"versioned": False})  # bulk
        schemas.define_type(cur, tx, "edge", "depends_on", obj, status="active",
                            traits={"versioned": True, "acyclic": True})
    return d


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Lifespan:
    """Minimal ASGI lifespan driver (ASGITransport does not run lifespan events)."""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        self._q = asyncio.Queue()
        self._started = asyncio.Event()
        self._done = asyncio.Event()
        await self._q.put({"type": "lifespan.startup"})
        self._task = asyncio.create_task(
            self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, self._q.get, self._send))
        await self._started.wait()
        return self

    async def _send(self, msg):
        t = msg["type"]
        if t.endswith("startup.complete") or t.endswith("startup.failed"):
            self._started.set()
        elif t.endswith("shutdown.complete") or t.endswith("shutdown.failed"):
            self._done.set()

    async def __aexit__(self, *exc):
        await self._q.put({"type": "lifespan.shutdown"})
        await self._done.wait()
        await self._task


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    """Point the server at a fresh data dir and hand back its projects root, UNBUILT.

    Split out of `env` because build_app mounts one ASGI app per project it discovers: a test that
    needs an extra project has to lay it down on disk before the app is built, or the project has
    no route at all.
    """
    root = tmp_path / "data" / "projects"
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(root))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    return root


@pytest.fixture()
def env(projects_dir):
    from hivemind_server import app as appmod
    cfg = Config()
    application = appmod.build_app(cfg)
    reg = application.state.registry
    proj = reg.all()[0]
    # grab the bootstrap token
    token = json.loads((proj.dir / "tokens.json").read_text())
    tok = next(iter(token))
    return application, proj, tok


PROTO = "2026-07-28"


def _rpc(method, params=None, _id=1):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": PROTO,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {"jsonrpc": "2.0", "id": _id, "method": method, "params": params}


def _headers(tok, method, name=None):
    h = {"Authorization": f"Bearer {tok}",
         "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": PROTO,
         "Mcp-Method": method}
    if name:
        h["Mcp-Name"] = name
    return h


async def _post(c, base, tok, method, params=None, _id=1):
    name = (params or {}).get("name") if method == "tools/call" else None
    return await c.post(f"{base}/mcp", json=_rpc(method, params, _id),
                        headers=_headers(tok, method, name))


def _parse(r):
    """Streamable HTTP may answer as application/json or as a single SSE event."""
    ct = r.headers.get("content-type", "")
    if ct.startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError("no SSE data line")
    return r.json()


def _call(r):
    assert r.status_code == 200, r.text
    body = _parse(r)
    assert "result" in body, body
    sc = body["result"].get("structuredContent")
    if sc is not None:
        return sc
    # fall back to the text content block
    return json.loads(body["result"]["content"][0]["text"])
