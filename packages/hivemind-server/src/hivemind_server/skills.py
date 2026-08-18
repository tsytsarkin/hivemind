"""Mini-skill registry: procedures agents write down so the fleet stops re-deriving them.

Versioning mirrors the tool registry deliberately — a published version is immutable, you
supersede by publishing a new semver, and you retire with a yank (never a delete). The difference
is the payload: a tool ships executable bytes, a skill ships a procedure in prose.
"""
from __future__ import annotations

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
            requires: Optional[dict] = None, verified_how: Optional[str] = None) -> dict:
    _validate(id, version, title, description, body)
    tags = tags or []
    with db.write(agent_id, f"skill_publish {id}@{version}") as tx:
        cur = tx.cur
        if cur.execute("SELECT 1 FROM skill_version WHERE id=? AND version=?",
                       (id, version)).fetchone():
            raise Conflict(f"{id}@{version} already published (immutable). Bump the version.")
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
    return {"id": id, "version": version, "latest": latest}


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
           limit: int = 20, format: str = "concise") -> dict:
    limit = max(1, min(limit, 100))
    from .search import _fts_query
    with db.read() as cur:
        if query:
            m = _fts_query(query)
            ids = [r["id"] for r in cur.execute(
                "SELECT id FROM skill_fts WHERE skill_fts MATCH ? ORDER BY rank LIMIT ?",
                (m, limit * 3))] if m else []
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
    return {"skills": out, "count": len(out),
            "hint": "call skill_get(id) for the full procedure" if out else
                    "nothing matched — if you solve this, skill_publish it"}
