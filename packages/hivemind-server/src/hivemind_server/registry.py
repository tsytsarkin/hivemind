"""Tool registry: publish/resolve/yank standalone tools. Versions are IMMUTABLE once published;
range strings are rejected as versions; yank (never delete) hides a version from resolution unless
it is the only exact match. Tool bytes live in the blob store (artifact_digest)."""
from __future__ import annotations

import difflib
import json
import re
from typing import Optional

from . import semver
from .db import Conflict, Database, Invalid, NotFound

_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)?$")
_REQUIRED = ("id", "version", "runtime", "entrypoint")
_RUNTIMES = {"python", "node", "binary", "shell", "container"}


def _validate_manifest(manifest: dict) -> None:
    for k in _REQUIRED:
        if not manifest.get(k):
            raise Invalid(f"manifest missing required field {k!r}")
    if not _ID_RE.match(manifest["id"]):
        raise Invalid(f"invalid tool id {manifest['id']!r} (want reverse-dns-ish [a-z0-9._-/])")
    if manifest["runtime"] not in _RUNTIMES:
        raise Invalid(f"runtime must be one of {sorted(_RUNTIMES)}")
    ver = str(manifest["version"])
    if semver.is_range(ver) or semver.parse(ver) is None:
        raise Invalid(f"version {ver!r} must be an exact semver (no ranges)")


def _run_command(manifest: dict) -> str:
    rt, hint, entry = manifest["runtime"], manifest.get("runtime_hint"), manifest["entrypoint"]
    if rt == "python":
        return f"uv run --script {entry}" if hint in (None, "uv") else f"python {entry}"
    if rt == "node":
        return f"npx {entry}" if hint == "npx" else f"node {entry}"
    if rt == "shell":
        return f"sh {entry}"
    return f"./{entry}"


def publish(db: Database, agent_id: str, manifest: dict, artifact_digest: str, *,
            force: bool = False) -> dict:
    _validate_manifest(manifest)
    tid, version = manifest["id"], str(manifest["version"])
    warnings = []
    is_new = False
    with db.write(agent_id, f"tool_publish {tid}@{version}") as tx:
        cur = tx.cur
        b = cur.execute("SELECT 1 FROM blob WHERE digest=?", (artifact_digest,)).fetchone()
        if b is None:
            raise Invalid(f"artifact {artifact_digest} not uploaded; PUT the tool blob first")
        exists = cur.execute("SELECT 1 FROM tool_version WHERE id=? AND version=?",
                             (tid, version)).fetchone()
        if exists:
            raise Conflict(f"{tid}@{version} already published (immutable). Bump the version.")
        is_new = cur.execute("SELECT 1 FROM tool WHERE id=?", (tid,)).fetchone() is None
        if is_new:
            dups = find_similar(db, id=tid, description=manifest.get("description", ""))
            if dups and not force:
                listed = ", ".join(f"{d['id']}@{d['version']} ({d['why']})" for d in dups)
                raise Invalid(
                    f"a similar tool already exists: {listed}. Publish a NEW VERSION of it "
                    f"instead of a duplicate, or re-publish with force=true if this is "
                    f"genuinely different.")
            if dups:
                warnings.append({"similar_tools": dups, "note": "published despite similarity"})
        cur.execute("INSERT INTO tool(id,latest_version,created_tx) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO NOTHING", (tid, version, tx.tx_id))
        cur.execute("INSERT INTO tool_version(id,version,manifest,artifact_digest,created_tx) "
                    "VALUES(?,?,?,?,?)",
                    (tid, version, json.dumps(manifest), artifact_digest, tx.tx_id))
        # recompute latest across non-yanked stable versions
        rows = cur.execute("SELECT version FROM tool_version WHERE id=? AND yanked=0",
                           (tid,)).fetchall()
        latest = semver.latest([r["version"] for r in rows]) or version
        cur.execute("UPDATE tool SET latest_version=? WHERE id=?", (latest, tid))
        if latest == version:
            _index(cur, tid, manifest)
    if latest == version:
        from . import embeddings
        embeddings.upsert(db, "tool", tid, " ".join(filter(None, [
            tid, manifest.get("description", ""), " ".join(manifest.get("tags") or []),
            manifest.get("runtime", "")])), agent_id=agent_id)
        try:
            autolink(db, tid, agent_id=agent_id)
        except Exception:
            pass
    return {"id": tid, "version": version, "latest": latest, "artifact_digest": artifact_digest,
            "new_tool": is_new, "warnings": warnings}


def yank(db: Database, agent_id: str, tid: str, version: str, reason: str = "") -> dict:
    with db.write(agent_id, f"tool_yank {tid}@{version}") as tx:
        cur = tx.cur
        n = cur.execute("UPDATE tool_version SET yanked=1, yanked_reason=? WHERE id=? AND version=?",
                        (reason, tid, version)).rowcount
        if n == 0:
            raise NotFound(f"{tid}@{version} not found")
        rows = cur.execute("SELECT version FROM tool_version WHERE id=? AND yanked=0",
                           (tid,)).fetchall()
        latest = semver.latest([r["version"] for r in rows])
        cur.execute("UPDATE tool SET latest_version=? WHERE id=?", (latest, tid))
        if latest is None:
            cur.execute("DELETE FROM tool_fts WHERE id=?", (tid,))
        else:
            r = cur.execute("SELECT manifest FROM tool_version WHERE id=? AND version=?",
                            (tid, latest)).fetchone()
            _index(cur, tid, json.loads(r["manifest"]))
    return {"id": tid, "version": version, "yanked": True, "latest": latest, "reason": reason}


def resolve(db: Database, tid: str, *, constraint: str = "", os: Optional[str] = None,
            arch: Optional[str] = None, include_prerelease: bool = False) -> dict:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT version, manifest, artifact_digest, yanked, yanked_reason "
            "FROM tool_version WHERE id=?", (tid,)).fetchall()
    if not rows:
        raise NotFound(f"tool {tid!r} not found")
    all_versions = [r["version"] for r in rows]
    live = [r for r in rows if not r["yanked"]]
    matching = [r for r in live if semver.satisfies(r["version"], constraint)]
    # yanked-but-exact-pin still resolvable (PEP 592 semantics)
    if not matching and constraint and not semver.is_range(constraint):
        matching = [r for r in rows if r["version"] == constraint.strip()]
    if not matching:
        raise NotFound(f"no version of {tid} satisfies {constraint!r}")
    # os/arch were accepted and ignored, so a caller could be handed a build that cannot run on
    # its machine. A manifest with no `artifacts` is platform-independent (a PEP 723 script
    # resolves its own deps on the target) and stays eligible.
    if os or arch:
        def _runs_here(row) -> bool:
            arts = (json.loads(row["manifest"]).get("artifacts") or [])
            if not arts:
                return True
            return any((not os or a.get("os") == os) and (not arch or a.get("arch") == arch)
                       for a in arts)
        platform_ok = [r for r in matching if _runs_here(r)]
        if not platform_ok:
            seen = sorted({f"{a.get('os')}/{a.get('arch')}"
                           for r in matching
                           for a in (json.loads(r["manifest"]).get("artifacts") or [])})
            raise NotFound(
                f"no version of {tid} satisfying {constraint!r} builds for "
                f"{os or 'any'}/{arch or 'any'}; available: {seen or ['none declared']}")
        matching = platform_ok
    best = max(matching, key=lambda r: semver._key(r["version"])) if include_prerelease else \
        _pick_stable(matching)
    manifest = json.loads(best["manifest"])
    newer = _newer_incompatible(all_versions, best["version"], constraint)
    href = f"/blobs/{best['artifact_digest'].replace(':', '/', 1)}"
    return {"id": tid, "version": best["version"], "manifest": manifest,
            "requested_platform": (f"{os or 'any'}/{arch or 'any'}" if (os or arch) else None),
            "artifact_digest": best["artifact_digest"], "artifact_url": href,
            "run": _run_command(manifest), "entrypoint": manifest["entrypoint"],
            "yanked": bool(best["yanked"]), "yanked_reason": best["yanked_reason"],
            "newer_incompatible": newer}


def _pick_stable(rows):
    stable = [r for r in rows if semver.parse(r["version"])[3] is None]
    pool = stable or rows
    return max(pool, key=lambda r: semver._key(r["version"]))


def _newer_incompatible(all_versions, chosen: str, constraint: str):
    newer = [v for v in all_versions if semver.compare(v, chosen) > 0
             and not semver.satisfies(v, constraint)]
    if not newer:
        return None
    top = semver.latest(newer, include_prerelease=True)
    return {"version": top, "reason": f"exists but does not satisfy {constraint!r}"}


def search(db: Database, query: str = "", *, os: Optional[str] = None,
           arch: Optional[str] = None, limit: int = 25, mode: str = "hybrid") -> dict:
    """FTS-backed tool search (was a full-table scan with Python-side filtering)."""
    from .search import _fts_query
    limit = max(1, min(limit, 200))
    with db.read() as cur:
        if query:
            m = _fts_query(query)
            lexical = [r["id"] for r in cur.execute(
                "SELECT id FROM tool_fts WHERE tool_fts MATCH ? ORDER BY rank LIMIT ?",
                (m, limit * 3))] if m else []
            semantic = []
            if mode in ("hybrid", "semantic"):
                from . import embeddings
                semantic = [i for i, _ in embeddings.query(db, "tool", query, limit=limit * 3)]
            if mode == "lexical":
                ids = lexical
            elif mode == "semantic":
                ids = semantic or lexical
            else:
                fused = _rrf(lexical, semantic)
                ids = sorted(fused, key=lambda k: fused[k], reverse=True)
        else:
            ids = [r["id"] for r in cur.execute(
                "SELECT id FROM tool ORDER BY created_tx DESC LIMIT ?", (limit * 3,))]
        out = []
        for tid in ids:
            r = cur.execute(
                "SELECT t.latest_version, tv.manifest FROM tool t JOIN tool_version tv "
                "ON tv.id=t.id AND tv.version=t.latest_version WHERE t.id=?", (tid,)).fetchone()
            if r is None:
                continue
            m2 = json.loads(r["manifest"])
            arts = m2.get("artifacts") or []
            if os and arts and not any(a.get("os") == os for a in arts):
                continue
            if arch and arts and not any(a.get("arch") == arch for a in arts):
                continue
            out.append({"id": tid, "latest": r["latest_version"],
                        "description": m2.get("description", ""), "runtime": m2.get("runtime"),
                        "platforms": [f"{a.get('os')}/{a.get('arch')}" for a in arts] or ["any"]})
            if len(out) >= limit:
                break
    from . import embeddings
    warn = embeddings.warning_if_stale(db, "tool") if mode in ("hybrid", "semantic") else None
    return {"tools": out, "count": len(out), "mode": mode,
            "semantic_backend": embeddings.backend_name(),
            **({"semantic_warning": warn} if warn else {}),
            "hint": "tool_resolve(id) for the ready-to-run command" if out else
                    "nothing matched - if you build one, tool_publish it"}


# ── MCP tool attachment (called from registry_tools.attach) ────────────────────────
def attach_tools(mcp, project, envelope, RO, WRITE, base) -> None:
    db = project.db

    @mcp.tool(annotations=WRITE,
              description="Publish an immutable tool version. `manifest` must include id "
                          "(reverse-dns), version (exact semver — no ranges), runtime, entrypoint. "
                          "Upload the tool blob first (PUT /blobs/...) and pass its artifact_digest. SEARCH FIRST (tool_search/tool_catalog): publishing a NEW id that resembles an existing tool is refused - bump that tool's version instead (force=true).")
    @envelope
    def tool_publish(manifest: dict, artifact_digest: str, agent: str = "agent",
                     force: bool = False) -> dict:
        return publish(db, agent, manifest, artifact_digest, force=force)

    @mcp.tool(annotations=WRITE,
              description="Remove a tool<->node link (the correction path for a wrong automatic "
                          "guess).")
    @envelope
    def tool_unlink(tool_id: str, node_id: str, relation: str = "about",
                    agent: str = "agent") -> dict:
        return unlink(db, agent, tool_id, node_id, relation)

    @mcp.tool(annotations=WRITE,
              description="Re-run automatic linking for a tool (or omit tool_id to backfill every "
                          "unlinked tool). Runs on publish already; use this after the graph has "
                          "grown.")
    @envelope
    def tool_autolink(tool_id: Optional[str] = None, agent: str = "agent") -> dict:
        return autolink(db, tool_id, agent_id=agent) if tool_id else autolink_all(db, agent_id=agent)

    @mcp.tool(annotations=RO,
              description="Preview which nodes a tool would be linked to, without linking.")
    @envelope
    def tool_suggest_links(tool_id: str, limit: int = 5) -> dict:
        return suggest_links(db, tool_id, limit=limit)

    @mcp.tool(annotations=RO,
              description="Resolve a tool to a runnable version. Returns the exact version, a "
                          "ready-to-run command, the artifact URL, and a newer_incompatible hint. "
                          "constraint accepts ^, ~, >=, or an exact version.")
    @envelope
    def tool_resolve(id: str, constraint: str = "", os: Optional[str] = None,
                     arch: Optional[str] = None, include_prerelease: bool = False) -> dict:
        return resolve(db, id, constraint=constraint, os=os, arch=arch,
                       include_prerelease=include_prerelease)

    @mcp.tool(annotations=RO,
              description="Browse the whole tool registry: tags with counts plus a one-line entry "
                          "per tool. Use before building anything - check what already exists.")
    @envelope
    def tool_catalog(topic: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
        return catalog(db, topic=topic, limit=limit, offset=offset)

    @mcp.tool(annotations=WRITE,
              description="Link a tool to a graph node it is about (relation: about|analyses|"
                          "produces). Anyone reading that node then sees the tool.")
    @envelope
    def tool_link(tool_id: str, node_id: str, relation: str = "about",
                  note: Optional[str] = None, agent: str = "agent") -> dict:
        return link(db, agent, tool_id, node_id, relation=relation, note=note)

    @mcp.tool(annotations=RO,
              description="List/search published tools (id, latest version, description, "
                          "platforms). Use this to discover what tools other agents have shared.")
    @envelope
    def tool_search(query: str = "", os: Optional[str] = None, arch: Optional[str] = None,
                    limit: int = 25, mode: str = "hybrid") -> dict:
        return search(db, query, os=os, arch=arch, limit=limit, mode=mode)

    @mcp.tool(annotations=WRITE,
              description="Yank a tool version (hide from resolution; still fetchable by exact pin). "
                          "Immutable registries never delete — this just stops new adopters.")
    @envelope
    def tool_yank(id: str, version: str, reason: str = "", agent: str = "agent") -> dict:
        return yank(db, agent, id, version, reason)


# ── discovery: FTS index, duplicate prevention, catalog, graph links ────────────────
_DUP_NAME = 0.72
_DUP_TEXT = 0.62


def _index(cur, tid: str, manifest: dict) -> None:
    text = " ".join(filter(None, [
        tid, manifest.get("description", ""), " ".join(manifest.get("tags") or []),
        manifest.get("runtime", ""), manifest.get("entrypoint", ""),
        " ".join(e.get("cmd", "") for e in (manifest.get("examples") or []))]))
    cur.execute("DELETE FROM tool_fts WHERE id=?", (tid,))
    cur.execute("INSERT INTO tool_fts(id, body) VALUES(?,?)", (tid, text))


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def find_similar(db: Database, *, id: str, description: str = "", limit: int = 5) -> list:
    """Existing tools that resemble the one being published — stops the same utility landing
    twice under two names, which is the failure mode a flat registry actually hits."""
    out = []
    with db.read() as cur:
        rows = cur.execute(
            "SELECT t.id, t.latest_version, tv.manifest FROM tool t "
            "JOIN tool_version tv ON tv.id=t.id AND tv.version=t.latest_version").fetchall()
    for r in rows:
        if r["id"] == id:
            continue
        m = json.loads(r["manifest"])
        name_score = _similar(id, r["id"])
        text_score = _similar(description, m.get("description", ""))
        if name_score >= _DUP_NAME or text_score >= _DUP_TEXT:
            out.append({"id": r["id"], "version": r["latest_version"],
                        "why": "similar name" if name_score >= _DUP_NAME else "similar description",
                        "score": round(max(name_score, text_score), 2)})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]


def catalog(db: Database, *, topic: Optional[str] = None, limit: int = 100, offset: int = 0,
            max_topics: int = 30) -> dict:
    """Advertise the tool registry: real totals, busiest tags, a page of one-line entries.
    Counts come from the table, never from the page."""
    limit = max(1, min(limit, 500))
    with db.read() as cur:
        total = cur.execute(
            "SELECT COUNT(*) c FROM tool WHERE latest_version IS NOT NULL").fetchone()["c"]
        topic_rows = cur.execute(
            "SELECT j.value AS topic, COUNT(*) AS n FROM tool t "
            "JOIN tool_version tv ON tv.id=t.id AND tv.version=t.latest_version, "
            "json_each(json_extract(tv.manifest, '$.tags')) j "
            "GROUP BY j.value ORDER BY n DESC, j.value").fetchall()
        rows = cur.execute(
            "SELECT t.id, t.latest_version, tv.manifest FROM tool t "
            "JOIN tool_version tv ON tv.id=t.id AND tv.version=t.latest_version "
            "WHERE t.latest_version IS NOT NULL ORDER BY t.id").fetchall()
        link_counts = dict(cur.execute(
            "SELECT id, COUNT(*) FROM tool_link GROUP BY id").fetchall() or [])
    entries = []
    for r in rows:
        m = json.loads(r["manifest"])
        if topic and topic not in (m.get("tags") or []):
            continue
        entries.append({"id": r["id"], "version": r["latest_version"],
                        "description": m.get("description", ""), "runtime": m.get("runtime"),
                        "tags": m.get("tags") or [],
                        "platforms": [f"{a.get('os')}/{a.get('arch')}"
                                      for a in (m.get("artifacts") or [])] or ["any"],
                        "linked_nodes": link_counts.get(r["id"], 0)})
    matched = len(entries)
    page = entries[offset:offset + limit]
    out = {"total_tools": total, "matched": matched, "returned": len(page), "offset": offset,
           "next_offset": (offset + len(page)) if offset + len(page) < matched else None,
           "topics": [{"topic": r["topic"], "tools": r["n"]} for r in topic_rows[:max_topics]],
           "total_topics": len(topic_rows), "tools": page,
           "hint": "tool_resolve(id) for the run command; tool_catalog(topic=…) to narrow"}
    if len(topic_rows) > max_topics:
        out["topics_truncated"] = (f"{len(topic_rows)} tags exist; showing the {max_topics} "
                                   f"largest. Prefer tool_search for anything specific.")
    return out


def link(db: Database, agent_id: str, tool_id: str, node_id: str, *, relation: str = "about",
         note: Optional[str] = None, source: str = "confirmed",
         score: Optional[float] = None) -> dict:
    with db.write(agent_id, f"tool_link {tool_id} -> {node_id}") as tx:
        cur = tx.cur
        if cur.execute("SELECT 1 FROM tool WHERE id=?", (tool_id,)).fetchone() is None:
            raise NotFound(f"tool {tool_id!r} not found")
        if cur.execute("SELECT 1 FROM node WHERE node_id=?", (node_id,)).fetchone() is None:
            raise Invalid(f"node {node_id!r} not found")
        cur.execute(
            "INSERT INTO tool_link(id,node_id,relation,source,score,note,created_tx) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id,node_id,relation) DO UPDATE SET "
            "note=COALESCE(excluded.note, tool_link.note), "
            "score=COALESCE(excluded.score, tool_link.score), "
            # a human/agent confirmation upgrades an auto link; it is never downgraded
            "source=CASE WHEN tool_link.source='confirmed' OR excluded.source='confirmed' "
            "THEN 'confirmed' ELSE 'auto' END",
            (tool_id, node_id, relation, source, score, note, tx.tx_id))
    return {"tool_id": tool_id, "node_id": node_id, "relation": relation,
            "source": source}


def for_node(db: Database, node_id: str) -> list:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT tl.id, tl.relation, tl.note, tl.source, tl.score, t.latest_version, tv.manifest FROM tool_link tl "
            "JOIN tool t ON t.id=tl.id LEFT JOIN tool_version tv "
            "ON tv.id=t.id AND tv.version=t.latest_version WHERE tl.node_id=? "
            "ORDER BY tl.created_tx DESC LIMIT 10", (node_id,)).fetchall()
    out = []
    for r in rows:
        m = json.loads(r["manifest"]) if r["manifest"] else {}
        out.append({"id": r["id"], "version": r["latest_version"],
                    "description": m.get("description", ""), "relation": r["relation"],
                    "note": r["note"], "source": r["source"], "score": r["score"]})
    return out


def reindex_all(db: Database) -> int:
    """Backfill tool_fts for tools published before the index existed."""
    with db.write("reindex", "tool_fts_reindex") as tx:
        cur = tx.cur
        cur.execute("DELETE FROM tool_fts")
        rows = cur.execute(
            "SELECT t.id, tv.manifest FROM tool t JOIN tool_version tv "
            "ON tv.id=t.id AND tv.version=t.latest_version").fetchall()
        for r in rows:
            _index(cur, r["id"], json.loads(r["manifest"]))
    return len(rows)


# ── hybrid retrieval: lexical (FTS/BM25) + semantic (embeddings), fused by RRF ───────
_RRF_K = 60


def _rrf(*ranked_lists) -> dict:
    """Reciprocal-rank fusion: robust, tuning-free, and it does not need the two scorers to be
    on comparable scales (BM25 ranks and cosine similarities are not)."""
    scores: dict = {}
    for lst in ranked_lists:
        for i, item_id in enumerate(lst):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (_RRF_K + i)
    return scores


def _link_query_text(db: Database, tool_id: str):
    with db.read() as cur:
        row = cur.execute(
            "SELECT tv.manifest FROM tool t JOIN tool_version tv ON tv.id=t.id "
            "AND tv.version=t.latest_version WHERE t.id=?", (tool_id,)).fetchone()
        if row is None:
            raise NotFound(f"tool {tool_id!r} not found")
        existing = {x["node_id"] for x in cur.execute(
            "SELECT node_id FROM tool_link WHERE id=?", (tool_id,))}
    m = json.loads(row["manifest"])
    text = " ".join(filter(None, [tool_id, m.get("description", ""),
                                  " ".join(m.get("tags") or [])]))
    return text, existing


def suggest_links(db: Database, item_id: str, *, limit: int = 5) -> dict:
    """Propose graph nodes this tool is probably about — suggestions only, never asserted.

    A link is a semantic claim, and a wrong one puts an irrelevant tool in front of every agent
    who reads that node. So this ranks candidates (by running the tool's own text against the
    node index) and leaves confirmation to the caller, mirroring propose->promote for schema and
    the human-gated guide.
    """
    from .search import candidate_nodes
    text, existing = _link_query_text(db, item_id)
    hits = candidate_nodes(db, text, limit=limit * 3)
    out = []
    for h in hits:
        if h["node_id"] in existing:
            continue
        out.append({"node_id": h["node_id"], "node_type": h["node_type"],
                    "subject_key": h.get("subject_key"),
                    "snippet": h["snippet"][:140], "score": h.get("score", 0)})
        if len(out) >= limit:
            break
    return {"tool_id": item_id, "suggestions": out, "count": len(out),
            "already_linked": len(existing),
            "hint": "these are GUESSES - confirm with tool_link(...) only where the match is real"}


# ── automatic linking ────────────────────────────────────────────────────────────────
# Links are created automatically, because a confirmation step that nobody performs is just an
# empty table. The guard against bad links is not a human gate but three cheap constraints:
#   * only the top few candidates per item, and only those close to the best match (relative
#     threshold), so the long tail of weak matches is never linked;
#   * every auto link records source='auto' and its score, so a reader can weigh it;
#   * a confirmed link is never downgraded, and anything can be unlinked.
AUTO_TOP_K = 3
AUTO_REL_SCORE = 0.55        # keep candidates scoring >= 55% of the best hit for this item


def autolink(db: Database, item_id: str, *, agent_id: str = "autolink", top_k: int = AUTO_TOP_K,
             rel_score: float = AUTO_REL_SCORE) -> dict:
    """Link an tool to the nodes it is most likely about. Idempotent; never downgrades a
    confirmed link."""
    sug = suggest_links(db, item_id, limit=max(top_k * 3, 6))["suggestions"]
    if not sug:
        return {"tool_id": item_id, "linked": 0, "considered": 0}
    best = max((s["score"] or 0) for s in sug) or 0.0
    keep = [s for s in sug if best <= 0 or (s["score"] or 0) >= best * rel_score][:top_k]
    linked = []
    for s in keep:
        link(db, agent_id, item_id, s["node_id"], relation="about", source="auto",
             score=s["score"])
        linked.append(s["node_id"])
    return {"tool_id": item_id, "linked": len(linked), "considered": len(sug),
            "node_ids": linked}


def autolink_all(db: Database, *, agent_id: str = "autolink", top_k: int = AUTO_TOP_K) -> dict:
    """Backfill: autolink every item that has no links yet. Safe to re-run."""
    with db.read() as cur:
        ids = [r[0] for r in cur.execute(
            "SELECT t.id FROM tool t LEFT JOIN tool_link l ON l.id=t.id "
            "WHERE t.latest_version IS NOT NULL GROUP BY t.id HAVING COUNT(l.node_id)=0")]
    total = 0
    for i in ids:
        try:
            total += autolink(db, i, agent_id=agent_id, top_k=top_k)["linked"]
        except Exception:
            continue                      # one bad item must not abort a backfill
    return {"items_processed": len(ids), "links_created": total}


def unlink(db: Database, agent_id: str, item_id: str, node_id: str,
           relation: str = "about") -> dict:
    """Remove a link — the correction path for an automatic guess that is wrong."""
    with db.write(agent_id, f"unlink tool {item_id} -/-> {node_id}") as tx:
        n = tx.cur.execute(
            "DELETE FROM tool_link WHERE id=? AND node_id=? AND relation=?",
            (item_id, node_id, relation)).rowcount
    return {"tool_id": item_id, "node_id": node_id, "removed": bool(n)}
