"""Mini-skill registry: procedures agents write down so the fleet stops re-deriving them.

Versioning mirrors the tool registry deliberately — a published version is immutable, you
supersede by publishing a new semver, and you retire with a yank (never a delete). The difference
is the payload: a tool ships executable bytes, a skill ships a procedure in prose.
"""
from __future__ import annotations

import difflib
import json
from typing import Optional

from . import semver
from .db import Conflict, Database, Invalid, NotFound

MAX_BODY_CHARS = 20000          # ~5k tokens: a mini-skill, not a manual
_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_./")


def _validate(id: str, version: str, title: str, description: str, body: str) -> None:
    if not id or not set(id) <= _ID_OK:
        raise Invalid(f"invalid skill id {id!r} (lowercase, [a-z0-9-_./])")
    if semver.is_range(version) or semver.parse(version) is None:
        raise Invalid(f"version {version!r} must be an exact semver (no ranges)")
    for field, val in (("title", title), ("description", description), ("body", body)):
        if not (val or "").strip():
            raise Invalid(f"skill {field} is required")
    if len(body) > MAX_BODY_CHARS:
        raise Invalid(f"body is {len(body)} chars > {MAX_BODY_CHARS}; keep a mini-skill small "
                      f"and link to detail rather than inlining it")


def _index(cur, sid: str, title: str, description: str, when_to_use: str, body: str,
           tags: list) -> None:
    text = " ".join(filter(None, [sid, title, description, when_to_use or "", body,
                                  " ".join(tags or [])]))
    cur.execute("DELETE FROM skill_fts WHERE id=?", (sid,))
    cur.execute("INSERT INTO skill_fts(id, body) VALUES(?,?)", (sid, text))


def publish(db: Database, agent_id: str, *, id: str, version: str, title: str, description: str,
            body: str, when_to_use: Optional[str] = None, tags: Optional[list] = None,
            requires: Optional[dict] = None, verified_how: Optional[str] = None,
            force: bool = False) -> dict:
    _validate(id, version, title, description, body)
    tags = tags or []
    warnings = []
    is_new = False
    with db.write(agent_id, f"skill_publish {id}@{version}") as tx:
        cur = tx.cur
        if cur.execute("SELECT 1 FROM skill_version WHERE id=? AND version=?",
                       (id, version)).fetchone():
            raise Conflict(f"{id}@{version} already published (immutable). Bump the version.")
        is_new = cur.execute("SELECT 1 FROM skill WHERE id=?", (id,)).fetchone() is None
        if is_new:
            dups = find_similar(db, id=id, title=title, description=description)
            if dups and not force:
                listed = ", ".join(f"{d['id']}@{d['version']} ({d['why']})" for d in dups)
                raise Invalid(
                    f"a similar skill already exists: {listed}. Publish a NEW VERSION of it "
                    f"instead of a duplicate (skill_publish with that id and a bumped semver), "
                    f"or re-publish with force=true if this is genuinely different.")
            if dups:
                warnings.append({"similar_skills": dups, "note": "published despite similarity"})
        cur.execute("INSERT INTO skill(id, latest_version, created_tx) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO NOTHING", (id, version, tx.tx_id))
        cur.execute(
            "INSERT INTO skill_version(id,version,title,description,when_to_use,body,tags,"
            "requires,verified_how,author,created_tx) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (id, version, title, description, when_to_use, body, json.dumps(tags),
             json.dumps(requires or {}), verified_how, agent_id, tx.tx_id))
        rows = cur.execute("SELECT version FROM skill_version WHERE id=? AND yanked=0",
                           (id,)).fetchall()
        latest = semver.latest([r["version"] for r in rows]) or version
        cur.execute("UPDATE skill SET latest_version=? WHERE id=?", (latest, id))
        if latest == version:                       # only the newest version drives search
            _index(cur, id, title, description, when_to_use or "", body, tags)
    if latest == version:
        from . import embeddings
        embeddings.upsert(db, "skill", id,
                          " ".join([id, title, description, when_to_use or "", body,
                                    " ".join(tags)]), agent_id=agent_id)
    return {"id": id, "version": version, "latest": latest, "new_skill": is_new,
            "warnings": warnings}


def yank(db: Database, agent_id: str, id: str, version: str, reason: str = "") -> dict:
    with db.write(agent_id, f"skill_yank {id}@{version}") as tx:
        cur = tx.cur
        n = cur.execute("UPDATE skill_version SET yanked=1, yanked_reason=? "
                        "WHERE id=? AND version=?", (reason, id, version)).rowcount
        if n == 0:
            raise NotFound(f"{id}@{version} not found")
        rows = cur.execute("SELECT version FROM skill_version WHERE id=? AND yanked=0",
                           (id,)).fetchall()
        latest = semver.latest([r["version"] for r in rows])
        cur.execute("UPDATE skill SET latest_version=? WHERE id=?", (latest, id))
        if latest is None:
            cur.execute("DELETE FROM skill_fts WHERE id=?", (id,))
        else:
            r = cur.execute("SELECT * FROM skill_version WHERE id=? AND version=?",
                            (id, latest)).fetchone()
            _index(cur, id, r["title"], r["description"], r["when_to_use"] or "", r["body"],
                   json.loads(r["tags"]))
    return {"id": id, "version": version, "yanked": True, "latest": latest, "reason": reason}


def get(db: Database, id: str, constraint: str = "") -> dict:
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM skill_version WHERE id=?", (id,)).fetchall()
    if not rows:
        raise NotFound(f"skill {id!r} not found")
    live = [r for r in rows if not r["yanked"]]
    match = [r for r in live if semver.satisfies(r["version"], constraint)]
    if not match and constraint and not semver.is_range(constraint):
        match = [r for r in rows if r["version"] == constraint.strip()]   # exact pin wins
    if not match:
        raise NotFound(f"no version of skill {id} satisfies {constraint!r}")
    best = max(match, key=lambda r: semver._key(r["version"]))
    return {"id": id, "version": best["version"], "title": best["title"],
            "description": best["description"], "when_to_use": best["when_to_use"],
            "body": best["body"], "tags": json.loads(best["tags"]),
            "requires": json.loads(best["requires"]), "verified_how": best["verified_how"],
            "author": best["author"], "yanked": bool(best["yanked"]),
            "yanked_reason": best["yanked_reason"]}


def search(db: Database, query: str = "", *, tags: Optional[list] = None,
           limit: int = 20, format: str = "concise", mode: str = "hybrid") -> dict:
    limit = max(1, min(limit, 100))
    from .search import _fts_query
    with db.read() as cur:
        if query:
            m = _fts_query(query)
            lexical = [r["id"] for r in cur.execute(
                "SELECT id FROM skill_fts WHERE skill_fts MATCH ? ORDER BY rank LIMIT ?",
                (m, limit * 3))] if m else []
            semantic = []
            if mode in ("hybrid", "semantic"):
                from . import embeddings
                semantic = [i for i, _ in embeddings.query(db, "skill", query, limit=limit * 3)]
            if mode == "lexical":
                ids = lexical
            elif mode == "semantic":
                ids = semantic or lexical
            else:
                fused = _rrf(lexical, semantic)
                ids = sorted(fused, key=lambda k: fused[k], reverse=True)
        else:
            ids = [r["id"] for r in cur.execute(
                "SELECT id FROM skill ORDER BY created_tx DESC LIMIT ?", (limit * 3,))]
        out = []
        for sid in ids:
            s = cur.execute("SELECT latest_version FROM skill WHERE id=?", (sid,)).fetchone()
            if not s or not s["latest_version"]:
                continue
            r = cur.execute("SELECT * FROM skill_version WHERE id=? AND version=?",
                            (sid, s["latest_version"])).fetchone()
            if r is None:
                continue
            t = json.loads(r["tags"])
            if tags and not set(tags) & set(t):
                continue
            if format == "concise":
                out.append({"id": sid, "version": r["version"], "title": r["title"],
                            "description": r["description"], "tags": t})
            else:
                out.append({"id": sid, "version": r["version"], "title": r["title"],
                            "description": r["description"], "when_to_use": r["when_to_use"],
                            "tags": t, "requires": json.loads(r["requires"]),
                            "verified_how": r["verified_how"], "author": r["author"]})
            if len(out) >= limit:
                break
    from . import embeddings
    warn = embeddings.warning_if_stale(db, "skill") if mode in ("hybrid", "semantic") else None
    return {"skills": out, "count": len(out), "mode": mode,
            "semantic_backend": embeddings.backend_name(),
            **({"semantic_warning": warn} if warn else {}),
            "hint": "call skill_get(id) for the full procedure" if out else
                    "nothing matched — if you solve this, skill_publish it"}


# ── duplicate prevention ────────────────────────────────────────────────────────────
_DUP_NAME = 0.72          # id/title similarity above this is suspicious
_DUP_TEXT = 0.62          # description similarity above this is suspicious


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def find_similar(db: Database, *, id: str, title: str = "", description: str = "",
                 limit: int = 5) -> list:
    """Existing skills that look like the one being published. Cheap and deterministic: name and
    description similarity, no embeddings — enough to stop the same procedure being written twice
    under different names, which is the actual failure mode in a small library."""
    out = []
    with db.read() as cur:
        rows = cur.execute(
            "SELECT s.id, sv.title, sv.description, s.latest_version FROM skill s "
            "JOIN skill_version sv ON sv.id=s.id AND sv.version=s.latest_version").fetchall()
    for r in rows:
        if r["id"] == id:
            continue
        name_score = max(_similar(id, r["id"]), _similar(title, r["title"]))
        text_score = _similar(description, r["description"])
        if name_score >= _DUP_NAME or text_score >= _DUP_TEXT:
            out.append({"id": r["id"], "version": r["latest_version"], "title": r["title"],
                        "why": ("similar name" if name_score >= _DUP_NAME
                                else "similar description"),
                        "score": round(max(name_score, text_score), 2)})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]


# ── catalog: what is in here, so agents can browse rather than guess a query ──────────
def catalog(db: Database, *, topic: Optional[str] = None, limit: int = 100,
            offset: int = 0, max_topics: int = 30) -> dict:
    """Advertise the library: real totals, the busiest topics, and a page of one-line entries.

    Counts come from the whole table, never from the page — a catalog that reports the size of
    its own LIMIT is worse than no catalog, because it silently understates the library.
    """
    limit = max(1, min(limit, 500))
    with db.read() as cur:
        total = cur.execute(
            "SELECT COUNT(*) c FROM skill WHERE latest_version IS NOT NULL").fetchone()["c"]
        topic_rows = cur.execute(
            "SELECT j.value AS topic, COUNT(*) AS n FROM skill s "
            "JOIN skill_version sv ON sv.id=s.id AND sv.version=s.latest_version, "
            "json_each(sv.tags) j GROUP BY j.value ORDER BY n DESC, j.value").fetchall()
        params: list = []
        where = "WHERE s.latest_version IS NOT NULL"
        if topic:
            where += (" AND EXISTS (SELECT 1 FROM json_each(sv.tags) j2 WHERE j2.value = ?)")
            params.append(topic)
        matched = cur.execute(
            f"SELECT COUNT(*) c FROM skill s JOIN skill_version sv "
            f"ON sv.id=s.id AND sv.version=s.latest_version {where}", params).fetchone()["c"]
        rows = cur.execute(
            f"SELECT s.id, s.latest_version, sv.title, sv.description, sv.tags "
            f"FROM skill s JOIN skill_version sv ON sv.id=s.id AND sv.version=s.latest_version "
            f"{where} ORDER BY s.id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        link_counts = dict(cur.execute(
            "SELECT id, COUNT(*) FROM skill_link GROUP BY id").fetchall() or [])
    entries = [{"id": r["id"], "version": r["latest_version"], "title": r["title"],
                "description": r["description"], "tags": json.loads(r["tags"]),
                "linked_nodes": link_counts.get(r["id"], 0)} for r in rows]
    out = {
        "total_skills": total,
        "matched": matched,
        "returned": len(entries),
        "offset": offset,
        "next_offset": (offset + len(entries)) if offset + len(entries) < matched else None,
        "topics": [{"topic": r["topic"], "skills": r["n"]} for r in topic_rows[:max_topics]],
        "total_topics": len(topic_rows),
        "skills": entries,
    }
    if len(topic_rows) > max_topics:
        out["topics_truncated"] = (
            f"{len(topic_rows)} topics exist; showing the {max_topics} largest. "
            f"With more tags than skills, prefer skill_search over browsing by topic.")
    out["hint"] = "skill_get(id) for the procedure; skill_catalog(topic=…) or skill_search(query)"
    return out


# ── linking skills to the graph ──────────────────────────────────────────────────────
def link(db: Database, agent_id: str, skill_id: str, node_id: str, *, relation: str = "about",
         note: Optional[str] = None) -> dict:
    with db.write(agent_id, f"skill_link {skill_id} -> {node_id}") as tx:
        cur = tx.cur
        if cur.execute("SELECT 1 FROM skill WHERE id=?", (skill_id,)).fetchone() is None:
            raise NotFound(f"skill {skill_id!r} not found")
        if cur.execute("SELECT 1 FROM node WHERE node_id=?", (node_id,)).fetchone() is None:
            raise Invalid(f"node {node_id!r} not found")
        cur.execute("INSERT INTO skill_link(id,node_id,relation,note,created_tx) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(id,node_id,relation) DO UPDATE SET note=?",
                    (skill_id, node_id, relation, note, tx.tx_id, note))
    return {"skill_id": skill_id, "node_id": node_id, "relation": relation}


def for_node(db: Database, node_id: str) -> list:
    """Skills attached to a node — so an agent reading a thing sees the procedures about it."""
    with db.read() as cur:
        rows = cur.execute(
            "SELECT sl.id, sl.relation, sl.note, s.latest_version, sv.title, sv.description "
            "FROM skill_link sl JOIN skill s ON s.id=sl.id "
            "LEFT JOIN skill_version sv ON sv.id=s.id AND sv.version=s.latest_version "
            "WHERE sl.node_id=? ORDER BY sl.created_tx DESC LIMIT 10", (node_id,)).fetchall()
    return [{"id": r["id"], "version": r["latest_version"], "title": r["title"],
             "description": r["description"], "relation": r["relation"], "note": r["note"]}
            for r in rows]


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


def _link_query_text(db: Database, skill_id: str):
    with db.read() as cur:
        r = cur.execute(
            "SELECT sv.title, sv.description, sv.when_to_use, sv.tags FROM skill s "
            "JOIN skill_version sv ON sv.id=s.id AND sv.version=s.latest_version "
            "WHERE s.id=?", (skill_id,)).fetchone()
        if r is None:
            raise NotFound(f"skill {skill_id!r} not found")
        existing = {x["node_id"] for x in cur.execute(
            "SELECT node_id FROM skill_link WHERE id=?", (skill_id,))}
    text = " ".join(filter(None, [r["title"], r["description"], r["when_to_use"] or "",
                                  " ".join(json.loads(r["tags"]))]))
    return text, existing


def suggest_links(db: Database, item_id: str, *, limit: int = 5) -> dict:
    """Propose graph nodes this skill is probably about — suggestions only, never asserted.

    A link is a semantic claim, and a wrong one puts an irrelevant skill in front of every agent
    who reads that node. So this ranks candidates (by running the skill's own text against the
    node index) and leaves confirmation to the caller, mirroring propose->promote for schema and
    the human-gated guide.
    """
    from .search import search as node_search
    text, existing = _link_query_text(db, item_id)
    hits = node_search(db, text, limit=limit * 3)["results"]
    out = []
    for h in hits:
        if h["node_id"] in existing:
            continue
        out.append({"node_id": h["node_id"], "node_type": h["node_type"],
                    "subject_key": h.get("subject_key"),
                    "snippet": h["snippet"][:140], "score": h.get("score", 0)})
        if len(out) >= limit:
            break
    return {"skill_id": item_id, "suggestions": out, "count": len(out),
            "already_linked": len(existing),
            "hint": "these are GUESSES - confirm with skill_link(...) only where the match is real"}
