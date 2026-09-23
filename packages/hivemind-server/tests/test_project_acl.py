"""The ACL must cover every /p/<name>/ path, and must not leak a private project's existence."""
import json
import logging

import httpx
import pytest
from conftest import Lifespan, _parse, _post

from hivemind_server import app as appmod
from hivemind_server import projects_meta as pm
from hivemind_server.config import Config
from hivemind_server.identity import Identity, IdentityStore, current_identity, set_identity


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture()
def two_users(projects_dir):
    """A server holding a shared `default` and a private `nik.private`, plus nik's and ana's tokens.

    Both projects are laid down on disk BEFORE build_app, because a project gets its ASGI mount at
    build time: one created through the registry afterwards has no route at all, so even its owner
    would get Starlette's bare 404 and the owner test below would pass for the wrong reason.
    """
    (projects_dir / "default").mkdir(parents=True)
    private = projects_dir / "nik.private"
    private.mkdir(parents=True)
    pm.save(private, pm.ProjectMeta(name="nik.private", visibility="private", owner="nik"))

    application = appmod.build_app(Config())
    store = IdentityStore(application.state.cfg.identities_path)
    return application, store.mint("nik", "mac-studio"), store.mint("ana", "laptop")


@pytest.mark.anyio
async def test_owner_reaches_their_private_project(two_users):
    application, nik, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.private", nik, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
        assert "error" not in _parse(r), r.text


@pytest.mark.anyio
async def test_a_private_project_is_indistinguishable_from_a_missing_one(two_users):
    """Divergent errors are an existence oracle: ana must not learn nik.private exists.

    healthz is in the comparison on purpose. It is the one path that answers without a token, so
    if the carve-out were unconditional a 200 there would confirm the project exists to a caller
    with no credential at all — a cheaper oracle than anything the tool layer could leak.
    """
    application, _, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        forbidden_mcp = await _post(c, "/p/nik.private", ana, "tools/call",
                                    {"name": "graph_types", "arguments": {}})
        missing_mcp = await _post(c, "/p/does.not.exist", ana, "tools/call",
                                  {"name": "graph_types", "arguments": {}})
        forbidden_blob = await c.get("/p/nik.private/blobs/sha256/" + "0" * 64,
                                     headers=_auth(ana))
        missing_blob = await c.get("/p/does.not.exist/blobs/sha256/" + "0" * 64,
                                   headers=_auth(ana))
        forbidden_index = await c.get("/p/nik.private/")
        missing_index = await c.get("/p/does.not.exist/")
        forbidden_health = await c.get("/p/nik.private/healthz")
        missing_health = await c.get("/p/does.not.exist/healthz")

    # Volatile only: a real server stamps these, an in-process transport does not, and neither
    # says anything about the project.
    volatile = {"date", "server"}
    for what, forbidden, missing in [("mcp", forbidden_mcp, missing_mcp),
                                     ("blob", forbidden_blob, missing_blob),
                                     ("index", forbidden_index, missing_index),
                                     ("healthz", forbidden_health, missing_health)]:
        assert forbidden.status_code == missing.status_code, what
        assert forbidden.text == missing.text, what
        assert forbidden.status_code == 404, what
        assert "nik.private" not in forbidden.text, what
        # Headers as well as status and body. Both answers come from the same _json call today, so
        # this holds for free — but a `www-authenticate` added to the forbidden branch alone would
        # be the same existence oracle wearing a different field, and nothing else would fail.
        assert ({k: v for k, v in forbidden.headers.items() if k.lower() not in volatile}
                == {k: v for k, v in missing.headers.items() if k.lower() not in volatile}), \
            f"{what}: {dict(forbidden.headers)} != {dict(missing.headers)}"


@pytest.mark.anyio
async def test_the_blob_surface_is_not_a_bypass(two_users):
    """The REST routes never reach the tool layer; this is the hole an ACL there would miss.

    Asserted on the body as well as the status, because a missing blob is a 404 too: a status-only
    assertion would still pass with the ACL deleted. The controls at the end are the same requests
    against a project ana may use, which must answer something else — and must prove the path
    reaches a handler rather than 404ing at the router.
    """
    application, _, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        # Every REST route the project mount registers, so this is an inventory and not a sample;
        # the two that answer without a token (healthz and the index) are in the test above.
        for method, path in [("GET", "/p/nik.private/blobs/sha256/" + "0" * 64),
                             ("PUT", "/p/nik.private/blobs/sha256/" + "0" * 64),
                             ("POST", "/p/nik.private/blobs/batch"),
                             ("GET", "/p/nik.private/guide"),
                             ("GET", "/p/nik.private/guide/core"),
                             ("GET", "/p/nik.private/skills"),
                             ("GET", "/p/nik.private/skills/some.skill"),
                             ("GET", "/p/nik.private/tools"),
                             ("GET", "/p/nik.private/tools/some.tool")]:
            r = await c.request(method, path, headers=_auth(ana))
            assert r.status_code == 404, f"{method} {path} -> {r.status_code}"
            assert r.json() == appmod.PROJECT_DENIED, f"{method} {path} -> {r.text}"
        # Controls on a project ana MAY use, to prove those paths reach the blob handler at all:
        # the routes are /blobs/{algo}/{hex}, TWO segments, so `sha256:<hex>` (one segment, as this
        # test first had it) matches no route and 404s at the router — the assertions above would
        # then hold with the ACL deleted. A router miss is a 9-byte text/plain "Not Found"; the
        # handler's own answers are an EMPTY-bodied 404 and a JSON 400 digest-mismatch.
        got = await c.get("/p/default/blobs/sha256/" + "0" * 64, headers=_auth(ana))
        assert (got.status_code, got.text) == (404, ""), \
            f"GET control -> {got.status_code} {got.text!r}"
        put = await c.request("PUT", "/p/default/blobs/sha256/" + "0" * 64, headers=_auth(ana))
        assert put.status_code == 400 and "digest mismatch" in put.text, put.text


@pytest.mark.anyio
async def test_healthz_stays_open_because_it_names_nothing(two_users):
    application, _, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/p/default/healthz")).status_code == 200


@pytest.mark.anyio
async def test_the_root_index_no_longer_enumerates_projects(two_users):
    """It listed every project name with no token at all."""
    application, _, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await c.get("/")
        assert r.status_code == 200
        body = r.text
        assert "nik.private" not in body
        assert "projects" not in json.loads(body)


@pytest.mark.anyio
async def test_no_unauthenticated_root_route_enumerates_project_names(two_users):
    """Fixing only `GET /` would be theatre: /healthz and /projects handed out the same list."""
    application, _, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for path in ("/", "/healthz", "/projects"):
            r = await c.get(path)
            assert "nik.private" not in r.text, f"{path} leaks the name: {r.text}"


@pytest.mark.anyio
async def test_the_projects_listing_shows_only_what_the_caller_can_reach(two_users):
    application, nik, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        anon = await c.get("/projects")
        assert anon.status_code == 401, anon.text
        mine = await c.get("/projects", headers=_auth(nik))
        theirs = await c.get("/projects", headers=_auth(ana))
    assert mine.json()["projects"] == ["default", "nik.private"], mine.text
    assert theirs.json()["projects"] == ["default"], theirs.text


@pytest.mark.anyio
async def test_unreadable_metadata_denies_identically_and_says_so_in_the_log(two_users, caplog):
    """An operator who typos project.json gets a generic 404 like everyone else — telling them
    apart in the RESPONSE would be the oracle. The log is where the two are distinguished."""
    application, nik, _ = two_users
    private = application.state.registry.get("nik.private").dir
    (private / "project.json").write_text("{ this is not json")

    transport = httpx.ASGITransport(app=application)
    with caplog.at_level(logging.WARNING):
        async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                            base_url="http://t", timeout=30) as c:
            owner = await c.get("/p/nik.private/skills", headers=_auth(nik))
            missing = await c.get("/p/does.not.exist/skills", headers=_auth(nik))
    assert owner.status_code == 404 and owner.text == missing.text, owner.text
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "nik.private" in logged and "project.json" in logged, logged


@pytest.mark.anyio
async def test_a_shared_project_stays_reachable_by_any_user(two_users):
    """The control for every denial above: the ACL must not have closed the ordinary case."""
    application, _, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/default", ana, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
        assert (await c.get("/p/default/skills", headers=_auth(ana))).status_code == 200


@pytest.mark.anyio
async def test_a_denied_request_leaves_no_identity_behind(two_users):
    """Every refusal path is above `set_identity`, so a denied caller must not be readable — and
    the stale identity from the request before it must not be either."""
    application, _, ana = two_users
    set_identity(Identity(user="stale", device="earlier-request"))
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await c.get("/p/nik.private/skills", headers=_auth(ana))
    assert r.status_code == 404
    assert current_identity() is None
