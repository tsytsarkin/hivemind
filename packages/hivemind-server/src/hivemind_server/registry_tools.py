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
                          "resource_link URL to fetch the bytes over REST (not inline). A unique "
                          "digest PREFIX (8+ hex characters, as a listing shows) is accepted; an "
                          "unknown or ambiguous one is an error, never an empty answer.")
    @_envelope
    def artifact_ref(digest: str) -> dict:
        digest = store.resolve_digest(digest)    # a prefix is what a listing actually gives you
        meta = store.stat(digest)
        href = f"{_base()}/blobs/{digest.replace(':', '/', 1)}"
        return {"digest": digest, "size": meta["size"], "media_type": meta.get("media_type"),
                "resource_link": href, "upload_hint": f"PUT {href}"}

    @mcp.tool(annotations=WRITE,
              description="Attach an already-uploaded artifact (by digest) to a node/edge VERSION "
                          "with a role label (e.g. 'binary','crashlog','poc'). Upload bytes first "
                          "via `PUT /blobs/<algo>/<hex>` (the hivemind CLI does this). A unique "
                          "digest prefix (8+ hex characters) is accepted here too.")
    @_envelope
    def artifact_attach(digest: str, version_id: str, role: str = "attachment",
                        filename: Optional[str] = None, agent: str = "agent") -> dict:
        return store.attach(agent, digest, version_id, role=role, filename=filename)

    @mcp.tool(annotations=RO,
              description="Report uploads that were never attached to anything, grouped by "
                          "uploader (the person the token names, with the agent label they used). "
                          "Uploading is not recording — unattached bytes are invisible to other "
                          "agents and are garbage-collected. Check this for your own user after "
                          "uploading a batch.")
    @_envelope
    def artifact_orphans(older_than_hours: int = 0, limit: int = 20) -> dict:
        return store.orphans(older_than_hours=older_than_hours, limit=limit)

    @mcp.tool(annotations=RO,
              description="List the node/edge versions that ATTACH an artifact digest (its "
                          "blob_ref rows). A unique digest PREFIX (8+ hex characters — what a "
                          "listing displays) is accepted and resolved; the full digest it "
                          "matched comes back in the reply. An unknown, ambiguous or malformed "
                          "digest is an ERROR, not an empty result. An empty `refs` list means "
                          "only that nothing ATTACHED it — it does NOT mean orphaned or "
                          "about-to-be-collected: a digest recorded in a node's props, or "
                          "published as a tool artifact, is still a GC root and has no blob_ref "
                          "row. Use artifact_orphans for what is genuinely unreferenced.")
    @_envelope
    def artifact_refs(digest: str) -> dict:
        # One resolve, not two: a caller that pasted a prefix needs the full digest echoed back,
        # and a caller whose digest matched nothing needs to be told so rather than handed an
        # empty list. Resolving here and again inside refs() cost two index scans per call.
        resolved, rows = store.resolve_and_refs(digest)
        return {"digest": resolved, "refs": rows}

    # ── tool-registry tools (implemented in task 6 / registry.py) ───────────────────
    reg.attach_tools(mcp, db, _envelope, RO, WRITE)
