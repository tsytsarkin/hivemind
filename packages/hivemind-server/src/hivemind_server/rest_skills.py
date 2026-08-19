"""REST surface for the mini-skill library, so it can be browsed without an MCP client —
a dashboard, a curl, or an agent that just wants the catalog as JSON.

  GET /skills                 catalog: topics with counts + one line per skill
  GET /skills?topic=ops       narrowed to one tag
  GET /skills/{id}            the full procedure (newest non-yanked version)
  GET /skills/{id}?constraint=^1.0
"""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import skills
from .db import NotFound


def register_skill_routes(mcp, project) -> None:
    db = project.db

    @mcp.custom_route("/skills", methods=["GET"])
    async def skill_catalog(req: Request) -> Response:
        topic = req.query_params.get("topic")
        limit = min(int(req.query_params.get("limit", 200)), 500)
        return JSONResponse(await run_in_threadpool(skills.catalog, db, topic=topic, limit=limit))

    @mcp.custom_route("/skills/{skill_id:path}", methods=["GET"])
    async def skill_get(req: Request) -> Response:
        sid = req.path_params["skill_id"]
        constraint = req.query_params.get("constraint", "")
        try:
            out = await run_in_threadpool(skills.get, db, sid, constraint)
        except NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        out["links"] = await run_in_threadpool(_links_for, db, sid)
        return JSONResponse(out)


def _links_for(db, skill_id: str) -> list:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT node_id, relation, note FROM skill_link WHERE id=?", (skill_id,)).fetchall()
    return [dict(r) for r in rows]
