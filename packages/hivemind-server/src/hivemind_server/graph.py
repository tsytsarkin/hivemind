"""Core graph operations: two-axis versioning, generic typed edges, traversal.

Axis 1 (revision / supersession): node_version.prev_version chain + single open head.
Axis 2 (subject-version): node rows grouped by subject_key, ordered by subject_order.
Edges are fully schema-defined; the engine enforces only generic traits (schemas.edge_traits).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .db import SENTINEL, Conflict, Database, Invalid, NotFound, Tx, canonical_json
from .ids import content_hash, ulid
from . import schemas


# ── helpers ──────────────────────────────────────────────────────────────────────
def _node_row(cur, node_id: str) -> Optional[dict]:
    r = cur.execute("SELECT * FROM node WHERE node_id=?", (node_id,)).fetchone()
    return dict(r) if r else None


def _resolve_redirect(cur, node_id: str) -> str:
    """Follow redirect_to tombstones (cross-identity merges) to the live node."""
    seen = set()
    while True:
        r = cur.execute("SELECT redirect_to FROM node WHERE node_id=?", (node_id,)).fetchone()
        if r is None:
            raise NotFound(f"node {node_id!r} not found")
        if r["redirect_to"] is None:
            return node_id
        if node_id in seen:
            raise Invalid("redirect cycle")
        seen.add(node_id)
        node_id = r["redirect_to"]


def _current_node_version(cur, node_id: str) -> Optional[dict]:
    r = cur.execute(
        "SELECT * FROM node_version WHERE node_id=? AND tx_to=? ", (node_id, SENTINEL)
    ).fetchone()
    return dict(r) if r else None


def _resolve_as_of_tx(cur, as_of: Any) -> int:
    """as_of may be an int tx_id or an ISO timestamp string; return the tx boundary."""
    if isinstance(as_of, int):
        return as_of
    r = cur.execute("SELECT MAX(tx_id) AS t FROM tx WHERE tx_time<=?", (str(as_of),)).fetchone()
    return r["t"] if r and r["t"] is not None else 0


def _assertive_edge_types(cur) -> list[str]:
    rows = cur.execute(
        "SELECT DISTINCT name FROM edge_type WHERE assertive=1 AND status IN ('active','proposed')"
    ).fetchall()
    return [r["name"] for r in rows]


def node_flags(cur, node_id: str) -> dict:
    """Surface generic 'assertive' state: is this node an endpoint of an OPEN assertive edge?"""
    types = _assertive_edge_types(cur)
    if not types:
        return {}
    ph = ",".join("?" * len(types))
    # versioned assertive edges (current heads)
    q_ver = (
        f"SELECT 1 FROM edge e JOIN edge_version ev ON ev.edge_id=e.edge_id AND ev.tx_to=? "
        f"WHERE e.edge_type IN ({ph}) AND (e.src_node_id=? OR e.dst_node_id=?) "
        f"AND COALESCE(json_extract(ev.props,'$.status'),'open')='open' LIMIT 1"
    )
    hit = cur.execute(q_ver, (SENTINEL, *types, node_id, node_id)).fetchone()
    if hit:
        return {"disputed": True}
    q_bulk = (
        f"SELECT 1 FROM edge_bulk WHERE edge_type IN ({ph}) "
        f"AND (src_node_id=? OR dst_node_id=?) "
        f"AND COALESCE(json_extract(props,'$.status'),'open')='open' LIMIT 1"
    )
    hit = cur.execute(q_bulk, (*types, node_id, node_id)).fetchone()
    return {"disputed": True} if hit else {}


def _version_public(row: dict) -> dict:
    return {
        "version_id": row["version_id"],
        "seq": row["seq"],
        "prev_version": row["prev_version"],
        "props": json.loads(row["props"]),
        "schema_ver": row["schema_ver"],
        "content_hash": row["content_hash"],
        "tx_from": row["tx_from"],
        "tx_to": None if row["tx_to"] == SENTINEL else row["tx_to"],
        "retracted": bool(row["retracted"]),
    }


# ── node upsert (chooses axis by subject identity) ─────────────────────────────────
def upsert_node(db: Database, agent_id: str, node_type: str, props: dict, *,
                subject_key: Optional[str] = None, subject_version: Optional[str] = None,
                subject_order: Optional[str] = None, node_id: Optional[str] = None,
                expected_head: Optional[str] = None, reason: Optional[str] = None) -> dict:
    if (subject_key is None) != (subject_version is None):
        raise Invalid("subject_key and subject_version must be given together")
    with db.write(agent_id, reason) as tx:
        cur = tx.cur
        schema_ver = schemas.validate_props(cur, "node", node_type, props)

        target = None
        if node_id is not None:
            live = _resolve_redirect(cur, node_id)
            target = _node_row(cur, live)
        elif subject_key is not None:
            r = cur.execute(
                "SELECT * FROM node WHERE subject_key=? AND subject_version=?",
                (subject_key, subject_version),
            ).fetchone()
            target = dict(r) if r else None

        ch = content_hash(props)

        if target is None:
            # ── subject axis: brand-new node / new subject cell ──
            if expected_head is not None:
                raise Conflict("expected_head given but no existing node to supersede")
            nid = node_id or ulid()
            cur.execute(
                "INSERT INTO node(node_id,node_type,subject_key,subject_version,subject_order,"
                "created_tx) VALUES(?,?,?,?,?,?)",
                (nid, node_type, subject_key, subject_version, subject_order, tx.tx_id),
            )
            vid = ulid()
            cur.execute(
                "INSERT INTO node_version(version_id,node_id,seq,prev_version,props,schema_ver,"
                "content_hash,tx_from,tx_to) VALUES(?,?,?,?,?,?,?,?,?)",
                (vid, nid, 1, None, canonical_json(props), schema_ver, ch, tx.tx_id, SENTINEL),
            )
            from . import search as _search
            _search.index_node(cur, nid, props)
            return {"node_id": nid, "version_id": vid, "seq": 1, "created": True,
                    "superseded": False, "axis": "subject"}

        # ── revision axis: supersede the existing cell's head ──
        nid = target["node_id"]
        if target["node_type"] != node_type:
            raise Invalid(
                f"node {nid} is type {target['node_type']!r}, not {node_type!r}"
            )
        head = _current_node_version(cur, nid)
        if head is None:
            raise Invalid(f"node {nid} has no current version (corrupt)")
        if expected_head is not None and head["version_id"] != expected_head:
            raise Conflict(
                f"stale write: head is {head['version_id']}, you sent {expected_head}"
            )
        if head["content_hash"] == ch:
            return {"node_id": nid, "version_id": head["version_id"], "seq": head["seq"],
                    "created": False, "superseded": False, "noop": True, "axis": "revision"}
        cur.execute(
            "UPDATE node_version SET tx_to=? WHERE node_id=? AND tx_to=?",
            (tx.tx_id, nid, SENTINEL),
        )
        vid = ulid()
        seq = head["seq"] + 1
        cur.execute(
            "INSERT INTO node_version(version_id,node_id,seq,prev_version,props,schema_ver,"
            "content_hash,tx_from,tx_to) VALUES(?,?,?,?,?,?,?,?,?)",
            (vid, nid, seq, head["version_id"], canonical_json(props), schema_ver, ch,
             tx.tx_id, SENTINEL),
        )
        if subject_order is not None:
            cur.execute("UPDATE node SET subject_order=? WHERE node_id=?", (subject_order, nid))
        from . import search as _search
        _search.index_node(cur, nid, props)
        return {"node_id": nid, "version_id": vid, "seq": seq, "created": False,
                "superseded": True, "axis": "revision"}


# ── node reads ─────────────────────────────────────────────────────────────────────
def get_node(db: Database, *, node_id: Optional[str] = None, subject_key: Optional[str] = None,
             subject_version: Optional[str] = None, as_of: Any = None,
             history: bool = False) -> dict:
    with db.read() as cur:
        if node_id is None:
            if subject_key is None or subject_version is None:
                raise Invalid("give node_id, or (subject_key + subject_version)")
            r = cur.execute(
                "SELECT node_id FROM node WHERE subject_key=? AND subject_version=?",
                (subject_key, subject_version),
            ).fetchone()
            if r is None:
                raise NotFound(f"no node for subject {subject_key!r}@{subject_version!r}")
            node_id = r["node_id"]
        node_id = _resolve_redirect(cur, node_id)
        nrow = _node_row(cur, node_id)
        out = {"node_id": node_id, "node_type": nrow["node_type"],
               "subject_key": nrow["subject_key"], "subject_version": nrow["subject_version"],
               "subject_order": nrow["subject_order"], "flags": node_flags(cur, node_id)}

        if history:
            rows = cur.execute(
                "WITH RECURSIVE chain(version_id,prev_version,seq) AS ("
                "  SELECT version_id,prev_version,seq FROM node_version "
                "   WHERE node_id=? AND tx_to=? "
                "  UNION ALL "
                "  SELECT p.version_id,p.prev_version,p.seq FROM node_version p "
                "    JOIN chain c ON p.version_id=c.prev_version) "
                "SELECT nv.* FROM chain c JOIN node_version nv USING(version_id) "
                "ORDER BY nv.seq DESC",
                (node_id, SENTINEL),
            ).fetchall()
            out["history"] = [_version_public(dict(r)) for r in rows]
            out["current"] = out["history"][0] if out["history"] else None
            return out

        if as_of is not None:
            t = _resolve_as_of_tx(cur, as_of)
            r = cur.execute(
                "SELECT * FROM node_version WHERE node_id=? AND tx_from<=? AND tx_to>? ",
                (node_id, t, t),
            ).fetchone()
            out["current"] = _version_public(dict(r)) if r else None
            out["as_of_tx"] = t
            return out

        head = _current_node_version(cur, node_id)
        out["current"] = _version_public(head) if head else None
        return out


def list_subjects(db: Database, subject_key: str, *, as_of_subject: Optional[str] = None) -> dict:
    """List all subject-version cells of a thing (ordered), or resolve newest at/before target."""
    with db.read() as cur:
        rows = cur.execute(
            "SELECT n.node_id,n.subject_version,n.subject_order,nv.version_id,nv.props "
            "FROM node n JOIN node_version nv ON nv.node_id=n.node_id AND nv.tx_to=? "
            "WHERE n.subject_key=? AND n.redirect_to IS NULL "
            "ORDER BY n.subject_order IS NULL, n.subject_order, n.subject_version",
            (SENTINEL, subject_key),
        ).fetchall()
        cells = [{"node_id": r["node_id"], "subject_version": r["subject_version"],
                  "subject_order": r["subject_order"], "version_id": r["version_id"]}
                 for r in rows]
        if as_of_subject is None:
            return {"subject_key": subject_key, "cells": cells}
        # newest cell whose subject_order (fallback subject_version) <= target
        elig = [c for c in cells
                if (c["subject_order"] or c["subject_version"] or "") <= as_of_subject]
        chosen = elig[-1] if elig else None
        return {"subject_key": subject_key, "as_of_subject": as_of_subject, "resolved": chosen}


# ── edges ──────────────────────────────────────────────────────────────────────────
def _check_endpoint_types(cur, traits: dict, src_type: str, dst_type: str) -> None:
    src_ok = "*" in traits["src_types"] or src_type in traits["src_types"]
    dst_ok = "*" in traits["dst_types"] or dst_type in traits["dst_types"]
    if not src_ok:
        raise Invalid(f"src node type {src_type!r} not allowed (want {traits['src_types']})")
    if not dst_ok:
        raise Invalid(f"dst node type {dst_type!r} not allowed (want {traits['dst_types']})")


def _reaches(cur, edge_type: str, start: str, goal: str, versioned: bool, cap: int = 20000) -> bool:
    """Directed reachability over current edges of one type (for acyclic guard)."""
    stack = [start]
    seen = set()
    while stack and len(seen) < cap:
        n = stack.pop()
        if n == goal:
            return True
        if n in seen:
            continue
        seen.add(n)
        if versioned:
            rows = cur.execute(
                "SELECT e.dst_node_id d FROM edge e JOIN edge_version ev "
                "ON ev.edge_id=e.edge_id AND ev.tx_to=? WHERE e.edge_type=? AND e.src_node_id=?",
                (SENTINEL, edge_type, n),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT dst_node_id d FROM edge_bulk WHERE edge_type=? AND src_node_id=?",
                (edge_type, n),
            ).fetchall()
        stack.extend(r["d"] for r in rows)
    return False


def upsert_edge(db: Database, agent_id: str, edge_type: str, src_node_id: str, dst_node_id: str,
                props: Optional[dict] = None, *, expected_head: Optional[str] = None,
                source_tag: Optional[str] = None, reason: Optional[str] = None) -> dict:
    props = props or {}
    with db.write(agent_id, reason) as tx:
        cur = tx.cur
        traits = schemas.edge_traits(cur, edge_type)
        schema_ver = schemas.validate_props(cur, "edge", edge_type, props)

        src = _resolve_redirect(cur, src_node_id)
        dst = _resolve_redirect(cur, dst_node_id)
        srow, drow = _node_row(cur, src), _node_row(cur, dst)

        # symmetric edges: canonicalize orientation so (a,b)==(b,a)
        if traits["symmetric"] and src > dst:
            src, dst = dst, src
            srow, drow = drow, srow
        _check_endpoint_types(cur, traits, srow["node_type"], drow["node_type"])

        if traits["acyclic"]:
            if src == dst or _reaches(cur, edge_type, dst, src, traits["versioned"]):
                raise Invalid(f"edge {src}->{dst} of type {edge_type!r} would create a cycle")

        ch = content_hash(props)

        if not traits["versioned"]:
            # ── bulk edge: no per-edge history; identity incl. source_tag ──
            if source_tag is None:
                raise Invalid(f"edge type {edge_type!r} is bulk (versioned=0); source_tag required")
            cur.execute(
                "INSERT INTO edge_bulk(edge_type,src_node_id,dst_node_id,props,source_tag,created_tx)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(edge_type,src_node_id,dst_node_id,source_tag) "
                "DO UPDATE SET props=excluded.props, created_tx=excluded.created_tx",
                (edge_type, src, dst, canonical_json(props), source_tag, tx.tx_id),
            )
            return {"edge_type": edge_type, "src": src, "dst": dst, "bulk": True,
                    "source_tag": source_tag}

        # ── versioned edge: identity = (edge_type, src, dst); supersede on relink ──
        erow = cur.execute(
            "SELECT * FROM edge WHERE edge_type=? AND src_node_id=? AND dst_node_id=?",
            (edge_type, src, dst),
        ).fetchone()
        if erow is None:
            if expected_head is not None:
                raise Conflict("expected_head given but edge does not exist")
            eid = ulid()
            cur.execute(
                "INSERT INTO edge(edge_id,edge_type,src_node_id,dst_node_id,created_tx) "
                "VALUES(?,?,?,?,?)", (eid, edge_type, src, dst, tx.tx_id))
            vid = ulid()
            cur.execute(
                "INSERT INTO edge_version(version_id,edge_id,seq,prev_version,props,schema_ver,"
                "content_hash,tx_from,tx_to) VALUES(?,?,?,?,?,?,?,?,?)",
                (vid, eid, 1, None, canonical_json(props), schema_ver, ch, tx.tx_id, SENTINEL))
            return {"edge_id": eid, "version_id": vid, "seq": 1, "created": True,
                    "superseded": False, "src": src, "dst": dst}
        eid = erow["edge_id"]
        head = cur.execute(
            "SELECT * FROM edge_version WHERE edge_id=? AND tx_to=?", (eid, SENTINEL)
        ).fetchone()
        head = dict(head)
        if expected_head is not None and head["version_id"] != expected_head:
            raise Conflict(f"stale edge write: head is {head['version_id']}")
        if head["content_hash"] == ch:
            return {"edge_id": eid, "version_id": head["version_id"], "seq": head["seq"],
                    "created": False, "superseded": False, "noop": True, "src": src, "dst": dst}
        cur.execute("UPDATE edge_version SET tx_to=? WHERE edge_id=? AND tx_to=?",
                    (tx.tx_id, eid, SENTINEL))
        vid = ulid()
        seq = head["seq"] + 1
        cur.execute(
            "INSERT INTO edge_version(version_id,edge_id,seq,prev_version,props,schema_ver,"
            "content_hash,tx_from,tx_to) VALUES(?,?,?,?,?,?,?,?,?)",
            (vid, eid, seq, head["version_id"], canonical_json(props), schema_ver, ch,
             tx.tx_id, SENTINEL))
        return {"edge_id": eid, "version_id": vid, "seq": seq, "created": False,
                "superseded": True, "src": src, "dst": dst}


def bulk_replace(db: Database, agent_id: str, edge_type: str, source_tag: str,
                 edges: list[tuple], *, reason: Optional[str] = None) -> dict:
    """Wholesale (re)load of a bulk edge set under a source_tag: delete-then-insert atomically."""
    with db.write(agent_id, reason) as tx:
        cur = tx.cur
        traits = schemas.edge_traits(cur, edge_type)
        if traits["versioned"]:
            raise Invalid(f"edge type {edge_type!r} is versioned; use upsert_edge")
        cur.execute("DELETE FROM edge_bulk WHERE edge_type=? AND source_tag=?",
                    (edge_type, source_tag))
        n = 0
        for e in edges:
            s, d = _resolve_redirect(cur, e[0]), _resolve_redirect(cur, e[1])
            p = e[2] if len(e) > 2 else {}
            cur.execute(
                "INSERT INTO edge_bulk(edge_type,src_node_id,dst_node_id,props,source_tag,created_tx)"
                " VALUES(?,?,?,?,?,?)", (edge_type, s, d, canonical_json(p), source_tag, tx.tx_id))
            n += 1
        return {"edge_type": edge_type, "source_tag": source_tag, "count": n, "replaced": True}


# ── traversal ────────────────────────────────────────────────────────────────────────
def neighbors(db: Database, node_id: str, *, edge_types: Optional[list[str]] = None,
              depth: int = 1, direction: str = "out") -> dict:
    if depth < 1 or depth > 4:
        raise Invalid("depth must be 1..4")
    if direction not in ("out", "in", "both"):
        raise Invalid("direction must be out|in|both")
    with db.read() as cur:
        node_id = _resolve_redirect(cur, node_id)
        type_filter = ""
        params_types: tuple = ()
        if edge_types:
            ph = ",".join("?" * len(edge_types))
            type_filter = f" AND edge_type IN ({ph})"
            params_types = tuple(edge_types)

        # unified current-edge view: versioned heads + bulk, as (s, d)
        edge_cte = (
            "cur_edges AS ("
            "  SELECT e.src_node_id AS s, e.dst_node_id AS d "
            "    FROM edge e JOIN edge_version ev ON ev.edge_id=e.edge_id AND ev.tx_to=? "
            f"   WHERE 1=1{type_filter} "
            "  UNION ALL "
            f"  SELECT src_node_id, dst_node_id FROM edge_bulk WHERE 1=1{type_filter}"
            ")"
        )
        if direction == "out":
            adj = "adj AS (SELECT s AS a, d AS b FROM cur_edges)"
        elif direction == "in":
            adj = "adj AS (SELECT d AS a, s AS b FROM cur_edges)"
        else:
            adj = ("adj AS (SELECT s AS a, d AS b FROM cur_edges "
                   "UNION SELECT d AS a, s AS b FROM cur_edges)")
        sql = (
            f"WITH RECURSIVE {edge_cte}, {adj}, "
            "reach(node_id, depth, path) AS ("
            "  SELECT ?, 0, ',' || ? || ',' "
            "  UNION "
            "  SELECT adj.b, r.depth+1, r.path || adj.b || ',' "
            "    FROM reach r JOIN adj ON adj.a = r.node_id "
            "   WHERE r.depth < ? AND r.path NOT LIKE '%,' || adj.b || ',%' "
            ") SELECT node_id, MIN(depth) AS depth FROM reach "
            "WHERE node_id != ? GROUP BY node_id ORDER BY depth, node_id"
        )
        params = (SENTINEL, *params_types, *params_types, node_id, node_id, depth, node_id)
        rows = cur.execute(sql, params).fetchall()
        out = []
        for r in rows:
            head = _current_node_version(cur, r["node_id"])
            nrow = _node_row(cur, r["node_id"])
            out.append({"node_id": r["node_id"], "depth": r["depth"],
                        "node_type": nrow["node_type"] if nrow else None,
                        "props": json.loads(head["props"]) if head else None,
                        "flags": node_flags(cur, r["node_id"])})
        return {"start": node_id, "direction": direction, "depth": depth, "neighbors": out}


def search_nodes(db: Database, query: str, *, types: Optional[list[str]] = None,
                 limit: int = 25, cursor: int = 0) -> dict:
    """Search current node versions via FTS5 (unicode61 + trigram, RRF-fused)."""
    from . import search as _search
    return _search.search(db, query, types=types, limit=limit, cursor=cursor)
    limit = max(1, min(limit, 200))
    with db.read() as cur:
        params: list = []
        where = "nv.tx_to=?"
        params.append(SENTINEL)
        if types:
            where += " AND n.node_type IN (%s)" % ",".join("?" * len(types))
            params.extend(types)
        if query:
            where += " AND nv.props LIKE ?"
            params.append(f"%{query}%")
        rows = cur.execute(
            f"SELECT n.node_id, n.node_type, n.subject_key, n.subject_version, nv.version_id, "
            f"nv.props FROM node n JOIN node_version nv ON nv.node_id=n.node_id "
            f"WHERE {where} AND n.redirect_to IS NULL ORDER BY n.node_id LIMIT ? OFFSET ?",
            (*params, limit + 1, cursor),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        results = []
        for r in rows:
            props = json.loads(r["props"])
            snippet = json.dumps(props)[:200]
            results.append({
                "node_id": r["node_id"], "node_type": r["node_type"],
                "subject_key": r["subject_key"], "subject_version": r["subject_version"],
                "version_id": r["version_id"], "snippet": snippet,
                "flags": node_flags(cur, r["node_id"]),
            })
    return {"results": results, "next_cursor": (cursor + limit) if has_more else None,
            "has_more": has_more}


def node_types(db: Database, *, subject_key: Optional[str] = None) -> dict:
    """Node types actually present, with counts — the discovery half of searching by type.

    schema_get says which types are DEFINED; this says which have data and how much, so an agent
    can pick a type to browse instead of guessing. Optionally scoped to one subject_key.
    """
    where, args = "n.redirect_to IS NULL", []
    if subject_key:
        where += " AND n.subject_key = ?"
        args.append(subject_key)
    with db.read() as cur:
        rows = cur.execute(
            f"SELECT n.node_type AS t, COUNT(*) AS c FROM node n WHERE {where} "
            f"GROUP BY n.node_type ORDER BY c DESC, t", args).fetchall()
        total = sum(r["c"] for r in rows)
    return {"types": [{"type": r["t"], "nodes": r["c"]} for r in rows],
            "distinct_types": len(rows), "total_nodes": total,
            **({"subject_key": subject_key} if subject_key else {}),
            "hint": "graph_search(types=[...]) to browse one; empty query lists them all"}
