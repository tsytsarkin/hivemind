"""REST blob endpoints, registered on the MCP app via custom_route so they live in the same
process/prefix (/p/<project>/...). Large bytes move here, never through JSON-RPC.

  PUT  /blobs/{algo}/{hex}     stream upload, verify digest (idempotent 201/200); 413 as soon as
                               the size cap is crossed — on Content-Length before a byte is read
  GET  /blobs/{algo}/{hex}     stream download, immutable cache; honours a single `bytes=` Range
                               (206 + Content-Range, or 416), which `Accept-Ranges` advertises
  HEAD /blobs/{algo}/{hex}     existence + size (same status/headers a GET would answer with)
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
from .envelope import CurrentBlobs, CurrentProject

_UPLOAD_CHUNK = 1024 * 1024


def _unlink(path: str) -> None:
    """Drop an abandoned upload temp file. Best effort: gc() sweeps stale ones anyway, and a
    failure here must not turn a 413 into a 500."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _parse_range(header: str, size: int):
    """Parse a single `bytes=` range against a known size.

    Returns an inclusive `(first, last)` pair, the string `"unsatisfiable"`, or None meaning
    "ignore the header and send the whole body".

    A MULTI-range request (`bytes=0-9,20-29`) is answered with the whole body: this server does
    not build multipart/byteranges, and RFC 9110 permits a server to ignore Range entirely, so
    200-with-everything is always a correct answer where a 206 carrying only the first part
    would be a silently corrupt one. Same for a header we cannot parse.
    """
    if not header:
        return None
    unit, _, spec = header.partition("=")
    spec = spec.strip()
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        return None
    first_s, sep, last_s = spec.partition("-")
    if not sep:
        return None
    first_s, last_s = first_s.strip(), last_s.strip()
    try:
        if not first_s:                       # suffix form: bytes=-N asks for the LAST N bytes
            n = int(last_s)
            if n <= 0:
                return "unsatisfiable"
            first, last = max(0, size - n), size - 1
        else:
            first = int(first_s)
            last = int(last_s) if last_s else size - 1
    except ValueError:
        return None
    if first < 0:
        return None
    # Order matters: `bytes=<past the end>-` derives `last` from the size, so it would look like
    # a backwards range and be ignored (a 200 with the whole body) instead of a 416.
    if first >= size:                         # past the end (and any range on an empty blob)
        return "unsatisfiable"
    if last < first:                          # backwards: unparseable, not unsatisfiable
        return None
    return first, min(last, size - 1)


def register_blob_routes(mcp) -> None:
    # One app serves every project, so both of these resolve per REQUEST. Built here rather than
    # taken as arguments on purpose: a caller passing a real Project would silently re-bind these
    # routes to one project at attach time, and every request would answer for it.
    store = CurrentBlobs()
    project = CurrentProject()

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
        limit = store.max_bytes
        # Refuse on the DECLARED length, before a temp file exists and before a byte is read.
        # The cap used to fire in finalize_written, i.e. after the upload had been streamed to
        # disk in full: a 4 GB PUT burned the entire transfer and then failed.
        declared_len = req.headers.get("content-length")
        if declared_len is not None:
            try:
                n = int(declared_len)
            except ValueError:
                n = -1
            if n > limit:
                return JSONResponse(
                    {"error": f"declared Content-Length {n} exceeds max_blob_bytes {limit}",
                     "max_blob_bytes": limit, "declared_size": n}, status_code=413)
        tmp = await run_in_threadpool(store.new_tmp)
        h = hashlib.sha256()
        size = 0
        over = False
        try:
            f = await run_in_threadpool(open, tmp, "wb")
            try:
                async for chunk in req.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        # Content-Length is a hint (absent under chunked encoding, and free to
                        # lie), so the streaming guard stays — but it breaks AT the crossing
                        # rather than after the last byte, which is the whole point.
                        over = True
                        break
                    h.update(chunk)
                    await run_in_threadpool(f.write, chunk)
            finally:
                await run_in_threadpool(f.close)
            if over:
                await run_in_threadpool(_unlink, tmp)
                return JSONResponse(
                    {"error": f"upload exceeds max_blob_bytes {limit}", "max_blob_bytes": limit},
                    status_code=413)
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
        size = meta["size"]
        headers = {"Content-Length": str(size),
                   "Cache-Control": "public, max-age=31536000, immutable",
                   "ETag": f'"{digest}"', "Accept-Ranges": "bytes"}
        if meta.get("media_type"):
            headers["Content-Type"] = meta["media_type"]

        # Accept-Ranges has been advertised since this endpoint was written and nothing read the
        # header, so a caller that assembled chunks got each chunk's worth of the FILE'S HEAD,
        # repeated, with a 200 on every one of them. Honour it: a resumable multi-GB download is
        # the reason the advertisement is there.
        rng = _parse_range(req.headers.get("range", ""), size)
        if rng == "unsatisfiable":
            # 416 carries the real size so the caller can recompute its range instead of guessing.
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}",
                                                      "Accept-Ranges": "bytes",
                                                      "ETag": headers["ETag"],
                                                      "Content-Length": "0"})
        first, last = rng if rng else (0, size - 1)
        length = (last - first + 1) if size else 0
        if rng:
            # The cache identity of a part is the identity of the whole: same ETag, same
            # immutable Cache-Control, so a resumed download can still validate what it got.
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"
            headers["Content-Length"] = str(length)
        status = 206 if rng else 200
        if req.method == "HEAD":
            return Response(status_code=status, headers=headers)

        path = store.path_for(digest)

        def _iter():
            remaining = length
            with open(path, "rb") as fh:
                if first:
                    fh.seek(first)
                while remaining > 0:
                    b = fh.read(min(_UPLOAD_CHUNK, remaining))
                    if not b:
                        break
                    remaining -= len(b)
                    yield b

        return StreamingResponse(_iter(), status_code=status, headers=headers,
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
