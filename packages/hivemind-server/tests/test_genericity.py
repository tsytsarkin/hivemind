"""Acceptance: the engine is domain-agnostic. Two unrelated packs work with zero code change,
and the engine source contains no domain nouns."""
import json
from pathlib import Path

import pytest
from hivemind_server.db import Database
from hivemind_server import schemas, graph

REPO = Path(__file__).resolve().parents[3]
ENGINE = REPO / "packages/hivemind-server/src/hivemind_server"
DOMAIN_NOUNS = ["IOSurface", "kernelcache", "contradict", "finding", "patchdiff",
                "vulnerab", "exploit", "iphone"]


def test_engine_has_no_domain_nouns():
    offenders = []
    for py in ENGINE.glob("*.py"):
        text = py.read_text().lower()
        for noun in DOMAIN_NOUNS:
            if noun.lower() in text:
                offenders.append(f"{py.name}: {noun}")
    assert offenders == [], f"engine leaked domain vocabulary: {offenders}"


def test_security_research_pack(tmp_path):
    db = Database(tmp_path / "sec.db")
    pack = json.loads((REPO / "packs/security-research/schema.json").read_text())
    schemas.apply_pack(db, "op", pack)
    comp = graph.upsert_node(db, "a", "component", {"name": "IOSurfaceRootUserClient"},
                             subject_key="IOSurfaceRootUserClient", subject_version="26.6",
                             subject_order="0266")
    find = graph.upsert_node(db, "a", "finding", {"title": "UAF", "severity": "high"})
    poc = graph.upsert_node(db, "a", "poc", {"name": "trigger.c"})
    graph.upsert_edge(db, "a", "evidence_for", poc["node_id"], find["node_id"])   # poc->finding OK
    # domain/range enforced: component is NOT a valid evidence_for source
    import pytest as _pt
    with _pt.raises(Exception):
        graph.upsert_edge(db, "a", "evidence_for", comp["node_id"], find["node_id"])
    # assertive contradicts flags both endpoints
    f2 = graph.upsert_node(db, "a", "finding", {"title": "not a bug"})
    graph.upsert_edge(db, "a", "contradicts", find["node_id"], f2["node_id"], {"status": "open"})
    assert graph.get_node(db, node_id=find["node_id"])["flags"].get("disputed") is True


def test_unrelated_citations_pack_same_engine(tmp_path):
    """A totally different domain (bibliography) works with NO engine change."""
    db = Database(tmp_path / "cite.db")
    pack = {"name": "citations",
            "node_types": {"paper": {"schema": {"type": "object",
                           "properties": {"title": {"type": "string"}}, "required": ["title"]}},
                           "author": {"schema": {"type": "object",
                           "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            "edge_types": {"cites": {"schema": {"type": "object"}, "versioned": True,
                                     "acyclic": True, "src_types": ["paper"], "dst_types": ["paper"]},
                           "wrote": {"schema": {"type": "object"}, "versioned": False,
                                     "src_types": ["author"], "dst_types": ["paper"]}}}
    schemas.apply_pack(db, "op", pack)
    p1 = graph.upsert_node(db, "a", "paper", {"title": "Attention"})["node_id"]
    p2 = graph.upsert_node(db, "a", "paper", {"title": "BERT"})["node_id"]
    graph.upsert_edge(db, "a", "cites", p2, p1)                       # BERT cites Attention
    with pytest.raises(Exception):
        graph.upsert_edge(db, "a", "cites", p1, p2)                   # acyclic guard
    a1 = graph.upsert_node(db, "a", "author", {"name": "Vaswani"})["node_id"]
    graph.upsert_edge(db, "a", "wrote", a1, p1, source_tag="dblp")    # bulk edge
    nb = graph.neighbors(db, p2, edge_types=["cites"], depth=1)["neighbors"]
    assert any(n["node_id"] == p1 for n in nb)
