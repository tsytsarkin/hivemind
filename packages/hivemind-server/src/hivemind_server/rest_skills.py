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

from . import registry, skills
from .db import NotFound


def register_skill_routes(mcp, project) -> None:
    db = project.db

    @mcp.custom_route("/skills", methods=["GET"])
    async def skill_catalog(req: Request) -> Response:
        topic = req.query_params.get("topic")
        limit = min(int(req.query_params.get("limit", 100)), 500)
        offset = max(int(req.query_params.get("offset", 0)), 0)
        return JSONResponse(await run_in_threadpool(skills.catalog, db, topic=topic,
                                                    limit=limit, offset=offset))

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


def register_index_routes(mcp, project) -> None:
    """Health and an endpoint index UNDER the project prefix.

    Clients are configured with the project base URL (…/p/<project>), so `<base>/healthz` is the
    natural probe — it used to 404 and make a healthy server look dead. Both this and the
    server-root /healthz now answer.
    """
    db = project.db

    @mcp.custom_route("/healthz", methods=["GET"])
    async def project_health(_req: Request) -> Response:
        return JSONResponse({"ok": True, "project": project.name})

    @mcp.custom_route("/", methods=["GET"])
    async def project_index(req: Request) -> Response:
        base = str(req.url).rstrip("/")
        return JSONResponse({
            "project": project.name,
            "mcp": f"{base}/mcp",
            "endpoints": {
                "health": f"{base}/healthz",
                "guide": f"{base}/guide",
                "guide_section": f"{base}/guide/{{section}}",
                "skills": f"{base}/skills?topic=&limit=&offset=",
                "skill": f"{base}/skills/{{id}}?constraint=",
                "tools": f"{base}/tools?topic=&limit=&offset=",
                "tool": f"{base}/tools/{{id}}?constraint=",
                "blob": f"{base}/blobs/{{algo}}/{{hex}}",
                "blob_upload": f"PUT {base}/blobs/{{algo}}/{{hex}}?attach_to=&role=",
                "blob_batch": f"POST {base}/blobs/batch",
            },
            "note": "all endpoints except health require Authorization: Bearer <token>",
        })


def register_tool_routes(mcp, project) -> None:
    """GET /tools[?topic=] and GET /tools/{id}[?constraint=] — browse the tool registry."""
    db = project.db

    @mcp.custom_route("/tools", methods=["GET"])
    async def tool_catalog(req: Request) -> Response:
        topic = req.query_params.get("topic")
        limit = min(int(req.query_params.get("limit", 100)), 500)
        offset = max(int(req.query_params.get("offset", 0)), 0)
        return JSONResponse(await run_in_threadpool(registry.catalog, db, topic=topic,
                                                    limit=limit, offset=offset))

    @mcp.custom_route("/tools/{tool_id:path}", methods=["GET"])
    async def tool_get(req: Request) -> Response:
        tid = req.path_params["tool_id"]
        constraint = req.query_params.get("constraint", "")
        try:
            out = await run_in_threadpool(registry.resolve, db, tid, constraint=constraint)
        except NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse(out)
