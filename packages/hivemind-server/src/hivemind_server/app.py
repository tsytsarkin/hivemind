"""ASGI entrypoint. ONE MCP server, mounted twice: at /mcp, where each tool call names its own
project, and at /p/<name>/ for every project the registry holds (MCP at /p/<name>/mcp, REST blob
routes under the same prefix). A single middleware gates all of it: it resolves the caller's
identity once per request and, for a /p/<name> path, enforces the project ACL — so the REST routes,
which never reach a tool, are covered by the same check as an MCP call. On the neutral endpoint
there is no project in the URL to check, so the ACL moves into the call: envelope.resolve_project
runs it against the project= argument, and refuses a WRITE that named none. The server root
/healthz stays open, and so do a SHARED project's health probe and endpoint index; a private
project answers nothing it has not authorised.
"""
from __future__ import annotations

import contextlib
import json
import logging
import socket
from typing import Optional, Union

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute

from . import bus_ws as _bus_ws_mod
from .auth import bearer_from_headers
from .config import Config, config
from .envelope import set_mount_default, set_registry, visible_projects
from .identity import Identity, IdentityStore, resolve, set_identity
from .mcp_tools import build_mcp
from .project import ProjectRegistry, projects_root_from_env
from .projects_meta import can_access, load_with_problem

log = logging.getLogger(__name__)

# ONE body for "no such project" AND "not yours". Two different answers would let a stranger confirm
# that nik.private exists simply by observing which error came back, and that confirmation is the
# whole of what the private tier is meant to withhold.
PROJECT_DENIED = {"error": "unknown project or not accessible with this token"}
NO_TOKEN = {"error": "invalid or missing bearer token"}
# The project-neutral endpoint is /mcp and nothing else: every other path the MCP app registers
# needs a project to act on, and here there is none in the URL. Says which shape to use, and names
# no project — the caller supplied the path, so echoing nothing about the server gives nothing away.
NOT_NEUTRAL = {"error": "no such endpoint; only /mcp is project-neutral. "
                        "REST endpoints live under /p/<project>/…"}
# project dir -> the reason already warned about, so a file that is broken for a week does not
# write one warning per request (the ACL is consulted on every one of them, and /p/<name>/ answers
# before auth, so an outsider could otherwise flood the log on purpose). Keyed by PATH, not name,
# because one process can host two deployments holding a same-named project.
_METADATA_WARNED: dict = {}


class Denied:
    """A refusal, carried back out of _authorize so __call__ stays a sequence of guard clauses."""

    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: dict, headers=None):
        self.status = status
        self.body = body
        self.headers = headers


def _transport_security(cfg: Config) -> TransportSecuritySettings:
    if cfg.allowed_hosts == ["*"] or not cfg.allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=cfg.allowed_hosts,
                                     allowed_origins=[f"http://{h}" for h in cfg.allowed_hosts]
                                     + [f"https://{h}" for h in cfg.allowed_hosts])


class ProjectAuthMiddleware:
    """Bearer-token gate for every request, plus the project ACL for every /p/<name>/… one.

    This is where a token becomes a person: the caller is resolved once, here, and published on the
    identity contextvar, which is where a tool body reads the caller from (identity.current_identity;
    today only tests do, the write path lands in a later task). Server-level identities are tried
    first, then the project's own legacy tokens (see identity.resolve). It is not the only caller of
    identity.resolve — the root `GET /projects` route resolves independently, because it answers
    before any project is known; it publishes no contextvar, since no tool runs on it.

    A /p/<name> request also leaves the project it authorised on the mount-default contextvar, which
    is what a tool call with no project= argument means and what the REST handlers read their project
    from. The neutral endpoint sets no default (see _neutral).

    The ACL lives here rather than in the tool decorator because the REST surface — blob GET/PUT,
    guide, skills, the project index — never reaches a tool at all: an ACL in the tool layer would
    leave `GET /p/nik.private/blobs/<digest>` open to any authenticated user. Everything under the
    prefix passes through this one gate.
    """

    def __init__(self, app, registry: ProjectRegistry, cfg: Config, identities: IdentityStore):
        self.app = app
        self.registry = registry
        self.cfg = cfg
        self.identities = identities

    async def __call__(self, scope, receive, send):
        # Clear first, before any early return: an in-process ASGI caller (httpx.ASGITransport, or
        # an embedding) invokes the app in its OWN task, so a request that returns early would
        # otherwise leave the previous caller's identity readable. A real server hands every
        # request a fresh context copy, but the invariant must not depend on that.
        set_identity(None)
        set_mount_default(None)          # same reason: a stale project must not become the default
        # Third of the same kind, and it was the one left out: a request with no Host header (or a
        # non-http scope, which returns below) used to leave the PREVIOUS caller's address readable,
        # and bus_connect builds the ws:// URL it hands an agent out of exactly that. Cleared first,
        # then set below if this request actually carries one.
        _bus_ws_mod.set_origin("")
        # Published per request, before any early return, because nothing enforces one app per
        # process: a module-global registry would let a call in one deployment resolve a name against
        # another's data dir. This middleware is the one place that holds both.
        set_registry(self.registry, require_auth=self.cfg.require_auth)
        if scope["type"] != "http":
            # The only non-http route under /p/ is the bus WebSocket, which carries its own
            # credential in the query string (a single-use ticket or a listen key, both minted by
            # bus_connect inside the project and redeemable only against that project's hub). It
            # cannot use the bearer header: the listener is launched by Monitor, which cannot set
            # one. Because this returns before the ACL below, bus_ws.websocket_endpoint runs that
            # check itself — at the handshake and again on the open socket.
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        # Record the address this caller reached us on, so a tool can hand back a URL that works
        # from where the caller is (see bus_ws._ORIGIN).
        host = hdrs.get("x-forwarded-host") or hdrs.get("host") or ""
        if host:
            proto = hdrs.get("x-forwarded-proto") or scope.get("scheme") or "http"
            _bus_ws_mod.set_origin(f"{proto}://{host}")
        # No else: cleared at the top of __call__ with the other two. bus_ws_tools._ws_url falls
        # back to cfg.public_url when it is empty, which is the right answer for "this caller did
        # not tell us where it reached us" — a stale neighbour's address is not.
        if not path.startswith("/p/"):
            return await self._neutral(scope, receive, send, path, hdrs)
        parts = path.split("/", 3)  # ['', 'p', '<name>', 'rest...']
        name = parts[2] if len(parts) > 2 else ""
        tail = parts[3] if len(parts) > 3 else ""
        verdict = self._authorize(hdrs, name, tail)
        if isinstance(verdict, Denied):
            return await self._json(send, verdict.status, verdict.body, extra=verdict.headers)
        if verdict is not None:
            scope.setdefault("state", {})["identity"] = verdict
            scope["state"]["client_id"] = verdict.user
        # Tool bodies read this caller off the contextvar, and they run on the MCP transport's own
        # tasks — safe only because mcp 2.x carries contextvars PER MESSAGE (the transport snapshots
        # the sender's context on every send and the dispatcher runs each handler inside that
        # snapshot), so the identity travels with the call even on the session-based stateful path.
        # We depend on that; an SDK upgrade could remove it.
        # test_a_handshake_era_request_resolves_identity_per_request is what pins it.
        set_identity(verdict)
        # The URL named a project and this caller may reach it, so it is what a tool call with no
        # project= argument means. Published only AFTER _authorize passed, so a denied request never
        # leaves a usable default behind.
        set_mount_default(name)
        return await self.app(scope, receive, send)

    async def _neutral(self, scope, receive, send, path: str, hdrs) -> None:
        """The project-neutral surface: the server root, and /mcp with no project in the URL.

        Only four paths exist outside a /p/ prefix. The MCP app is mounted at the root as well, so
        everything it registers — the blob routes, the guide, the skills and tools listings — is
        reachable here too, and those handlers take their project from the URL, which this one does
        not carry. Rather than trusting each of them to fail closed, the router is closed instead:
        anything but the four is a 404 before it reaches a handler.
        """
        if path in ("/", "/healthz", "/projects"):
            # Answered by the server-root routes, which resolve their own caller (GET /projects) or
            # name nothing at all. No project, so no ACL to apply here.
            return await self.app(scope, receive, send)
        if path.rstrip("/") != "/mcp":
            return await self._json(send, 404, NOT_NEUTRAL)
        token = bearer_from_headers(hdrs)
        who = resolve(token, self.identities, None)
        if self.cfg.require_auth and who is None:
            # `resolve(..., None)` deliberately refuses a legacy project token here: it is pinned to
            # the project whose tokens.json holds it, and this endpoint has not named a project yet.
            # Such a caller uses its own /p/<name>/mcp, where the pin can be checked.
            return await self._json(send, 401, NO_TOKEN,
                                    extra=[(b"www-authenticate", b"Bearer")])
        set_identity(who)
        # No mount default: on this endpoint the tool argument is the only thing that says which
        # project a call is for, which is why a write with no argument is refused rather than
        # defaulted (envelope.resolve_project).
        return await self.app(scope, receive, send)

    def _authorize(self, hdrs, name: str, tail: str) -> Union[Identity, None, Denied]:
        """Resolve the caller and decide whether this project is theirs to reach.

        Three verdicts, and None means ALLOW rather than "no caller resolved": an Identity to
        publish; None for the two allowed requests that have no caller — auth is off, or a shared
        project's health/index answering without a token; or a Denied to send back instead. Do not
        read None as "auth is off"; it is "proceed with nobody".

        Lifted out of __call__ so the request path stays readable as guard clauses; every refusal
        here answers with PROJECT_DENIED, so an outsider cannot tell a private project from a typo.
        """
        project = self.registry.get(name)
        if project is None:
            return Denied(404, PROJECT_DENIED)
        if not self.cfg.require_auth:
            # HIVEMIND_REQUIRE_AUTH=0 is the supported no-auth local mode: with no credential there
            # is nobody to authorize, so there is no ACL either — by construction, not by omission.
            # Deliberately BEFORE the metadata read, so this mode never touches project.json on the
            # request path: nothing here consults the file, so a broken one changes nothing and
            # warning about it would be noise. A local operator who wants it checked turns auth on.
            return None
        meta, problem = load_with_problem(project.dir, project.name)
        self._note_metadata(project, problem)
        who = resolve(bearer_from_headers(hdrs), self.identities, project)
        if who is None:
            if meta.visibility != "shared":
                # A private project owes an unauthenticated caller nothing — not its data, not its
                # index, not even its liveness. Answering healthz here would confirm it exists to
                # someone holding no credential at all, which is a cheaper oracle than any of the
                # ones above.
                return Denied(404, PROJECT_DENIED)
            # A shared project is knowable to everyone by definition, so its health probe and
            # endpoint index answer without a token: a client may hold only the project base URL,
            # and a healthy server must not look dead to it. Neither exposes project data.
            #
            # This tuple is the entire reason a shared project's REST surface still needs a token,
            # and one more entry in it is an ACL bypass — envelope.set_registry names this line for
            # that reason. test_a_shared_projects_open_tails_are_exactly_two pins both halves: these
            # two open at 200, everything else 401. Do not widen it without changing that test, and
            # do not widen it by changing that test.
            if tail not in ("", "healthz"):
                return Denied(401, NO_TOKEN, [(b"www-authenticate", b"Bearer")])
            return None
        if not can_access(who, meta):
            return Denied(404, PROJECT_DENIED)
        return who

    def _note_metadata(self, project, problem: Optional[str]) -> None:
        """Log unreadable metadata. The response cannot say so — that would be the oracle — but an
        operator who typos project.json must be able to find out from somewhere."""
        key = str(project.dir)
        if problem is None:
            _METADATA_WARNED.pop(key, None)      # fixed: warn again if it breaks a second time
        elif _METADATA_WARNED.get(key) != problem:
            _METADATA_WARNED[key] = problem
            log.warning("project %r at %s: %s — failing closed, every caller sees the generic 404",
                        project.name, project.dir, problem)

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
    identities = IdentityStore(cfg.identities_path)

    # ONE MCP server for every project: each tool resolves its project per call (see envelope), so
    # the same ASGI app can serve the neutral /mcp endpoint and every /p/<name> prefix. It used to
    # be one server per project, which made the project a property of the connection — and so
    # unanswerable for an agent holding one token and working in two projects at once.
    mcp = build_mcp(registry, identities)
    asgi = mcp.streamable_http_app(streamable_http_path="/mcp",
                                   transport_security=_transport_security(cfg),
                                   host=cfg.host)
    # Both halves of "serve this project under its own prefix" are factored out of the startup
    # loop, because a project created later — through project_create, in this process — needs
    # EXACTLY the same treatment. They used to be inline here, which is why such a project was
    # answered by the neutral /mcp immediately but 404'd on every path under /p/<name>/ (blob
    # upload, guide, catalogs, its own /mcp) until somebody restarted the server.
    def _prepare_project(project) -> None:
        project.tokens.ensure_first_token(client_id=f"{project.name}-bootstrap")
        from . import guide as _guide
        _guide.ensure_core_guide(project.db)
        # At startup, not on first use: a listener reconnecting after a server restart redeems a
        # listen key signed with this secret, and no tool call need have happened first. Without it
        # bus_ws._secret would mint a throwaway one and reject the key.
        _bus_ws_mod.register_secret(project.dir)
        # ...and record that this project HAS a /p/<name>/ prefix, which is the thing bus_connect
        # cannot otherwise know. Recorded beside the routes it describes — and now added in the
        # same breath as them, so "mounted" and "has routes" cannot drift apart.
        _bus_ws_mod.register_mount(project.dir)

    def _project_routes(project) -> list:
        # The bus WebSocket is mounted at the Starlette level: MCPServer.custom_route registers
        # HTTP methods only, so a ws route cannot go through it. Auth is the connect ticket in the
        # query string (see bus_ws), not the bearer header, because the listener is launched by
        # Monitor and cannot set headers. The project DIRECTORY goes with the name because the
        # endpoint is where the bus ACL is enforced (the middleware below skips non-HTTP scopes),
        # and it reads project.json from that directory on every check.
        # require_auth travels with it for the same reason envelope.set_registry takes it
        # explicitly: with auth off _authorize above applies no ACL to any other surface, and a bus
        # that failed closed on the same request would be the one thing such a deployment could not
        # use — its credentials are minted with no user in them at all.
        def _ws_route(p=project, require_auth=cfg.require_auth):
            async def endpoint(ws):
                from . import bus_ws as _b
                await _b.websocket_endpoint(ws, p.name, p.dir, require_auth=require_auth)
            return endpoint
        def _chat_route(p=project, require_auth=cfg.require_auth):
            async def endpoint(ws):
                from . import chat_ws as _c
                await _c.websocket_endpoint(ws, p.name, p.dir, require_auth=require_auth,
                                            identities=identities, db=p.db)
            return endpoint
        # The ws route comes FIRST: Starlette takes the first matching route, and
        # Mount("/p/<name>") would otherwise swallow this path into the MCP app, which has no
        # websocket handler and so refuses the connection.
        return [WebSocketRoute(f"/p/{project.name}/bus/ws", _ws_route()),
                WebSocketRoute(f"/p/{project.name}/chat/ws", _chat_route()),
                Mount(f"/p/{project.name}", app=asgi)]

    mounts = []
    for project in registry.all():
        _prepare_project(project)
        mounts.extend(_project_routes(project))
    # Last, so the project prefixes and the root routes below match first. This is the neutral
    # endpoint: /mcp works here, and ProjectAuthMiddleware._neutral 404s every other path it
    # exposes, since those need a project the URL does not carry.
    mounts.append(Mount("", app=asgi))

    # The three routes below are the server root. All three used to hand every project name to
    # anyone who asked, which is the same existence oracle the /p/ ACL removes — fixing only one of
    # them would be theatre.
    async def healthz(_req: Request) -> Response:
        return JSONResponse({"ok": True})

    async def list_projects(req: Request) -> Response:
        who = resolve(bearer_from_headers(req.headers), identities, None)
        if cfg.require_auth and who is None:
            # `resolve(..., None)` only accepts a server-level identity: a legacy project token is
            # pinned to one project and cannot be recognised without knowing which, so it uses its
            # own project base URL instead.
            return JSONResponse(NO_TOKEN, status_code=401,
                                headers={"www-authenticate": "Bearer"})
        # The same list the tool layer names in its refusals, from one helper: two spellings of
        # "projects you can use" would eventually disagree, and this is the one an agent is told to
        # trust. It answers for the auth-off mode too, where every project is reachable.
        return JSONResponse({"projects": visible_projects(who)})

    async def index(req: Request) -> Response:
        root = str(req.base_url).rstrip("/")
        return JSONResponse({
            "service": "hivemind",
            "health": f"{root}/healthz",
            # Project-neutral: pass project=<name> to each tool. The per-project form still works.
            "mcp": f"{root}/mcp",
            "mcp_per_project": f"{root}/p/<project>/mcp",
            "your_projects": f"{root}/projects",
            "note": ("project names are not listed here — GET /projects with your token, or point "
                     "clients at a project base URL"),
        })

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        import asyncio as _asyncio

        from . import bus_ws as _bus_ws
        from .chat import ChatStore
        from . import graph_tasks
        # WebSocket sends issued from MCP tool threads are scheduled onto this loop.
        _bus_ws.set_loop(_asyncio.get_running_loop())

        async def clean_chat() -> None:
            for current in registry.all():
                try:
                    await _asyncio.to_thread(ChatStore(current.db).cleanup)
                    await _asyncio.to_thread(graph_tasks.reap_expired, current.db)
                except Exception:
                    log.exception("chat retention cleanup failed for project %r", current.name)

        async def housekeeping() -> None:
            while True:
                await _asyncio.sleep(3600)
                await clean_chat()

        async with mcp.session_manager.run():
            await clean_chat()
            cleanup_task = _asyncio.create_task(housekeeping())
            try:
                yield
            finally:
                cleanup_task.cancel()
                with contextlib.suppress(_asyncio.CancelledError):
                    await cleanup_task

    routes = [Route("/", index), Route("/healthz", healthz),
              Route("/projects", list_projects), *mounts]
    app = Starlette(routes=routes, lifespan=lifespan)

    def _serve_new_project(project) -> None:
        """Give a just-created project its /p/<name>/ routes on the LIVE app.

        Called by project_tools right after the project is fully built, so `project_create` returns
        something usable immediately instead of a project whose own prefix 404s until the next
        restart. It does exactly what the startup loop does — same prep, same two routes — so a
        restart converges on the identical state rather than changing behaviour after the fact.

        INSERTED, not appended: the trailing Mount("") matches every path, so a route added after
        it could never match. It goes immediately before that catch-all, which also keeps it after
        the three root routes — the same relative order build_app produces at startup.
        """
        _prepare_project(project)
        for route in _project_routes(project):
            app.router.routes.insert(len(app.router.routes) - 1, route)

    # Hung on the registry because that is what project_tools already holds; it has no other handle
    # on the running app. Absent (an app built without this, or a bare registry in a test) simply
    # means no dynamic routes, which is the old behaviour rather than an error.
    registry.on_project_created = _serve_new_project

    app.add_middleware(ProjectAuthMiddleware, registry=registry, cfg=cfg, identities=identities)
    app.state.registry = registry
    app.state.cfg = cfg
    app.state.identities = identities
    return app


def ui_listener_app(cfg: Config, mcp_app: Starlette):
    """A disabled or unauthenticated deployment never opens a human-console socket."""
    if not cfg.ui_enabled or not cfg.require_auth:
        return None
    from .ui_app import build_ui_app
    return build_ui_app(cfg, mcp_app.state.registry, mcp_app.state.identities)


def _open_listener(host: str, port: int) -> socket.socket:
    """Bind before starting either server so port conflicts cannot leave a half-live deployment."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family)


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
          f"(MCP: /mcp with project=<name>, or /p/<project>/mcp)  "
          f"auth={'on' if cfg.require_auth else 'OFF'}")
    ui = ui_listener_app(cfg, app)
    if ui is None:
        if cfg.ui_enabled and not cfg.require_auth:
            print("[hivemind] web UI disabled because token authentication is off")
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
        return
    import asyncio

    async def serve_both(mcp_socket, ui_socket):
        mcp_server = uvicorn.Server(uvicorn.Config(app, log_level="info"))
        ui_server = uvicorn.Server(uvicorn.Config(ui, log_level="info"))
        print(f"[hivemind] web UI listening on http://{cfg.ui_host}:{cfg.ui_port}")
        running = [asyncio.create_task(mcp_server.serve(sockets=[mcp_socket])),
                   asyncio.create_task(ui_server.serve(sockets=[ui_socket]))]
        try:
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            mcp_server.should_exit = ui_server.should_exit = True
            await asyncio.gather(*running)
            for task in done:
                task.result()
        finally:
            mcp_server.should_exit = ui_server.should_exit = True
            for task in running:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    with _open_listener(cfg.host, cfg.port) as mcp_socket, \
            _open_listener(cfg.ui_host, cfg.ui_port) as ui_socket:
        asyncio.run(serve_both(mcp_socket, ui_socket))


if __name__ == "__main__":
    main()
