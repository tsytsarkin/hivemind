"""REST blob endpoints, registered on each project's MCP app via custom_route so they live in the
same process/prefix (/p/<project>/...). Large bytes move here, never through JSON-RPC.

  PUT  /blobs/{algo}/{hex}     stream upload, verify digest (idempotent 201/200)
  GET  /blobs/{algo}/{hex}     stream download (Range-aware), immutable cache
  HEAD /blobs/{algo}/{hex}     existence + size
  POST /blobs/batch            Git-LFS style: {objects:[{oid,size}]} -> which are missing
"""
from __future__ import annotations

import hashlib
import json
import os

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .db import Invalid, NotFound

_UPLOAD_CHUNK = 1024 * 1024


def register_blob_routes(mcp, project) -> None:
    store = project.blobs

    def _digest(req: Request) -> str:
        return f"{req.path_params['algo']}:{req.path_params['hex']}"

    def _agent(req: Request) -> str:
        return req.headers.get("x-hivemind-agent", req.scope.get("state", {}).get(
            "client_id", "rest"))

    @mcp.custom_route("/blobs/{algo}/{hex}", methods=["PUT"])
    async def put_blob(req: Request) -> Response:
        try:
            declared = _digest(req)
            store.parse_digest(declared)
        except Invalid as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if store.exists(declared):
            return JSONResponse({"digest": declared, "deduplicated": True,
                                 "next": ("already stored — still attach it with "
                                          "artifact_attach(digest, version_id, role=...)")},
                                status_code=200)
        tmp = await run_in_threadpool(store.new_tmp)
        h = hashlib.sha256()
        size = 0
        try:
            f = await run_in_threadpool(open, tmp, "wb")
            try:
                async for chunk in req.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    h.update(chunk)
                    await run_in_threadpool(f.write, chunk)
            finally:
                await run_in_threadpool(f.close)
            computed = "sha256:" + h.hexdigest()
            media = req.headers.get("content-type")
            res = await run_in_threadpool(
                store.finalize_written, tmp, computed, size, media, _agent(req),
            )
            # Attach in the same request when the caller says what it belongs to. The leak that
            # produced 94GB of garbage was upload-then-forget, so the fix is to make attaching
            # part of the upload rather than a second call somebody has to remember.
            attach_to = req.query_params.get("attach_to")
            if attach_to:
                try:
                    await run_in_threadpool(
                        store.attach, _agent(req), computed, attach_to,
                        role=req.query_params.get("role", "attachment"),
                        filename=req.query_params.get("filename"))
                    res["attached_to"] = attach_to
                except (Invalid, NotFound) as e:
                    res["attach_error"] = str(e)
            if computed != declared:
                return JSONResponse(
                    {"error": f"digest mismatch: url {declared}, body {computed}"},
                    status_code=400)
            if not res.get("attached_to"):
                res["next"] = ("attach it: artifact_attach(digest, version_id, role=...), or pass "
                               "?attach_to=<version_id> on the upload — an unattached upload is "
                               "invisible to other agents and is eventually garbage-collected")
            return JSONResponse(res, status_code=201)
        except Invalid as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/blobs/{algo}/{hex}", methods=["HEAD", "GET"])
    async def get_blob(req: Request) -> Response:
        digest = _digest(req)
        try:
            meta = await run_in_threadpool(store.stat, digest)
        except NotFound:
            return Response(status_code=404)
        headers = {"Content-Length": str(meta["size"]),
                   "Cache-Control": "public, max-age=31536000, immutable",
                   "ETag": f'"{digest}"', "Accept-Ranges": "bytes"}
        if meta.get("media_type"):
            headers["Content-Type"] = meta["media_type"]
        if req.method == "HEAD":
            return Response(status_code=200, headers=headers)

        path = store.path_for(digest)

        def _iter():
            with open(path, "rb") as fh:
                while True:
                    b = fh.read(_UPLOAD_CHUNK)
                    if not b:
                        break
                    yield b

        return StreamingResponse(_iter(), status_code=200, headers=headers,
                                 media_type=headers.get("Content-Type",
                                                        "application/octet-stream"))

    @mcp.custom_route("/blobs/batch", methods=["POST"])
    async def batch(req: Request) -> Response:
        body = await req.json()
        objects = body.get("objects", [])
        out = []
        for o in objects:
            oid = o.get("oid")
            try:
                present = bool(oid) and await run_in_threadpool(store.exists, oid)
            except Invalid:
                out.append({"oid": oid, "error": "bad digest"})
                continue
            entry = {"oid": oid, "size": o.get("size"), "present": present}
            base = f"/p/{project.name}/blobs/{oid.replace(':', '/', 1)}" if oid else None
            entry["actions"] = ({"download": {"href": base}} if present
                                else {"upload": {"href": base}})
            out.append(entry)
        return JSONResponse({"objects": out})
