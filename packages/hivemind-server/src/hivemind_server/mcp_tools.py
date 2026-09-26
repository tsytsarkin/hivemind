"""MCP tool surface. `build_mcp` returns ONE MCPServer for every project the registry holds: each
tool takes a `project` argument, and the `db` used in the bodies below is a proxy onto whatever
that resolved to for the call in flight (see envelope). Few powerful, namespaced tools;
read tools annotated read-only; every tool returns a uniform envelope so agents get actionable
errors instead of opaque failures.
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server import MCPServer

from . import graph, guide, schemas, skills, traps
from .envelope import (RO, WRITE, CurrentDb, ProjectAware, envelope as _envelope,
                       set_registry)

INSTRUCTIONS = (
    "Hivemind REPLACES local memory for this fleet: read it before doing any work, and persist "
    "all work into it rather than into local memory files or scratch notes, which no other agent "
    "can see. It is a shared, versioned knowledge graph + artifact store + tool registry. It is domain-agnostic: node and edge TYPES are defined at runtime in the schema. "
    "Before writing, call schema_get to see the types this project defines, and read the live "
    "guide via guide_get(section='core'). Two versioning axes: revision (supersession — pass "
    "expected_head for safe concurrent edits) and subject-version (the version of the described "
    "thing, e.g. an OS build — pass subject_key/subject_version). Large binaries go through the "
    "REST /blobs endpoints (hivemind CLI), never inline. Nodes flagged 'disputed' have an open "
    "assertive edge — resolve before relying on them. graph_get returns a node together with the "
    "mini-skills associated with it, the tools built for it and its traps, so one call tells you "
    "what is already known. Before working out a non-obvious procedure, "
    "ALWAYS search first — tool_search/tool_catalog, skill_search/skill_catalog and "
    "trap_search — before building a tool or working out a procedure; build only if they come "
    "back empty. Publish what you build (tool_publish/skill_publish) and trap_record an "
    "approach the moment you abandon it. Search is hybrid lexical+semantic. "
    "Every tool takes a `project` argument saying which graph to act in; a tool that WRITES "
    "refuses to guess it, so pass it explicitly unless your endpoint URL already names a project. "
    "Separately from the graph there is an AGENT BUS for live peer-to-peer coordination: call "
    "bus_connect ONCE at session start and run the Monitor snippet it returns — messages from "
    "other agents then arrive as notifications with no polling. bus_send / bus_broadcast reach "
    "peers, bus_peers lists who is connected. Bus traffic is ephemeral; anything worth keeping "
    "still goes in the graph."
)


def build_mcp(registry, identities, *, instructions: str = INSTRUCTIONS) -> MCPServer:
    """One project-neutral MCP server. `identities` is threaded in for the project tools
    (project_share has to check the grantee exists before writing a name nobody can hold)."""
    # A default for any caller that is not an HTTP request. ProjectAuthMiddleware re-publishes both
    # per request and THAT is the authoritative one; require_auth stays strict here because
    # build_mcp does not know the deployment's mode.
    set_registry(registry)
    # Resolved per call, so none of the 47 project-aware tool bodies has to know the project
    # exists: the
    # alternative was editing every one of them to take it as an argument. A body that needs the
    # project itself rather than its database adds `project = CurrentProject()` the same way.
    db = CurrentDb()
    real = MCPServer(name="hivemind", instructions=instructions, version="1.3.0")
    # Registration goes through the proxy so a tool cannot be added without project resolution.
    mcp = ProjectAware(real)

    # ── graph reads ──────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO, description="Search nodes by text and/or BY TYPE. Pass types=[...] to restrict, and an EMPTY query with types to browse every node of that type (returns total_of_type). graph_types() lists the types that have data. props_filter={'gated': true, 'kind': 'mach-service'} filters on exact FIELD VALUES - the only way to match booleans and numbers, since text search cannot tell gated=true from gated=false (null matches absent). Each hit carries a 200-character `snippet` by default. fields=[\"title\",\"status\"] instead returns just those keys per hit as real `props` plus that hit's `author`, and is the cheap, precise way to read structure across a page; props=true returns every key the same way. BOTH of those modes are bounded: any one node's props over 4000 characters is replaced by a `_prefix` marker naming its real size (graph_get that node for the rest), and a page STOPS EARLY once 40000 characters of props have been shipped - so at the default limit=25 a fields= page over long props can come back with ~11 hits. props=true additionally caps the page at 10 hits, so prefer fields unless you genuinely need the lot. A shortened reply says props_clamped and names which bound fired in props_clamped_by; has_more/next_cursor then carry the rest, so page on rather than reading a short page as the end. Paginated: pass the next_cursor from a reply back as cursor; has_more says when to stop. author='<user>' restricts to rows whose CURRENT version that identity wrote; see contributors on graph_get for everyone who ever revised a node.")
    @_envelope
    def graph_search(query: str = "", types: Optional[list[str]] = None,
                     limit: int = 25, cursor: int = 0,
                     props_filter: Optional[dict] = None,
                     fields: Optional[list[str]] = None, props: bool = False,
                     author: Optional[str] = None) -> dict:
        out = graph.search_nodes(db, query, types=types, limit=limit, cursor=cursor,
                                 props_filter=props_filter, fields=fields, props=props,
                                 author=author)
        if query:                       # surface known dead-ends for this query, unprompted
            rel = traps.search(db, query, limit=3)["traps"]
            if rel:
                out["related_traps"] = rel
                out["trap_warning"] = ("Known dead-ends match this query — read them before "
                                       "spending time; see trap_get(trap_id).")
        return out

    @mcp.tool(annotations=RO,
              description="Which node types actually hold data, with counts. Use this to pick a "
                          "type to browse; schema_get says what is DEFINED, this says what EXISTS.")
    @_envelope
    def graph_types(subject_key: Optional[str] = None) -> dict:
        return graph.node_types(db, subject_key=subject_key)

    @mcp.tool(annotations=RO,
              description="Fetch a node by node_id OR (subject_key+subject_version). Returns the node plus everything recorded about it: the mini-skills associated with it (with descriptions), the tools built for it, and any traps. "
                          "history=true returns the full revision chain (newest first); "
                          "as_of=<tx_id|ISO time> returns the revision current then.")
    @_envelope
    def graph_get(node_id: Optional[str] = None, subject_key: Optional[str] = None,
                  subject_version: Optional[str] = None, as_of: Any = None,
                  history: bool = False) -> dict:
        out = graph.get_node(db, node_id=node_id, subject_key=subject_key,
                             subject_version=subject_version, as_of=as_of, history=history)
        t = traps.for_node(db, out["node_id"], subject_key=out.get("subject_key"),
                           subject_version=out.get("subject_version"))
        if t:                           # an agent reading this node cannot miss its dead-ends
            out["traps"] = t
        sk = skills.for_node(db, out["node_id"])
        if sk:                          # ...nor the procedures written about it
            out["skills"] = sk
            out["skills_count"] = len(sk)
            out["skills_hint"] = ("mini-skills associated with this node — read the relevant one "
                                  "with skill_get(id) BEFORE working out your own procedure")
        from . import registry as _reg
        tl = _reg.for_node(db, out["node_id"])
        if tl:                          # ...nor the tools built for it
            out["tools"] = tl
        return out

    @mcp.tool(annotations=RO,
              description="List all subject-version cells of a thing (ordered), or with "
                          "as_of_subject resolve the newest cell at/before that version.")
    @_envelope
    def graph_subjects(subject_key: str, as_of_subject: Optional[str] = None) -> dict:
        return graph.list_subjects(db, subject_key, as_of_subject=as_of_subject)

    @mcp.tool(annotations=RO,
              description="Bounded neighbor traversal (depth 1..4) over named edge types. "
                          "direction=out|in|both.")
    @_envelope
    def graph_neighbors(node_id: str, edge_types: Optional[list[str]] = None,
                        depth: int = 1, direction: str = "out") -> dict:
        return graph.neighbors(db, node_id, edge_types=edge_types, depth=depth,
                               direction=direction)

    # ── graph writes ─────────────────────────────────────────────────────────────
    @mcp.tool(annotations=WRITE,
              description="Create a node, or supersede an existing one. Give subject_key+"
                          "subject_version to target a subject cell (new cell = create, existing "
                          "= supersede). Pass expected_head for optimistic concurrency (409 on "
                          "conflict). `agent` is a free-form LABEL for the job; the author is "
                          "taken from your token and cannot be set from here.")
    @_envelope
    def graph_upsert(type: str, props: dict, agent: str = "agent",
                     subject_key: Optional[str] = None, subject_version: Optional[str] = None,
                     subject_order: Optional[str] = None, node_id: Optional[str] = None,
                     expected_head: Optional[str] = None, reason: Optional[str] = None) -> dict:
        out = graph.upsert_node(db, agent, type, props, subject_key=subject_key,
                                subject_version=subject_version, subject_order=subject_order,
                                node_id=node_id, expected_head=expected_head, reason=reason)
        out["schema_version"] = int(db.meta_get("schema_version", "0"))
        return out

    @mcp.tool(annotations=WRITE,
              description="Add or supersede a typed edge (any schema-defined edge type). Bulk "
                          "(versioned=0) types require source_tag. For assertive edge types, put "
                          "status:'open'|'resolved' in props.")
    @_envelope
    def graph_link(edge_type: str, src: str, dst: str, props: Optional[dict] = None,
                   agent: str = "agent", expected_head: Optional[str] = None,
                   source_tag: Optional[str] = None, reason: Optional[str] = None) -> dict:
        out = graph.upsert_edge(db, agent, edge_type, src, dst, props or {},
                                expected_head=expected_head, source_tag=source_tag, reason=reason)
        out["schema_version"] = int(db.meta_get("schema_version", "0"))
        return out

    @mcp.tool(annotations=WRITE,
              description="Wholesale (re)load a bulk edge set under a source_tag (e.g. an imported "
                          "call graph). edges = [[src,dst,{props}], ...]. Replaces the prior set "
                          "for that (edge_type, source_tag).")
    @_envelope
    def graph_bulk_load(edge_type: str, source_tag: str, edges: list,
                        agent: str = "agent") -> dict:
        norm = [(e[0], e[1], e[2] if len(e) > 2 else {}) for e in edges]
        return graph.bulk_replace(db, agent, edge_type, source_tag, norm)

    # ── schema ───────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO,
              description="Dump this project's node/edge types (schemas, traits, status) + "
                          "schema_version. Read this before writing to learn the vocabulary.")
    @_envelope
    def schema_get(kind: Optional[str] = None, name: Optional[str] = None) -> dict:
        return schemas.get_schema(db, kind=kind, name=name)

    @mcp.tool(annotations=WRITE,
              description="Propose an ADDITIVE schema change (new type, optional prop, wider enum, "
                          "new edge type). Creates a 'proposed' type for human promotion. "
                          "traits (edge only): versioned, symmetric, transitive, acyclic, "
                          "assertive, directed, src_types, dst_types, cardinality. "
                          "Destructive changes are rejected — they are human-only.")
    @_envelope
    def schema_propose(kind: str, name: str, json_schema: dict, agent: str = "agent",
                       traits: Optional[dict] = None, why: str = "", force: bool = False) -> dict:
        return schemas.propose_type(db, agent, kind, name, json_schema, traits=traits,
                                    why=why, force=force)

    @mcp.tool(annotations=RO,
              description="What changed in the schema (and guide) since you last looked. Pass the "
                          "`cursor` from a previous schema_get/schema_changes; returns each type "
                          "that appeared or was re-versioned with who/when/why, so you can re-read "
                          "only what moved. Call this when schema_version differs from your last one.")
    @_envelope
    def schema_changes(since_cursor: int = 0, include_guide: bool = True) -> dict:
        return schemas.changes_since(db, since_cursor, include_guide=include_guide)

    @mcp.tool(annotations=WRITE,
              description="Promote a proposed type version to active (reviewer/human action).")
    @_envelope
    def schema_promote(kind: str, name: str, agent: str = "reviewer",
                       version: Optional[int] = None) -> dict:
        return schemas.promote_type(db, agent, kind, name, version=version)

    @mcp.tool(annotations=WRITE,
              description="Apply a domain pack: define many node/edge types as active at once. "
                          "pack = {name, node_types:{name:{schema,parent?}}, "
                          "edge_types:{name:{schema,...traits}}}. Operator action.")
    @_envelope
    def schema_apply(pack: dict, agent: str = "operator", force: bool = False) -> dict:
        return schemas.apply_pack(db, agent, pack, force=force)

    # ── mini-skills: documented procedures, versioned like tools ──────────────────
    @mcp.tool(annotations=RO,
              description="Search the mini-skill registry — procedures other agents wrote down "
                          "(how to do a complex action, with the gotchas). ALWAYS search here "
                          "before working out a non-obvious procedure from scratch. mode: hybrid (default, lexical+semantic fused), lexical, or semantic. "
                          "author='<user>' restricts to skills whose LATEST version that "
                          "identity published: one they wrote and someone else has since revised "
                          "leaves their list, though both immutable versions still exist.")
    @_envelope
    def skill_search(query: str = "", tags: Optional[list[str]] = None, limit: int = 20,
                     response_format: str = "concise", mode: str = "hybrid",
                     author: Optional[str] = None) -> dict:
        return skills.search(db, query, tags=tags, limit=limit, format=response_format, mode=mode,
                             author=author)

    @mcp.tool(annotations=RO,
              description="Browse the whole mini-skill library: topics with counts plus a one-line "
                          "entry per skill. Use this to see what exists before inventing a query; "
                          "pass topic= to narrow to one tag.")
    @_envelope
    def skill_catalog(topic: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
        return skills.catalog(db, topic=topic, limit=limit, offset=offset)

    @mcp.tool(annotations=WRITE,
              description="Link a skill to a graph node it is about (relation: about|uses|"
                          "produces). Anyone reading that node then sees the procedure.")
    @_envelope
    def skill_link(skill_id: str, node_id: str, relation: str = "about",
                   note: Optional[str] = None, agent: str = "agent") -> dict:
        return skills.link(db, agent, skill_id, node_id, relation=relation, note=note)

    @mcp.tool(annotations=WRITE,
              description="Remove a skill<->node link. Use when an automatic link is wrong — that "
                          "is the correction path, since linking happens automatically.")
    @_envelope
    def skill_unlink(skill_id: str, node_id: str, relation: str = "about",
                     agent: str = "agent") -> dict:
        return skills.unlink(db, agent, skill_id, node_id, relation)

    @mcp.tool(annotations=WRITE,
              description="Re-run automatic linking for a skill (or omit skill_id to backfill "
                          "every unlinked skill). Happens on publish already; use this after the "
                          "graph has grown new nodes worth linking to.")
    @_envelope
    def skill_autolink(skill_id: Optional[str] = None, agent: str = "agent") -> dict:
        return (skills.autolink(db, skill_id, agent_id=agent) if skill_id
                else skills.autolink_all(db, agent_id=agent))

    @mcp.tool(annotations=RO,
              description="Preview which nodes a skill would be linked to, without linking.")
    @_envelope
    def skill_suggest_links(skill_id: str, limit: int = 5) -> dict:
        return skills.suggest_links(db, skill_id, limit=limit)

    @mcp.tool(annotations=RO,
              description="Fetch a mini-skill's full procedure. constraint accepts ^, ~, >= or an "
                          "exact version; default is the newest non-yanked version.")
    @_envelope
    def skill_get(id: str, constraint: str = "") -> dict:
        return skills.get(db, id, constraint)

    @mcp.tool(annotations=WRITE,
              description="Publish a mini-skill: a repeatable procedure you worked out, so nobody "
                          "re-derives it. Versions are IMMUTABLE — bump the semver to revise. "
                          "Write `body` as steps someone else can follow, include the gotchas, and "
                          "state in `verified_how` how you confirmed it actually works. Publishing a NEW id that looks like an existing skill is refused — revise the existing one by bumping its version instead (force=true to override).")
    @_envelope
    def skill_publish(id: str, version: str, title: str, description: str, body: str,
                      agent: str = "agent", when_to_use: Optional[str] = None,
                      tags: Optional[list[str]] = None, requires: Optional[dict] = None,
                      verified_how: Optional[str] = None, force: bool = False) -> dict:
        return skills.publish(db, agent, id=id, version=version, title=title,
                              description=description, body=body, when_to_use=when_to_use,
                              tags=tags, requires=requires, verified_how=verified_how,
                              force=force)

    @mcp.tool(annotations=WRITE,
              description="Yank a mini-skill version (hide it from search; still fetchable by "
                          "exact pin). Use when a procedure has become wrong or unsafe.")
    @_envelope
    def skill_yank(id: str, version: str, reason: str = "", agent: str = "agent") -> dict:
        return skills.yank(db, agent, id, version, reason)

    # ── traps: recorded dead-ends ─────────────────────────────────────────────────
    @mcp.tool(annotations=RO,
              description="Search recorded dead-ends (approaches that wasted time and why). "
                          "Check this BEFORE starting a non-trivial approach. "
                          "author='<user>' restricts to the traps that identity recorded.")
    @_envelope
    def trap_search(query: str = "", node_id: Optional[str] = None,
                    include_retired: bool = False, limit: int = 20,
                    response_format: str = "concise", author: Optional[str] = None) -> dict:
        return traps.search(db, query, node_id=node_id, include_retired=include_retired,
                            limit=limit, format=response_format, author=author)

    @mcp.tool(annotations=RO, description="Fetch one trap in full.")
    @_envelope
    def trap_get(trap_id: str) -> dict:
        return traps.get(db, trap_id)

    @mcp.tool(annotations=WRITE,
              description="Record a dead-end so nobody repeats it. REQUIRED: what_failed (what you "
                          "actually tried) and symptom (what you actually observed) — a trap "
                          "without both is an opinion. Attach it to a node with node_id, and/or "
                          "scope it with subject_key+subject_version when it is only true for one "
                          "version; omit both for a project-wide trap. Record this WHEN YOU "
                          "ABANDON AN APPROACH, not at the end of the task.")
    @_envelope
    def trap_record(title: str, what_failed: str, symptom: str, agent: str = "agent",
                    root_cause: Optional[str] = None, instead: Optional[str] = None,
                    node_id: Optional[str] = None, subject_key: Optional[str] = None,
                    subject_version: Optional[str] = None, cost_minutes: Optional[int] = None,
                    evidence: Optional[str] = None, verified_how: Optional[str] = None,
                    confidence: str = "medium") -> dict:
        return traps.record(db, agent, title=title, what_failed=what_failed, symptom=symptom,
                            root_cause=root_cause, instead=instead, node_id=node_id,
                            subject_key=subject_key, subject_version=subject_version,
                            cost_minutes=cost_minutes, evidence=evidence,
                            verified_how=verified_how, confidence=confidence)

    @mcp.tool(annotations=WRITE,
              description="Retire or dispute a trap: status='retired' when it no longer applies "
                          "(fixed, or was version-specific), 'disputed' when you have evidence it "
                          "is wrong. Always give a reason — a trap that misleads is worse than no "
                          "trap. Disputed traps stay visible.")
    @_envelope
    def trap_status(trap_id: str, status: str, reason: str = "", agent: str = "agent") -> dict:
        return traps.set_status(db, agent, trap_id, status, reason)

    # ── guide ────────────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO,
              description="Read the live guide. No section = the section index (names + token "
                          "counts). Pass section (e.g. 'core') for its body.")
    @_envelope
    def guide_get(section: Optional[str] = None) -> dict:
        return guide.get_section(db, section) if section else guide.get_index(db)

    @mcp.tool(annotations=WRITE,
              description="Propose a guide edit for human review (the guide is human-gated; this "
                          "does NOT change the live guide). Use for domain knowledge worth sharing.")
    @_envelope
    def guide_propose(section: str, body: str, agent: str = "agent", why: str = "") -> dict:
        return guide.propose_section(db, agent, section, body, why=why)

    from . import registry_tools  # attach artifact + tool-registry tools (added incrementally)
    registry_tools.attach(mcp)
    from . import project_tools    # the project lifecycle: create/list/info/share/unshare
    # On `real`, NOT the proxy: these tools are about projects rather than in one, and the `project`
    # argument they carry themselves would be shadowed by the injected per-call one.
    project_tools.attach(real, registry, identities)
    from . import bus_ws_tools    # agent bus: WebSocket push, deliberately outside the graph
    from .config import config as _config
    bus_ws_tools.attach(mcp, _config())
    from . import chat_tools      # durable DMs, public project rooms and canonical sessions
    chat_tools.attach(mcp, _config(), identities)
    return real                   # mount the real server; the proxy only wraps registration
