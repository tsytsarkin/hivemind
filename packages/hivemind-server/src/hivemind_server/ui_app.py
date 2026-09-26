"""Separate-port, credential-scoped human console; no agent MCP routes are mounted here."""
from __future__ import annotations

import hmac
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .identity import IdentityStore, set_identity
from .db import Invalid
from .projects_meta import can_access
from .ui_payload import TooLarge, read_json
from .ui_sessions import UISessions

COOKIE = "hm_ui_session"
PROJECT_DENIED = {"error": "unknown project or not accessible with this token"}
ASSETS = Path(__file__).with_name("ui_assets")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def _json(data, status=200):
    return JSONResponse(data, status_code=status, headers={"cache-control": "no-store",
                                               "x-content-type-options": "nosniff"})


def build_ui_app(cfg, registry, identities: IdentityStore) -> Starlette:
    sessions = UISessions(cfg.data_dir / "ui-sessions.db")
    failures: dict[str, list[float]] = {}

    def user(req: Request, *, unsafe: bool = False):
        session = sessions.lookup(req.cookies.get(COOKIE), identities)
        if session is None:
            return None, _json({"error": "login required"}, 401)
        who, csrf = session
        if unsafe:
            origin = req.headers.get("origin")
            if (origin and origin != str(req.base_url).rstrip("/")) or not hmac.compare_digest(
                    req.headers.get("x-csrf-token", ""), csrf):
                return None, _json({"error": "invalid origin or CSRF token"}, 403)
        set_identity(who)
        return who, None

    def project(req: Request, who):
        p = registry.get(req.path_params["project"])
        if p is None or not can_access(who, p.meta):
            return None, _json(PROJECT_DENIED, 404)
        return p, None

    async def login(req: Request):
        origin = req.headers.get("origin")
        if origin and origin != str(req.base_url).rstrip("/"):
            return _json({"error": "invalid origin"}, 403)
        ip = req.client.host if req.client else "unknown"
        now = time.time()
        failures[ip] = [t for t in failures.get(ip, []) if now - t < 60]
        if len(failures[ip]) >= 10:
            return _json({"error": "too many login attempts"}, 429)
        try:
            payload = await read_json(req, max_bytes=8192)
        except TooLarge:
            return _json({"error": "login request is too large"}, 413)
        except Invalid:
            payload = {}
        token = payload.get("token") if isinstance(payload, dict) else None
        who = identities.verify(token) if isinstance(token, str) and len(token) < 4096 else None
        if who is None:
            failures[ip].append(now)
            return _json({"error": "invalid server user token"}, 401)
        secret, csrf = sessions.create(who)
        answer = _json({"user": who.user, "device": who.device, "csrf_token": csrf})
        answer.set_cookie(COOKIE, secret, httponly=True, samesite="strict",
                          secure=req.url.scheme == "https", max_age=24 * 3600, path="/")
        return answer

    async def logout(req: Request):
        _, error = user(req, unsafe=True)
        if error:
            return error
        sessions.revoke(req.cookies.get(COOKIE))
        answer = _json({"ok": True})
        answer.delete_cookie(COOKIE, path="/")
        return answer

    async def projects(req: Request):
        who, error = user(req)
        if error:
            return error
        return _json({"projects": [p.name for p in registry.all() if can_access(who, p.meta)]})

    async def session(req: Request):
        who, error = user(req)
        if error:
            return error
        current = sessions.lookup(req.cookies.get(COOKIE), identities)
        return _json({"user": who.user, "device": who.device, "csrf_token": current[1]})

    async def shell(_req: Request):
        return FileResponse(ASSETS / "index.html", media_type="text/html",
                            headers={"cache-control": "no-store"})

    async def asset(req: Request):
        name = req.path_params["name"]
        if name not in ("styles.css", "app.js"):
            return _json({"error": "not found"}, 404)
        media_type = "text/css" if name.endswith(".css") else "text/javascript"
        return FileResponse(ASSETS / name, media_type=media_type)

    async def overview(req: Request):
        who, error = user(req)
        if error:
            return error
        p, error = project(req, who)
        if error:
            return error
        from .chat import ChatStore
        from . import instructions
        return _json({"project": p.name, "rooms": ChatStore(p.db).rooms(),
                      "agents": ChatStore(p.db).agents(),
                      "instructions": instructions.list_project(p.db, limit=25)})

    async def project_api(req: Request):
        who, error = user(req, unsafe=req.method == "POST")
        if error:
            return error
        p, error = project(req, who)
        if error:
            return error
        from . import ui_api
        return await ui_api.handle(req, p, who, identities)

    async def browser_headers(req, call_next):
        set_identity(None)
        try:
            response = await call_next(req)
            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            set_identity(None)

    app = Starlette(routes=[Route("/", shell), Route("/assets/{name}", asset),
                            Route("/api/login", login, methods=["POST"]),
                            Route("/api/logout", logout, methods=["POST"]),
                            Route("/api/session", session),
                            Route("/api/projects", projects),
                            Route("/api/projects/{project}/overview", overview),
                            Route("/api/projects/{project}/{tail:path}", project_api,
                                  methods=["GET", "POST"])],
                   middleware=[Middleware(BaseHTTPMiddleware, dispatch=browser_headers)])
    app.state.sessions, app.state.registry, app.state.identities = sessions, registry, identities
    return app
