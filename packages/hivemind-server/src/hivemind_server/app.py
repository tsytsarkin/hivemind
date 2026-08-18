"""ASGI entrypoint. One Starlette app hosts every project under /p/<name>/… (MCP at
/p/<name>/mcp, REST blob routes under the same prefix). A single auth middleware gates all
project traffic with that project's bearer tokens; /healthz stays open.
"""
from __future__ import annotations

import contextlib
import json
from typing import Optional

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .auth import bearer_from_headers
from .config import Config, config
from .mcp_tools import build_mcp
from .project import ProjectRegistry, projects_root_from_env


def _transport_security(cfg: Config) -> TransportSecuritySettings:
    if cfg.allowed_hosts == ["*"] or not cfg.allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=cfg.allowed_hosts,
                                     allowed_origins=[f"http://{h}" for h in cfg.allowed_hosts]
                                     + [f"https://{h}" for h in cfg.allowed_hosts])


class ProjectAuthMiddleware:
    """Bearer-token gate for every /p/<name>/… request, checked against that project's tokens."""

    def __init__(self, app, registry: ProjectRegistry, cfg: Config):
        self.app = app
        self.registry = registry
        self.cfg = cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/p/"):
            return await self.app(scope, receive, send)
        parts = path.split("/", 3)  # ['', 'p', '<name>', 'rest...']
        name = parts[2] if len(parts) > 2 else ""
        project = self.registry.get(name)
        if project is None:
            return await self._json(send, 404, {"error": f"unknown project {name!r}"})
        if self.cfg.require_auth:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = bearer_from_headers(headers)
            access = project.tokens.verify(token) if token else None
            if access is None:
                return await self._json(send, 401, {"error": "invalid or missing bearer token"},
                                        extra=[(b"www-authenticate", b"Bearer")])
            scope.setdefault("state", {})["client_id"] = access.client_id
        return await self.app(scope, receive, send)

    async def _json(self, send, status, body, extra=None):
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(payload)).encode())]
        if extra:
            headers += extra
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


def build_app(cfg: Optional[Config] = None) -> Starlette:
    cfg = cfg or config()
    cfg.ensure_dirs()
    registry = ProjectRegistry(projects_root_from_env(cfg.data_dir),
                               max_blob_bytes=cfg.max_blob_bytes,
                               blob_grace_seconds=cfg.blob_grace_seconds)
    registry.discover()

    mounts = []
    mcps = []
    for project in registry.all():
        project.tokens.ensure_first_token(client_id=f"{project.name}-bootstrap")
        mcp = build_mcp(project)
        mcps.append(mcp)
        asgi = mcp.streamable_http_app(streamable_http_path="/mcp",
                                       transport_security=_transport_security(cfg),
                                       host=cfg.host)
        mounts.append(Mount(f"/p/{project.name}", app=asgi))

    async def healthz(_req: Request) -> Response:
        return JSONResponse({"ok": True, "projects": [p.name for p in registry.all()]})

    async def list_projects(_req: Request) -> Response:
        return JSONResponse({"projects": [p.name for p in registry.all()]})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            for mcp in mcps:
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    routes = [Route("/healthz", healthz), Route("/projects", list_projects), *mounts]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(ProjectAuthMiddleware, registry=registry, cfg=cfg)
    app.state.registry = registry
    return app


def main() -> None:
    cfg = config()
    cfg.ensure_dirs()
    app = build_app(cfg)
    # surface any freshly-minted bootstrap tokens for each project
    reg: ProjectRegistry = app.state.registry
    for p in reg.all():
        tok_path = p.dir / "tokens.json"
        print(f"[hivemind] project {p.name!r}: data={p.dir}  tokens={tok_path}")
    print(f"[hivemind] listening on http://{cfg.host}:{cfg.port}  "
          f"(MCP: /p/<project>/mcp)  auth={'on' if cfg.require_auth else 'OFF'}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
