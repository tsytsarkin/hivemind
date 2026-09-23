"""Boot the real ASGI app in-process and exercise MCP + auth over HTTP via httpx ASGITransport."""
import asyncio
import json

import httpx
import pytest
from conftest import PROTO, Lifespan, _call, _parse, _post, _rpc

from hivemind_server import app as appmod
from hivemind_server.config import Config


@pytest.mark.anyio
async def test_health_open_and_auth_required(env):
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True
        # It used to answer with every project name, to anyone, with no token: liveness is all a
        # health probe is for, and a name list is an existence oracle.
        assert "projects" not in r.json()
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


EXPECTED_TOOLS = {
    "graph_search", "graph_get", "graph_subjects", "graph_types", "graph_neighbors", "graph_upsert",
    "graph_link", "graph_bulk_load", "schema_get", "schema_propose", "schema_changes",
    "schema_promote", "schema_apply", "guide_get", "guide_propose",
    "skill_search", "skill_get", "skill_publish", "skill_yank",
    "skill_catalog", "skill_link", "skill_suggest_links", "skill_unlink", "skill_autolink",
    "trap_search", "trap_get", "trap_record", "trap_status",
    "artifact_ref", "artifact_attach", "artifact_refs", "artifact_orphans",
    "tool_publish", "tool_resolve", "tool_search", "tool_yank",
    "tool_catalog", "tool_link", "tool_suggest_links", "tool_unlink", "tool_autolink",
    "project_list", "project_create", "project_info", "project_share", "project_unshare",
}


@pytest.mark.anyio
async def test_every_expected_tool_is_registered(env):
    """Guards against a tool block silently failing to register (a source edit that didn't
    apply still passes unit tests, because those call the modules directly)."""
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport, base_url="http://t",
                                                        timeout=30) as c:
        base = f"/p/{proj.name}"
        r = await _post(c, base, tok, "tools/list")
        assert r.status_code == 200, r.text
        names = {t["name"] for t in _parse(r)["result"]["tools"]}
        missing = EXPECTED_TOOLS - names
        assert not missing, f"tools missing from the MCP surface: {sorted(missing)}"


@pytest.mark.anyio
async def test_health_and_index_work_on_both_bases_without_a_token(env):
    """Clients hold the PROJECT base URL, so <base>/healthz must answer — it used to 404 and make
    a healthy server look dead. Data endpoints stay authenticated."""
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for path in ("/healthz", f"/p/{proj.name}/healthz"):
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert r.json()["ok"] is True
        for path in ("/", f"/p/{proj.name}/"):
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert "mcp" in r.text
        # data endpoints still require a token
        assert (await c.get(f"/p/{proj.name}/skills")).status_code == 401
        ok = await c.get(f"/p/{proj.name}/skills", headers={"Authorization": f"Bearer {tok}"})
        assert ok.status_code == 200


@pytest.mark.anyio
async def test_bus_websocket_route_is_reachable(env):
    """Route ORDER matters: Mount("/p/<name>") would otherwise swallow /p/<name>/bus/ws into the
    MCP app, which has no websocket handler, and every listener would be refused."""
    application, proj, tok = env
    ws_paths = []
    for r in application.routes:
        path = getattr(r, "path", "")
        if path.endswith("/bus/ws"):
            ws_paths.append((path, type(r).__name__))
    assert ws_paths, "no bus websocket route registered"
    path, kind = ws_paths[0]
    assert kind == "WebSocketRoute"
    # and it must come BEFORE the catch-all mount for the same prefix
    prefix = f"/p/{proj.name}"
    order = [getattr(r, "path", "") for r in application.routes]
    assert order.index(f"{prefix}/bus/ws") < order.index(prefix), \
        "the websocket route must be matched before the project Mount"


@pytest.mark.anyio
async def test_bus_connect_ws_url_uses_the_callers_host_not_the_bind_address(env):
    """The server binds 0.0.0.0 so both the LAN and the mesh reach it, which makes its configured
    public_url `http://0.0.0.0:8787`. Handing that to a listener produced a real
    ConnectionRefusedError against 0.0.0.0. The URL must come from the Host the call arrived on."""
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://box.local:8787",
                                                        timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", tok, "tools/call",
                        {"name": "bus_connect", "arguments": {"label": "from-elsewhere"}})
        assert r.status_code == 200, r.text
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
        assert "ws://box.local:8787/" in out["ws_url"], out["ws_url"]
        assert "0.0.0.0" not in out["monitor_command"], out["monitor_command"]


class OneTaskPerRequest:
    """Drive the app the way a real server does: every request in its own task.

    httpx.ASGITransport calls the app in the CALLER's task, so an identity the middleware set and
    one left over from an earlier request are indistinguishable, and a tool body that reads the
    right caller may only be reading the test's own context. uvicorn hands each request a fresh
    context copy; a test that claims identity is resolved PER REQUEST has to reproduce that.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        await asyncio.create_task(self.app(scope, receive, send))


def _identity_probe(monkeypatch):
    """Capture the identity a TOOL BODY sees, and return the list it accumulates into.

    graph.node_types is what the graph_types tool body calls, so patching it puts the probe exactly
    where a real tool would read the caller; reading the contextvar from the test's own context
    would prove nothing about what a tool sees.
    """
    from hivemind_server import graph
    from hivemind_server.identity import current_identity
    seen = []

    def probe(db, *, subject_key=None):        # same signature as the real graph.node_types
        seen.append(current_identity())
        return {"types": []}

    monkeypatch.setattr(graph, "node_types", probe)
    return seen


@pytest.mark.anyio
async def test_a_tool_body_sees_the_calling_user(env, monkeypatch):
    """The token is the authority. A tool must be able to read who is calling without being told."""
    application, proj, tok = env
    from hivemind_server.identity import IdentityStore
    server_tok = IdentityStore(application.state.cfg.identities_path).mint("nik", "mac-studio")
    seen = _identity_probe(monkeypatch)

    transport = httpx.ASGITransport(app=OneTaskPerRequest(application))
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", server_tok, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
    assert len(seen) == 1, "the tool body never ran"
    who = seen[0]
    assert who is not None, "the tool body could not see the caller"
    assert (who.user, who.device, who.legacy) == ("nik", "mac-studio", False)


@pytest.mark.anyio
async def test_a_tool_body_sees_a_legacy_token_as_legacy_and_project_scoped(env, monkeypatch):
    """The bootstrap token the fleet already holds is a legacy project token: it must keep working,
    and it must arrive marked legacy and pinned to the project whose file holds it."""
    application, proj, tok = env
    seen = _identity_probe(monkeypatch)

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", tok, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
    who = seen[0]
    assert (who.legacy, who.project_scope) == (True, proj.name)
    assert who.user == f"legacy:{proj.name}-bootstrap"


@pytest.mark.anyio
async def test_a_revoked_token_gets_the_generic_401_and_writes_nothing(env):
    """Review Focus 1: a token in neither store must be refused, with no side effect."""
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        base = f"/p/{proj.name}"
        before = await _post(c, base, tok, "tools/call",
                             {"name": "graph_types", "arguments": {}})
        assert before.status_code == 200
        # Define the type the refused write uses. A fresh project has no node types, so a write of
        # an undefined type would be refused by schema validation as well, and "nothing was
        # written" could not tell an auth refusal from a schema one.
        _call(await _post(c, base, tok, "tools/call", {"name": "schema_propose", "arguments": {
            "kind": "node", "name": "note", "json_schema": {"type": "object"},
            "agent": "test"}}, 2))
        write = {"name": "graph_upsert",
                 "arguments": {"type": "note", "props": {"text": "should not exist"},
                               "reason": "must be refused"}}
        r = await _post(c, base, "hm_revoked_never_existed", "tools/call", write, 3)
        assert r.status_code == 401
        assert "invalid or missing bearer token" in r.text
        # and nothing was written
        after = await _post(c, base, tok, "tools/call",
                            {"name": "graph_search", "arguments": {"query": "should not exist"}}, 4)
        assert json.loads(_parse(after)["result"]["content"][0]["text"])["results"] == []
        # Control: the identical write lands once the token is valid. Without this, the assertion
        # above would hold even with the auth gate deleted.
        assert _call(await _post(c, base, tok, "tools/call", write, 5))["ok"] is True
        found = await _post(c, base, tok, "tools/call",
                            {"name": "graph_search", "arguments": {"query": "should not exist"}}, 6)
        assert len(json.loads(_parse(found)["result"]["content"][0]["text"])["results"]) == 1


@pytest.mark.anyio
async def test_a_server_level_token_authenticates(env):
    application, proj, tok = env
    from hivemind_server.identity import IdentityStore
    store = IdentityStore(application.state.cfg.identities_path)
    server_tok = store.mint("nik", "mac-studio")

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", server_tok, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text


@pytest.mark.anyio
async def test_a_legacy_token_reaches_only_its_own_project(tmp_path, monkeypatch):
    """What kept projects apart used to be incidental: each project verified its own tokens.json.
    Resolving identity centrally must not widen that — a legacy token must still reach exactly the
    one project whose file holds it. The same token against its own project is the control, so a
    401 from the other project means "wrong project", not "bad token"."""
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "data" / "projects"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(root))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    application = appmod.build_app(Config())
    reg = application.state.registry
    assert {p.name for p in reg.all()} == {"alpha", "beta"}
    alpha_tok = next(iter(json.loads((reg.get("alpha").dir / "tokens.json").read_text())))

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        control = await _post(c, "/p/alpha", alpha_tok, "tools/call",
                              {"name": "graph_types", "arguments": {}})
        assert control.status_code == 200, control.text
        crossed = await _post(c, "/p/beta", alpha_tok, "tools/call",
                              {"name": "graph_types", "arguments": {}})
        assert crossed.status_code == 401, crossed.text


@pytest.mark.anyio
async def test_with_auth_off_a_tool_runs_and_sees_no_caller(tmp_path, monkeypatch):
    """HIVEMIND_REQUIRE_AUTH=0 is a supported local mode: there is no token, so there is nobody to
    resolve. The tool must still run, and must see None rather than some earlier caller.

    Deliberately NOT driven through OneTaskPerRequest: this test wants the polluted context that an
    in-process caller gives it, because that is what can expose a stale identity.
    """
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "data" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    monkeypatch.setenv("HIVEMIND_REQUIRE_AUTH", "0")
    application = appmod.build_app(Config())
    proj = application.state.registry.all()[0]
    seen = _identity_probe(monkeypatch)
    from hivemind_server.identity import Identity, set_identity
    set_identity(Identity(user="stale", device="earlier-request"))

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", "", "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
    assert seen == [None], seen


@pytest.mark.anyio
async def test_an_early_return_leaves_no_stale_identity_readable(env):
    """`set_identity(None)` is hoisted above every early return on purpose, and nothing pinned it
    there: no test failed if it slid back down to sit beside the resolve call.

    Two exits depend on the hoist — an unknown project, and any path outside /p/ — and both are
    reached before a caller is resolved. An in-process caller (httpx.ASGITransport, or an embedding)
    invokes the app in its OWN context, so an identity left from the request before it stays
    readable to whatever runs next unless the middleware clears it first.
    """
    application, proj, tok = env
    from hivemind_server.identity import Identity, current_identity, set_identity

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for path in ("/p/does.not.exist/skills", "/healthz", "/"):
            set_identity(Identity(user="stale", device="earlier-request"))
            r = await c.get(path, headers={"Authorization": f"Bearer {tok}"})
            assert current_identity() is None, f"{path} -> {r.status_code} left a caller readable"


HANDSHAKE_PROTO = "2025-11-25"      # a handshake-era version the SDK still routes the old way


@pytest.mark.anyio
async def test_a_handshake_era_request_resolves_identity_per_request(env, monkeypatch):
    """The transport has two entry paths, and identity must be per-request on both.

    A MODERN protocol version (what the other tests send) is handled inside the request's own task.
    A handshake-era version, which a client may still negotiate, goes instead through the session
    manager, where the MCP loop belongs to a SESSION — a task started once, when the session was
    created. The danger is that a tool body then reads whoever opened the session rather than
    whoever made the call: the wrong author, and later the wrong subject for an authorization
    decision. It does not, because the transport snapshots the sender's contextvars per message
    (mcp.shared._context_streams) and the dispatcher runs each handler in that snapshot — so the
    identity this middleware sets travels with the message, not with the session. This test is what
    holds that: alice opens the session, bob calls through it, and the tool must see bob.

    NOTE for whoever breaks this: reusing one session across two credentials is only possible
    because we never populate `scope["user"]`. The SDK's own session manager refuses a request whose
    `scope["user"]` differs from the AuthenticatedUser that created the session, answering 404
    "Session not found" (streamable_http_manager._handle_stateful_request). Wiring `scope["user"]`
    — e.g. by adopting the SDK's BearerAuthMiddleware — therefore breaks this test at its status
    assertion for a reason that has nothing to do with the contextvar claim above. If that happens,
    give alice and bob a session each and assert the tool saw the right caller in each, rather than
    concluding the per-message snapshot is gone.
    """
    application, proj, tok = env
    from hivemind_server.identity import IdentityStore
    store = IdentityStore(application.state.cfg.identities_path)
    tok_a, tok_b = store.mint("alice", "box-a"), store.mint("bob", "box-b")
    seen = _identity_probe(monkeypatch)

    def hdrs(token, session_id=None):
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": HANDSHAKE_PROTO}
        if session_id:
            h["Mcp-Session-Id"] = session_id
        return h

    # One task per request, or "the tool body saw bob" would just be the test's own context.
    transport = httpx.ASGITransport(app=OneTaskPerRequest(application))
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        base = f"/p/{proj.name}/mcp"
        init = await c.post(base, headers=hdrs(tok_a), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": HANDSHAKE_PROTO, "capabilities": {},
                       "clientInfo": {"name": "handshake-era", "version": "0"}}})
        assert init.status_code == 200, init.text
        # The whole test turns on the mount being STATEFUL — a session that outlives the request.
        # On a stateless mount there is no session id, bob's call would open its own conversation,
        # and the test would pass while proving nothing.
        session_id = init.headers.get("mcp-session-id")
        assert session_id, "no mcp-session-id: the mount is stateless, so nothing is being reused"
        # alice opened the conversation; bob makes the call, carrying the id she was handed
        r = await c.post(base, headers=hdrs(tok_b, session_id), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "graph_types", "arguments": {}}})
        assert r.status_code == 200, r.text
        assert r.headers.get("mcp-session-id") == session_id, \
            "bob's call was answered on a different session, so it did not reuse alice's"
        assert "error" not in _parse(r), r.text
    assert [w.user for w in seen] == ["bob"], seen


@pytest.mark.anyio
async def test_a_refused_bus_handshake_looks_exactly_like_a_project_that_does_not_exist(env):
    """The bus ws route answers without a bearer token, so its refusal is the one place an
    unauthenticated stranger could probe for /p/<private>/bus/ws. It must not become an oracle.

    Neither path may accept(): uvicorn collapses ANY pre-accept close into `HTTP/1.1 403 Forbidden`
    with `Content-Length: 0` and discards the close code — measured on this repo's pinned uvicorn
    against a real socket, byte-identical for both requests below — while an accepted-then-closed
    socket is plainly distinguishable from an unmounted path.
    """
    application, proj, _tok = env

    async def drive(path, query):
        sent = []
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
                 "query_string": query.encode(), "root_path": "",
                 "headers": [(b"host", b"testserver")], "client": ("1.2.3.4", 1234),
                 "server": ("testserver", 80), "subprotocols": [], "state": {}}

        async def receive():
            return {"type": "websocket.connect"}

        async def send(m):
            sent.append(m)

        await asyncio.wait_for(application(scope, receive, send), timeout=5)
        return sent

    refused = await drive(f"/p/{proj.name}/bus/ws", "key=hk1.a.b.c.d")
    missing = await drive("/p/no-such-project/bus/ws", "key=hk1.a.b.c.d")
    for got, what in ((refused, "a refused credential"), (missing, "an unknown project")):
        assert [m["type"] for m in got] == ["websocket.close"], f"{what} -> {got}"
    # The codes differ (4401 vs the router's own close) and that is fine: uvicorn drops the code of
    # a pre-accept close. What must never differ is whether the socket was accepted.
    assert all(m["type"] != "websocket.accept" for m in refused + missing)
