"""Traps: recorded dead-ends — approaches that looked reasonable and turned out wrong.

This is episodic memory ("what happened last time someone tried X"), and the literature is blunt
about the failure mode: agents write confident-but-wrong self-reflections, store them, and then
reuse them as fact (memory confabulation). Three design choices push back on that:

  1. Evidence is mandatory in shape — `what_failed` (what was tried) and `symptom` (what was
     actually observed) are required. A trap with neither is an opinion, not an observation.
  2. Traps are scoped — a trap can attach to a node and/or a subject_version, so "true on build A"
     never silently becomes "true everywhere".
  3. Traps are falsifiable — anyone can retire or dispute one with a reason, and disputed traps
     stay visible rather than vanishing.
"""
from __future__ import annotations

from typing import Optional

from .db import Database, Invalid, NotFound
from .ids import ulid

VALID_STATUS = ("active", "retired", "disputed")
VALID_CONFIDENCE = ("low", "medium", "high")


def _index(cur, trap_id: str, *parts: Optional[str]) -> None:
    text = " ".join(p for p in parts if p)
    cur.execute("DELETE FROM trap_fts WHERE trap_id=?", (trap_id,))
    cur.execute("INSERT INTO trap_fts(trap_id, body) VALUES(?,?)", (trap_id, text))


def _row(r) -> dict:
    d = dict(r)
    d.pop("created_tx", None); d.pop("updated_tx", None)
    return d


def record(db: Database, agent_id: str, *, title: str, what_failed: str, symptom: str,
           root_cause: Optional[str] = None, instead: Optional[str] = None,
           node_id: Optional[str] = None, subject_key: Optional[str] = None,
           subject_version: Optional[str] = None, cost_minutes: Optional[int] = None,
           evidence: Optional[str] = None, verified_how: Optional[str] = None,
           confidence: str = "medium") -> dict:
    for field, val in (("title", title), ("what_failed", what_failed), ("symptom", symptom)):
        if not (val or "").strip():
            raise Invalid(
                f"{field} is required — a trap must say what was tried and what was observed, "
                f"otherwise it is an opinion and the next agent cannot judge it")
    if confidence not in VALID_CONFIDENCE:
        raise Invalid(f"confidence must be one of {VALID_CONFIDENCE}")
    tid = ulid()
    with db.write(agent_id, f"trap_record {title[:60]}") as tx:
        cur = tx.cur
        if node_id is not None:
            if cur.execute("SELECT 1 FROM node WHERE node_id=?", (node_id,)).fetchone() is None:
                raise Invalid(f"node {node_id!r} not found — omit node_id for a project-wide trap")
        cur.execute(
            "INSERT INTO trap(trap_id,title,what_failed,symptom,root_cause,instead,node_id,"
            "subject_key,subject_version,cost_minutes,evidence,verified_how,confidence,status,"
            "author,created_tx,updated_tx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
            (tid, title, what_failed, symptom, root_cause, instead, node_id, subject_key,
             subject_version, cost_minutes, evidence, verified_how, confidence, agent_id,
             tx.tx_id, tx.tx_id))
        _index(cur, tid, title, what_failed, symptom, root_cause, instead, subject_key)
    return {"trap_id": tid, "title": title, "scope": "node" if node_id else "project",
            "status": "active"}


def set_status(db: Database, agent_id: str, trap_id: str, status: str,
               reason: str = "") -> dict:
    if status not in VALID_STATUS:
        raise Invalid(f"status must be one of {VALID_STATUS}")
    with db.write(agent_id, f"trap_{status} {trap_id}: {reason}".strip(": ")) as tx:
        n = tx.cur.execute(
            "UPDATE trap SET status=?, status_reason=?, updated_tx=? WHERE trap_id=?",
            (status, reason, tx.tx_id, trap_id)).rowcount
        if n == 0:
            raise NotFound(f"no trap {trap_id!r}")
    return {"trap_id": trap_id, "status": status, "reason": reason}


def for_node(db: Database, node_id: str, *, subject_key: Optional[str] = None,
             subject_version: Optional[str] = None, include_retired: bool = False) -> list:
    """Traps attached to a node, plus project-wide traps scoped to the same subject version."""
    with db.read() as cur:
        q = ("SELECT * FROM trap WHERE (node_id=? OR (node_id IS NULL AND subject_key IS NOT NULL "
             "AND subject_key=? AND (subject_version IS NULL OR subject_version=?)))")
        params = [node_id, subject_key, subject_version]
        if not include_retired:
            q += " AND status != 'retired'"
        rows = cur.execute(q + " ORDER BY created_tx DESC LIMIT 20", params).fetchall()
    return [_row(r) for r in rows]


def search(db: Database, query: str = "", *, node_id: Optional[str] = None,
           include_retired: bool = False, limit: int = 20,
           format: str = "concise") -> dict:
    limit = max(1, min(limit, 100))
    from .search import _fts_query
    with db.read() as cur:
        if query:
            m = _fts_query(query)
            ids = [r["trap_id"] for r in cur.execute(
                "SELECT trap_id FROM trap_fts WHERE trap_fts MATCH ? ORDER BY rank LIMIT ?",
                (m, limit * 3))] if m else []
        else:
            ids = [r["trap_id"] for r in cur.execute(
                "SELECT trap_id FROM trap ORDER BY created_tx DESC LIMIT ?", (limit * 3,))]
        out = []
        for tid in ids:
            r = cur.execute("SELECT * FROM trap WHERE trap_id=?", (tid,)).fetchone()
            if r is None:
                continue
            if not include_retired and r["status"] == "retired":
                continue
            if node_id and r["node_id"] != node_id:
                continue
            d = _row(r)
            if format == "concise":
                d = {"trap_id": d["trap_id"], "title": d["title"], "symptom": d["symptom"],
                     "instead": d["instead"], "status": d["status"],
                     "confidence": d["confidence"],
                     "scope": "node" if d["node_id"] else "project"}
            out.append(d)
            if len(out) >= limit:
                break
    return {"traps": out, "count": len(out),
            "hint": ("call trap_get(trap_id) for the full record" if out else
                     "no known trap here — if you burn time on a dead end, trap_record it")}


def get(db: Database, trap_id: str) -> dict:
    with db.read() as cur:
        r = cur.execute("SELECT * FROM trap WHERE trap_id=?", (trap_id,)).fetchone()
    if r is None:
        raise NotFound(f"no trap {trap_id!r}")
    return _row(r)
