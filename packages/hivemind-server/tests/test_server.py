"""Boot the real ASGI app in-process and exercise MCP + auth over HTTP via httpx ASGITransport."""
import json
import httpx
import pytest

from hivemind_server.config import Config
from hivemind_server import app as appmod

import asyncio


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
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "data" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
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


@pytest.mark.anyio
async def test_health_open_and_auth_required(env):
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert "default" in r.json()["projects"]
        # MCP without a token -> 401
        r = await c.post(f"/p/{proj.name}/mcp", json=_rpc("tools/list"),
                         headers={"Accept": "application/json, text/event-stream",
                                  "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/list"})
        assert r.status_code == 401


@pytest.mark.anyio
async def test_tools_list_and_call(env):
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        # list tools
        base = f"/p/{proj.name}"
        r = await _post(c, base, tok, "tools/list")
        assert r.status_code == 200, r.text
        body = _parse(r)
        names = {t["name"] for t in body["result"]["tools"]}
        assert {"graph_upsert", "graph_get", "schema_propose", "guide_get"} <= names

        # define a node type, then upsert + read back via tool calls
        _call(await _post(c, base, tok, "tools/call", {"name": "schema_propose", "arguments": {
            "kind": "node", "name": "note", "json_schema": {"type": "object"}, "agent": "test"}}, 2))
        up = _call(await _post(c, base, tok, "tools/call", {"name": "graph_upsert", "arguments": {
            "type": "note", "props": {"text": "hello mesh"}, "agent": "test"}}, 3))
        assert up["ok"] is True
        nid = up["node_id"]
        got = _call(await _post(c, base, tok, "tools/call", {"name": "graph_get",
            "arguments": {"node_id": nid}}, 4))
        assert got["current"]["props"]["text"] == "hello mesh"


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
