"""Console login cannot widen a server token's project ACL or agent mailbox."""

import httpx
import pytest

from hivemind_server import projects_meta
from hivemind_server.config import Config
from hivemind_server.identity import IdentityStore


@pytest.mark.anyio
async def test_token_login_revoke_logout_and_project_access(env):
    from hivemind_server.ui_app import build_ui_app
    mcp, project, legacy = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    ui = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://testserver") as client:
        assert (await client.get("/api/projects")).status_code == 401
        assert (await client.post("/api/login", json={"token": legacy})).status_code == 401
        login = await client.post("/api/login", json={"token": token})
        assert login.status_code == 200 and "httponly" in login.headers["set-cookie"].lower()
        assert token not in login.text and token not in login.headers["set-cookie"]
        csrf = login.json()["csrf_token"]
        assert project.name in (await client.get("/api/projects")).json()["projects"]
        assert (await client.post("/api/logout")).status_code == 403
        assert (await client.post("/api/logout", headers={"x-csrf-token": csrf,
                                                         "origin": "https://elsewhere.test"})).status_code == 403
        assert (await client.post("/api/logout", headers={"x-csrf-token": csrf})).status_code == 200
        assert (await client.get("/api/projects")).status_code == 401
        await client.post("/api/login", json={"token": token})
        ids._tokens.pop(token)
        ids.save()
        assert (await client.get("/api/projects")).status_code == 401


@pytest.mark.anyio
async def test_ui_private_project_name_has_one_denial_for_missing_and_inaccessible(env):
    from hivemind_server.ui_app import build_ui_app
    mcp, project, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    nik, ana = ids.mint("nik", "mac"), ids.mint("ana", "laptop")
    meta = project.meta
    meta.visibility, meta.owner = "private", "nik"
    projects_meta.save(project.dir, meta)
    ui = build_ui_app(Config(), mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://testserver") as c:
        await c.post("/api/login", json={"token": ana})
        forbidden = await c.get(f"/api/projects/{project.name}/overview")
        absent = await c.get("/api/projects/unknown.project/overview")
        assert forbidden.status_code == absent.status_code == 404
        assert forbidden.text == absent.text
        await c.post("/api/login", json={"token": nik})
        assert (await c.get(f"/api/projects/{project.name}/overview")).status_code == 200


@pytest.mark.anyio
async def test_ui_session_survives_listener_restart_until_underlying_token_revoked(env):
    from hivemind_server.ui_app import build_ui_app
    mcp, _, _ = env
    ids = IdentityStore(mcp.state.cfg.identities_path)
    token = ids.mint("nik", "mac")
    first = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first),
                                 base_url="http://testserver") as c:
        login = await c.post("/api/login", json={"token": token})
        cookie = login.cookies.get("hm_ui_session")
        assert cookie
    restarted = build_ui_app(mcp.state.cfg, mcp.state.registry, ids)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),
                                 base_url="http://testserver",
                                 cookies={"hm_ui_session": cookie}) as c:
        assert (await c.get("/api/session")).status_code == 200
        ids._tokens.pop(token)
        ids.save()
        assert (await c.get("/api/session")).status_code == 401


@pytest.mark.anyio
async def test_unauthenticated_login_refuses_oversized_body_before_json_parse(env):
    from hivemind_server.ui_app import build_ui_app
    mcp, _, _ = env
    ui = build_ui_app(mcp.state.cfg, mcp.state.registry, mcp.state.identities)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://testserver") as c:
        response = await c.post("/api/login", content=b"{" + b" " * 100_000,
                                headers={"content-type": "application/json"})
    assert response.status_code == 413
