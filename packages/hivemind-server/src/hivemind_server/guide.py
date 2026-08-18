"""Live, sectioned guide (the self-updating-skill content). Agents PROPOSE edits; humans MERGE
(the injection firewall — the guide is the instruction-write path, kept separate from the graph).
Sections are token-budgeted so the injected 'core' stays small.
"""
from __future__ import annotations

from typing import Optional

from .db import Database, Invalid, NotFound
from .ids import ulid

MAX_SECTION_CHARS = 20000  # ~5k tokens


def get_index(db: Database) -> dict:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT name, guide_version, length(body) AS n FROM guide_section ORDER BY name"
        ).fetchall()
    return {"sections": [{"name": r["name"], "guide_version": r["guide_version"],
                          "chars": r["n"], "approx_tokens": r["n"] // 4} for r in rows]}


def get_section(db: Database, name: str) -> dict:
    with db.read() as cur:
        r = cur.execute("SELECT * FROM guide_section WHERE name=?", (name,)).fetchone()
    if r is None:
        raise NotFound(f"no guide section {name!r}")
    return {"name": name, "guide_version": r["guide_version"], "body": r["body"]}


def set_section(db: Database, agent_id: str, name: str, body: str) -> dict:
    """Operator/merge path: write a section directly, bumping its monotonic guide_version."""
    if len(body) > MAX_SECTION_CHARS:
        raise Invalid(f"section {name!r} is {len(body)} chars > budget {MAX_SECTION_CHARS}")
    with db.write(agent_id, f"guide_set {name}") as tx:
        cur = tx.cur
        cur.execute(
            "INSERT INTO guide_section(name,body,guide_version,updated_tx) VALUES(?,?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET body=excluded.body, "
            "guide_version=guide_section.guide_version+1, updated_tx=excluded.updated_tx",
            (name, body, tx.tx_id))
        gv = cur.execute("SELECT guide_version FROM guide_section WHERE name=?",
                         (name,)).fetchone()["guide_version"]
    return {"name": name, "guide_version": gv}


def propose_section(db: Database, agent_id: str, name: str, body: str, why: str = "") -> dict:
    """Agent path: file a proposal for human review. Never mutates the live section."""
    if len(body) > MAX_SECTION_CHARS:
        raise Invalid(f"proposed section too large ({len(body)} chars)")
    pid = ulid()
    with db.write(agent_id, f"guide_propose {name}") as tx:
        tx.cur.execute(
            "INSERT INTO guide_proposal(id,section,body,agent_id,why,status,created_tx) "
            "VALUES(?,?,?,?,?,'proposed',?)", (pid, name, body, agent_id, why, tx.tx_id))
    return {"proposal_id": pid, "section": name, "status": "proposed"}


def list_proposals(db: Database, status: str = "proposed") -> dict:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT id,section,agent_id,why,status FROM guide_proposal WHERE status=? "
            "ORDER BY created_tx DESC", (status,)).fetchall()
    return {"proposals": [dict(r) for r in rows]}


def merge_proposal(db: Database, agent_id: str, proposal_id: str) -> dict:
    with db.write(agent_id, f"guide_merge {proposal_id}") as tx:
        cur = tx.cur
        r = cur.execute("SELECT * FROM guide_proposal WHERE id=?", (proposal_id,)).fetchone()
        if r is None:
            raise NotFound(f"no proposal {proposal_id!r}")
        cur.execute(
            "INSERT INTO guide_section(name,body,guide_version,updated_tx) VALUES(?,?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET body=excluded.body, "
            "guide_version=guide_section.guide_version+1, updated_tx=excluded.updated_tx",
            (r["section"], r["body"], tx.tx_id))
        cur.execute("UPDATE guide_proposal SET status='merged' WHERE id=?", (proposal_id,))
    return {"proposal_id": proposal_id, "section": r["section"], "status": "merged"}
