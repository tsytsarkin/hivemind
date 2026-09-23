"""Per-call project resolution: the reason this design was chosen over server-side session state."""
import asyncio
import json

import httpx
import pytest
from conftest import Lifespan, _parse, _post

from hivemind_server import app as appmod
from hivemind_server import projects_meta as pm
from hivemind_server import schemas
from hivemind_server.config import Config
from hivemind_server.identity import IdentityStore


@pytest.fixture()
def two_projects(projects_dir):
    """A server holding two private projects of nik's and a shared `default`, plus nik's token.

    All are laid down on disk BEFORE build_app, like test_project_acl.two_users: a project gets
    its ASGI mount at build time, so one created through the registry afterwards has no
    /p/<name> route at all and the mount-default test below would 404 for the wrong reason.

    `default` exists so that "the caller has an accessible project called default" is true — that
    is the state in which a fallback to the configured default project would go unnoticed.
    """
    (projects_dir / "default").mkdir(parents=True)
    for name in ("nik.a", "nik.b"):
        d = projects_dir / name
        d.mkdir(parents=True)
        pm.save(d, pm.ProjectMeta(name=name, visibility="private", owner="nik"))
    application = appmod.build_app(Config())
    nik = IdentityStore(application.state.cfg.identities_path).mint("nik", "mac-studio")
    # `default` is seeded too, so a write that landed there by mistake would SUCCEED rather than
    # bounce off a missing node type: the silent-wrong-project bug has to be reproducible for the
    # tests below to be evidence against it.
    for name in ("default", "nik.a", "nik.b"):
        p = application.state.registry.get(name)
        with p.db.write("setup", "seed types") as tx:
            schemas.define_type(tx.cur, tx, "node", "note",
                                {"type": "object", "additionalProperties": True}, status="active")
    return application, nik


@pytest.mark.anyio
async def test_the_project_argument_appears_in_every_tool_schema(env):
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", tok, "tools/list")
        tools = _parse(r)["result"]["tools"]
    assert len(tools) > 40, f"only {len(tools)} tools listed — the surface is bigger than that"
    missing = [t["name"] for t in tools
               if "project" not in (t["inputSchema"].get("properties") or {})]
    assert not missing, f"tools with no project parameter: {missing}"
    # ...and it is never mandatory in the schema; the requirement is enforced at call time so the
    # error can name the projects the caller may actually use.
    assert not [t["name"] for t in tools if "project" in (t["inputSchema"].get("required") or [])]


@pytest.mark.anyio
async def test_a_write_without_a_project_is_refused(two_projects):
    """Fail closed: a forgotten argument used to mean a silent write into the default project."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", nik, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "no project"},
                                       "reason": "should be refused"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False
    assert "project" in out["error"]
    assert "nik.a" in out["error"], "the error must name the projects the caller can use"


@pytest.mark.anyio
async def test_the_neutral_endpoint_has_no_default_project_even_for_a_read(two_projects):
    """The ONE fallback is the project the caller's own URL named. A configured default would be
    the silent-wrong-project bug wearing a different hat: `default` is accessible to this caller,
    so a fallback to it would answer 200 and nothing would look wrong."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", nik, "tools/call", {"name": "graph_types", "arguments": {}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False and "project" in out["error"], out


@pytest.mark.anyio
async def test_nothing_was_written_by_the_refused_write(two_projects):
    """The refusal has to be a refusal, not a warning attached to a write that still happened."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        await _post(c, "", nik, "tools/call",
                    {"name": "graph_upsert",
                     "arguments": {"type": "note", "props": {"title": "no project"},
                                   "reason": "should be refused"}})
    for name in ("default", "nik.a", "nik.b"):
        db = application.state.registry.get(name).db
        with db.read() as cur:
            assert cur.execute("SELECT COUNT(*) c FROM node").fetchone()["c"] == 0, name


@pytest.mark.anyio
async def test_an_earlier_request_does_not_leave_a_default_behind(two_projects):
    """The mount default is per request. An in-process caller drives the app in its own task, so a
    request that did not clear it would hand the next one the previous project — and that next one
    is a write on the neutral endpoint, which must still be refused."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        await _post(c, "/p/nik.a", nik, "tools/call", {"name": "graph_types", "arguments": {}})
        r = await _post(c, "", nik, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "leaked?"},
                                       "reason": "should be refused"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False, out
    with application.state.registry.get("nik.a").db.read() as cur:
        assert cur.execute("SELECT COUNT(*) c FROM node").fetchone()["c"] == 0


@pytest.mark.anyio
async def test_with_auth_off_the_neutral_endpoint_still_works(projects_dir, monkeypatch):
    """HIVEMIND_REQUIRE_AUTH=0 is a supported local mode: there is no identity, so there is no ACL
    to apply — but resolution must still happen, or the mode is broken on the neutral endpoint."""
    (projects_dir / "solo").mkdir(parents=True)
    monkeypatch.setenv("HIVEMIND_REQUIRE_AUTH", "0")
    application = appmod.build_app(Config())
    p = application.state.registry.get("solo")
    with p.db.write("setup", "seed types") as tx:
        schemas.define_type(tx.cur, tx, "node", "note",
                            {"type": "object", "additionalProperties": True}, status="active")
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", "", "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "no auth"},
                                       "project": "solo", "reason": "auth off"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True, out
    assert out["project"] == "solo"


@pytest.mark.anyio
async def test_a_read_without_a_project_uses_the_mount_default(two_projects):
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.a", nik, "tools/call", {"name": "graph_types", "arguments": {}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True
    assert out["project"] == "nik.a", "every response echoes the resolved project"


@pytest.mark.anyio
async def test_a_write_on_a_project_mount_needs_no_argument(two_projects):
    """The URL naming the project IS explicit. Only the neutral endpoint has nothing to fall back
    on, and that is the case the refusal above covers."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.b", nik, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "from the mount"},
                                       "reason": "mount default"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True, out
    assert out["project"] == "nik.b"


@pytest.mark.anyio
async def test_an_explicit_project_beats_the_mount_it_arrived_on(two_projects):
    """A write carrying project= must land where the ARGUMENT says, not where the URL says."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.a", nik, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "argument wins"},
                                       "project": "nik.b", "reason": "precedence"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True and out["project"] == "nik.b", out
    with application.state.registry.get("nik.a").db.read() as cur:
        assert cur.execute("SELECT COUNT(*) c FROM node").fetchone()["c"] == 0


@pytest.mark.anyio
async def test_concurrent_calls_on_one_token_land_in_different_databases(two_projects):
    """The test that would have failed under a server-side 'current project'. One token, two
    parallel agents, two projects — neither may see the other's write."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        async def write(project, title):
            return await _post(c, "", nik, "tools/call",
                               {"name": "graph_upsert",
                                "arguments": {"type": "note", "props": {"title": title},
                                              "project": project, "reason": "concurrency"}})
        done = await asyncio.gather(*[write("nik.a", "only-in-a") for _ in range(5)],
                                    *[write("nik.b", "only-in-b") for _ in range(5)])
        for r in done:
            body = json.loads(_parse(r)["result"]["content"][0]["text"])
            assert body["ok"] is True, body

        async def titles(project):
            r = await _post(c, "", nik, "tools/call",
                            {"name": "graph_search",
                             "arguments": {"query": "only-in", "project": project}})
            body = json.loads(_parse(r)["result"]["content"][0]["text"])
            # graph_search returns a `snippet` (the props as JSON), not the props themselves.
            return {json.loads(hit["snippet"])["title"] for hit in body["results"]}

        assert await titles("nik.a") == {"only-in-a"}
        assert await titles("nik.b") == {"only-in-b"}


@pytest.mark.anyio
async def test_an_inaccessible_project_argument_is_refused(two_projects):
    application, nik = two_projects
    ana = IdentityStore(application.state.cfg.identities_path).mint("ana", "laptop")
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", ana, "tools/call",
                        {"name": "graph_types", "arguments": {"project": "nik.a"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False
    assert "nik.a" not in out["error"], "the error must not confirm that nik.a exists"


@pytest.mark.anyio
async def test_a_write_to_an_inaccessible_project_is_refused(two_projects):
    """The read path above is the cheap half. This is the one that matters: a caller naming someone
    else's project on a WRITE must be refused, without the error confirming it exists, and nothing
    may be written."""
    application, nik = two_projects
    ana = IdentityStore(application.state.cfg.identities_path).mint("ana", "laptop")
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", ana, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "not hers"},
                                       "project": "nik.a", "reason": "should be refused"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False
    assert "nik.a" not in out["error"], "the error must not confirm that nik.a exists"
    with application.state.registry.get("nik.a").db.read() as cur:
        assert cur.execute("SELECT COUNT(*) c FROM node").fetchone()["c"] == 0


def test_with_auth_on_a_call_with_no_identity_is_refused(two_projects):
    """The no-ACL branch must key off auth being OFF, never off an absent identity.

    Today the middleware makes an identity-less tool call unreachable — but only through a tail
    allowlist in another file, and one more unauthenticated tail there would have turned an inferred
    "nobody, so no ACL" into a full bypass with nothing here failing. Called directly, because the
    middleware is exactly what this must not depend on.
    """
    from hivemind_server import envelope
    from hivemind_server.db import Invalid
    from hivemind_server.identity import set_identity

    application, _ = two_projects
    registry = application.state.registry
    set_identity(None)
    envelope.set_mount_default("nik.a")          # as the middleware would, after its own ACL
    envelope.set_registry(registry, require_auth=True)
    with pytest.raises(Invalid):
        envelope.resolve_project(None, requires=False)
    with pytest.raises(Invalid):
        envelope.resolve_project("nik.a", requires=True)
    # Control: the same two calls in the mode that legitimately has nobody to authorize.
    envelope.set_registry(registry, require_auth=False)
    assert envelope.resolve_project(None, requires=False).name == "nik.a"
    assert envelope.resolve_project("nik.b", requires=True).name == "nik.b"
    envelope.set_mount_default(None)


@pytest.mark.anyio
async def test_a_missing_project_argument_is_refused_identically(two_projects):
    """Unknown and forbidden must read the same, or the pair is an existence oracle — the same
    property app.PROJECT_DENIED gives the middleware."""
    application, nik = two_projects
    ana = IdentityStore(application.state.cfg.identities_path).mint("ana", "laptop")
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        async def err(project):
            r = await _post(c, "", ana, "tools/call",
                            {"name": "graph_types", "arguments": {"project": project}})
            return json.loads(_parse(r)["result"]["content"][0]["text"])
        assert await err("nik.a") == await err("does.not.exist")


@pytest.mark.anyio
async def test_two_apps_in_one_process_do_not_share_a_registry(tmp_path, monkeypatch):
    """Nothing enforces one app per process. Held process-wide, the registry would be whichever app
    was built LAST, and a call on app A would resolve its project name against app B — writing into
    another deployment's data dir. Both apps hold a shared project of the same name, so the only
    thing that distinguishes them is which registry answered."""
    def build(tag):
        root = tmp_path / tag / "projects"
        (root / "shared").mkdir(parents=True)
        monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / tag))
        monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(root))
        monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
        application = appmod.build_app(Config())
        with application.state.registry.get("shared").db.write("setup", "seed types") as tx:
            schemas.define_type(tx.cur, tx, "node", "note",
                                {"type": "object", "additionalProperties": True}, status="active")
        return application, IdentityStore(application.state.cfg.identities_path).mint("nik", tag)

    app_a, tok_a = build("a")
    app_b, _ = build("b")            # built last: a process-wide registry would point here
    transport = httpx.ASGITransport(app=app_a)
    async with Lifespan(app_a), httpx.AsyncClient(transport=transport,
                                                  base_url="http://t", timeout=30) as c:
        r = await _post(c, "", tok_a, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "app A only"},
                                       "project": "shared", "reason": "two apps"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True, out

    def nodes(application):
        with application.state.registry.get("shared").db.read() as cur:
            return cur.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]
    assert (nodes(app_a), nodes(app_b)) == (1, 0)


@pytest.mark.anyio
async def test_root_rest_routes_are_not_an_unguarded_back_door(two_projects):
    """The MCP app carries custom REST routes; mounting it at the root must not expose them
    without a project."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for path in ("/blobs/sha256/" + "0" * 64, "/guide/core", "/skills", "/tools",
                     "/blobs/batch"):
            r = await c.get(path, headers={"Authorization": f"Bearer {nik}"})
            assert r.status_code in (400, 404), f"{path} -> {r.status_code}"
            assert "only-in" not in r.text and "note" not in r.text, f"{path} -> {r.text[:200]}"
        assert (await c.get("/blobs/sha256/" + "0" * 64)).status_code in (400, 401, 404)


@pytest.mark.anyio
async def test_the_rest_routes_answer_for_the_project_in_their_url(two_projects):
    """The REST handlers never pass through a tool wrapper, so their project comes from the mount.
    One app now serves every prefix: a name bound at attach time would answer for one project
    everywhere, and both of these would say the same thing."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    auth = {"Authorization": f"Bearer {nik}"}
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for name in ("nik.a", "nik.b"):
            health = await c.get(f"/p/{name}/healthz", headers=auth)
            assert health.json() == {"ok": True, "project": name}, health.text
            batch = await c.post(f"/p/{name}/blobs/batch", headers=auth,
                                 json={"objects": [{"oid": "sha256:" + "0" * 64, "size": 1}]})
            href = batch.json()["objects"][0]["actions"]["upload"]["href"]
            assert href.startswith(f"/p/{name}/blobs/"), href


@pytest.mark.anyio
async def test_the_neutral_endpoint_still_demands_a_token(two_projects):
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", "not-a-token", "tools/call",
                        {"name": "graph_types", "arguments": {"project": "nik.a"}})
    assert r.status_code == 401, r.text
