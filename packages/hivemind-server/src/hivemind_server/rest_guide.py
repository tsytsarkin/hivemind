"""REST guide endpoints so a plain `curl` (the skill's guide.sh) can pull the live guide with an
ETag cache. Same /p/<project> prefix, same bearer auth. ETag = the section's guide_version."""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import guide
from .db import NotFound


def register_guide_routes(mcp, project) -> None:
    db = project.db

    @mcp.custom_route("/guide", methods=["GET"])
    async def guide_index(_req: Request) -> Response:
        return JSONResponse(await run_in_threadpool(guide.get_index, db))

    @mcp.custom_route("/guide/{section}", methods=["GET"])
    async def guide_section(req: Request) -> Response:
        name = req.path_params["section"]
        try:
            sec = await run_in_threadpool(guide.get_section, db, name)
        except NotFound:
            return Response(status_code=404)
        etag = f'"{sec["guide_version"]}"'
        if req.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return PlainTextResponse(sec["body"], headers={
            "ETag": etag, "Cache-Control": "no-cache",
            "X-Guide-Version": str(sec["guide_version"])})
