"""Attach artifact + tool-registry MCP tools and REST routes to the MCP server.
Called from mcp_tools.build_mcp. Artifact bytes move over REST (rest_blobs); these tools handle
references, attachment, and (task 6) the tool registry.
"""
from __future__ import annotations

from typing import Optional

from . import registry as reg
from .envelope import (RO, WRITE, CurrentBlobs, CurrentDb, current_project,
                       envelope as _envelope)
from .rest_blobs import register_blob_routes
from .rest_guide import register_guide_routes
from .rest_skills import (register_index_routes, register_skill_routes,
                          register_tool_routes)


def _base() -> str:
    """The URL prefix of the project this CALL is for. Built per call, never at attach time: one
    server now answers for every project, so a baked-in prefix would hand back another project's
    URLs."""
    return f"/p/{current_project().name}"


def attach(mcp) -> None:
    # Every register_* below builds the per-call proxies it needs itself. Uniform on purpose: a
    # function taking a project as an argument is one a caller can hand a real Project to, which
    # would bind it at attach time and serve one project's data on every prefix.
    register_blob_routes(mcp)
    register_guide_routes(mcp)
    register_skill_routes(mcp)
    register_tool_routes(mcp)
    register_index_routes(mcp)
    store = CurrentBlobs()        # both resolve per call — see envelope._Current
    db = CurrentDb()

    # ── artifact tools (bytes go over REST; these manage references) ────────────────
    @mcp.tool(annotations=RO,
              description="Resolve a stored artifact by digest: returns size, media type, and a "
                          "resource_link URL to fetch the bytes over REST (not inline).")
    @_envelope
    def artifact_ref(digest: str) -> dict:
        meta = store.stat(digest)
        href = f"{_base()}/blobs/{digest.replace(':', '/', 1)}"
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
              description="Report uploads that were never attached to anything, by agent. "
                          "Uploading is not recording — unattached bytes are invisible to other "
                          "agents and are garbage-collected. Check this for your own agent id "
                          "after uploading a batch.")
    @_envelope
    def artifact_orphans(older_than_hours: int = 0, limit: int = 20) -> dict:
        return store.orphans(older_than_hours=older_than_hours, limit=limit)

    @mcp.tool(annotations=RO,
              description="List the node/edge versions that reference an artifact digest.")
    @_envelope
    def artifact_refs(digest: str) -> dict:
        return {"digest": digest, "refs": store.refs(digest)}

    # ── tool-registry tools (implemented in task 6 / registry.py) ───────────────────
    reg.attach_tools(mcp, db, _envelope, RO, WRITE)
