"""Attach artifact + tool-registry MCP tools and REST routes to a project's MCP server.
Called from mcp_tools.build_mcp. Artifact bytes move over REST (rest_blobs); these tools handle
references, attachment, and (task 6) the tool registry.
"""
from __future__ import annotations

import functools
from typing import Optional

from mcp.types import ToolAnnotations

from . import registry as reg
from .db import Conflict, Invalid, NotFound
from .rest_blobs import register_blob_routes
from .rest_guide import register_guide_routes
from .rest_skills import register_skill_routes, register_tool_routes

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)


def _envelope(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            out = fn(*a, **k)
            if isinstance(out, dict) and "ok" not in out:
                out = {"ok": True, **out}
            return out
        except Conflict as e:
            return {"ok": False, "error_kind": "conflict", "error": str(e)}
        except NotFound as e:
            return {"ok": False, "error_kind": "not_found", "error": str(e)}
        except Invalid as e:
            return {"ok": False, "error_kind": "invalid", "error": str(e)}
    return wrap


def attach(mcp, project) -> None:
    register_blob_routes(mcp, project)
    register_guide_routes(mcp, project)
    register_skill_routes(mcp, project)
    register_tool_routes(mcp, project)
    store = project.blobs
    db = project.db
    base = f"/p/{project.name}"

    # ── artifact tools (bytes go over REST; these manage references) ────────────────
    @mcp.tool(annotations=RO,
              description="Resolve a stored artifact by digest: returns size, media type, and a "
                          "resource_link URL to fetch the bytes over REST (not inline).")
    @_envelope
    def artifact_ref(digest: str) -> dict:
        meta = store.stat(digest)
        href = f"{base}/blobs/{digest.replace(':', '/', 1)}"
        return {"digest": digest, "size": meta["size"], "media_type": meta.get("media_type"),
                "resource_link": href, "upload_hint": f"PUT {href}"}

    @mcp.tool(annotations=WRITE,
              description="Attach an already-uploaded artifact (by digest) to a node/edge VERSION "
                          "with a role label (e.g. 'binary','crashlog','poc'). Upload bytes first "
                          "via `PUT /blobs/<algo>/<hex>` (the hivemind CLI does this).")
    @_envelope
    def artifact_attach(digest: str, version_id: str, role: str = "attachment",
                        filename: Optional[str] = None, agent: str = "agent") -> dict:
        return store.attach(agent, digest, version_id, role=role, filename=filename)

    @mcp.tool(annotations=RO,
              description="List the node/edge versions that reference an artifact digest.")
    @_envelope
    def artifact_refs(digest: str) -> dict:
        return {"digest": digest, "refs": store.refs(digest)}

    # ── tool-registry tools (implemented in task 6 / registry.py) ───────────────────
    reg.attach_tools(mcp, project, _envelope, RO, WRITE, base)
