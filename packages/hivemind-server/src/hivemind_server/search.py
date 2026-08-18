"""FTS5 search over node content. App-maintained (props are arbitrary JSON): on each node write
we flatten props to text and (re)index into two FTS tables — unicode61 for prose, trigram for
symbols/paths. Query fuses both with reciprocal-rank fusion (RRF)."""
from __future__ import annotations

import json
from typing import Any, List, Optional

from .db import SENTINEL, Database

_RRF_K = 60


def _flatten(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _flatten(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten(v, out)
    elif obj is not None:
        out.append(str(obj))


def flatten_props(props: dict) -> str:
    parts: List[str] = []
    _flatten(props, parts)
    return " ".join(parts)


def index_node(cur, node_id: str, props: dict) -> None:
    """(Re)index a node's current props. Called inside the same write tx as the node write."""
    text = flatten_props(props)
    cur.execute("DELETE FROM node_fts WHERE node_id=?", (node_id,))
    cur.execute("DELETE FROM sym_fts WHERE node_id=?", (node_id,))
    cur.execute("INSERT INTO node_fts(node_id, body) VALUES(?,?)", (node_id, text))
    cur.execute("INSERT INTO sym_fts(node_id, body) VALUES(?,?)", (node_id, text))


def unindex_node(cur, node_id: str) -> None:
    cur.execute("DELETE FROM node_fts WHERE node_id=?", (node_id,))
    cur.execute("DELETE FROM sym_fts WHERE node_id=?", (node_id,))


def _fts_query(q: str) -> str:
    """Build a safe FTS5 MATCH expression: quote each token, OR them for recall."""
    toks = [t for t in ''.join(ch if ch.isalnum() or ch in "_.:/-" else " " for ch in q).split()
            if t]
    if not toks:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)


def search(db: Database, query: str, *, types: Optional[List[str]] = None,
           limit: int = 25) -> dict:
    """Hybrid FTS: BM25 over prose + trigram over symbols, fused by RRF. Falls back to listing
    recent nodes when query is empty."""
    limit = max(1, min(limit, 200))
    match = _fts_query(query)
    with db.read() as cur:
        from .graph import node_flags, _current_node_version, _node_row  # local import
        ranks: dict = {}
        if match:
            for tbl in ("node_fts", "sym_fts"):
                rows = cur.execute(
                    f"SELECT node_id, rank FROM {tbl} WHERE {tbl} MATCH ? "
                    f"ORDER BY rank LIMIT 200", (match,)).fetchall()
                for i, r in enumerate(rows):
                    ranks[r["node_id"]] = ranks.get(r["node_id"], 0.0) + 1.0 / (_RRF_K + i)
            ordered = sorted(ranks, key=lambda n: ranks[n], reverse=True)
        else:
            ordered = [r["node_id"] for r in cur.execute(
                "SELECT node_id FROM node ORDER BY created_tx DESC LIMIT 200")]
        results = []
        for nid in ordered:
            nrow = _node_row(cur, nid)
            if nrow is None or nrow["redirect_to"] is not None:
                continue
            if types and nrow["node_type"] not in types:
                continue
            head = _current_node_version(cur, nid)
            if head is None:
                continue
            props = json.loads(head["props"])
            results.append({"node_id": nid, "node_type": nrow["node_type"],
                            "subject_key": nrow["subject_key"],
                            "subject_version": nrow["subject_version"],
                            "version_id": head["version_id"],
                            "score": round(ranks.get(nid, 0.0), 5),
                            "snippet": json.dumps(props)[:200],
                            "flags": node_flags(cur, nid)})
            if len(results) >= limit:
                break
    return {"results": results, "count": len(results),
            "backend": "fts5+rrf" if match else "recent"}


def reindex_all(db: Database) -> int:
    """Rebuild the FTS tables from current node heads (migration / repair)."""
    with db.write("reindex", "fts_reindex") as tx:
        cur = tx.cur
        cur.execute("DELETE FROM node_fts")
        cur.execute("DELETE FROM sym_fts")
        rows = cur.execute(
            "SELECT node_id, props FROM node_version WHERE tx_to=?", (SENTINEL,)).fetchall()
        for r in rows:
            index_node(cur, r["node_id"], json.loads(r["props"]))
    return len(rows)
