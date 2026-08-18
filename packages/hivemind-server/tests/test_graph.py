import threading
import pytest
from hivemind_server import graph
from hivemind_server.db import Conflict, SENTINEL


def heads_count(db, node_id):
    with db.read() as cur:
        return cur.execute(
            "SELECT COUNT(*) c FROM node_version WHERE node_id=? AND tx_to=?",
            (node_id, SENTINEL)).fetchone()["c"]


# ── revision axis ──────────────────────────────────────────────────────────────
def test_revision_supersession_and_history(db):
    r1 = graph.upsert_node(db, "a", "finding", {"title": "v1"})
    nid = r1["node_id"]
    r2 = graph.upsert_node(db, "a", "finding", {"title": "v2"}, node_id=nid)
    r3 = graph.upsert_node(db, "a", "finding", {"title": "v3"}, node_id=nid)
    assert r2["superseded"] and r3["superseded"]
    assert heads_count(db, nid) == 1
    h = graph.get_node(db, node_id=nid, history=True)["history"]
    assert [v["props"]["title"] for v in h] == ["v3", "v2", "v1"]      # newest first
    assert h[0]["prev_version"] == h[1]["version_id"]                   # unbroken chain
    assert graph.get_node(db, node_id=nid)["current"]["props"]["title"] == "v3"


def test_idempotent_noop_on_same_props(db):
    r1 = graph.upsert_node(db, "a", "finding", {"title": "x"})
    r2 = graph.upsert_node(db, "a", "finding", {"title": "x"}, node_id=r1["node_id"])
    assert r2.get("noop") is True and r2["version_id"] == r1["version_id"]


def test_as_of(db):
    r1 = graph.upsert_node(db, "a", "finding", {"title": "v1"})
    nid = r1["node_id"]
    with db.read() as cur:
        t_after_v1 = cur.execute("SELECT MAX(tx_id) t FROM tx").fetchone()["t"]
    graph.upsert_node(db, "a", "finding", {"title": "v2"}, node_id=nid)
    asof = graph.get_node(db, node_id=nid, as_of=t_after_v1)["current"]
    assert asof["props"]["title"] == "v1"


# ── optimistic concurrency (the correctness story) ──────────────────────────────
def test_cas_race_exactly_one_winner(db):
    r0 = graph.upsert_node(db, "a", "finding", {"n": 0})
    nid, head0 = r0["node_id"], r0["version_id"]
    results = {"ok": 0, "conflict": 0}
    lock = threading.Lock()

    def writer(i):
        try:
            graph.upsert_node(db, f"w{i}", "finding", {"n": i}, node_id=nid,
                              expected_head=head0)
            with lock: results["ok"] += 1
        except Conflict:
            with lock: results["conflict"] += 1

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(1, 13)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert results["ok"] == 1                    # exactly one winner
    assert results["conflict"] == 11             # everyone else cleanly rejected
    assert heads_count(db, nid) == 1             # single head invariant holds
    h = graph.get_node(db, node_id=nid, history=True)["history"]
    assert len(h) == 2 and h[0]["prev_version"] == h[1]["version_id"]


def test_blind_concurrent_writes_never_corrupt(db):
    r0 = graph.upsert_node(db, "a", "finding", {"n": 0})
    nid = r0["node_id"]

    def writer(i):
        graph.upsert_node(db, f"w{i}", "finding", {"n": i}, node_id=nid)  # no expected_head

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(1, 9)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert heads_count(db, nid) == 1
    h = graph.get_node(db, node_id=nid, history=True)["history"]
    assert len(h) == 9                            # 1 initial + 8 supersedes, unbroken


# ── subject-version axis + orthogonality ────────────────────────────────────────
def test_subject_axis_orthogonal(db):
    def put(ver, order, title):
        return graph.upsert_node(db, "a", "component", {"title": title},
                                 subject_key="X", subject_version=ver, subject_order=order)
    c5 = put("26.5", "0265", "on 26.5")
    c6 = put("26.6", "0266", "on 26.6")
    c7 = put("27.0b4", "0270b4", "on 27.0b4")
    assert len({c5["node_id"], c6["node_id"], c7["node_id"]}) == 3   # 3 distinct cells

    subs = graph.list_subjects(db, "X")["cells"]
    assert [c["subject_version"] for c in subs] == ["26.5", "26.6", "27.0b4"]

    # subject as-of 26.6 resolves to the 26.6 cell, not 27.0b4
    res = graph.list_subjects(db, "X", as_of_subject="0266")["resolved"]
    assert res["subject_version"] == "26.6"

    # superseding the 26.6 cell (revision axis) leaves 26.5 / 27.0b4 untouched
    graph.upsert_node(db, "a", "component", {"title": "on 26.6 (rev2)"},
                      subject_key="X", subject_version="26.6", subject_order="0266")
    assert heads_count(db, c6["node_id"]) == 1
    assert graph.get_node(db, node_id=c6["node_id"])["current"]["props"]["title"] == "on 26.6 (rev2)"
    assert graph.get_node(db, node_id=c5["node_id"])["current"]["props"]["title"] == "on 26.5"
    assert graph.get_node(db, node_id=c7["node_id"])["current"]["props"]["title"] == "on 27.0b4"
    # upsert-by-subject landed on the SAME cell, so still 3 cells total
    assert len(graph.list_subjects(db, "X")["cells"]) == 3


# ── edges: assertive surfacing, bulk vs versioned, traits ───────────────────────
def test_assertive_edge_surfaces_dispute(db):
    a = graph.upsert_node(db, "u", "finding", {"t": "A"})["node_id"]
    b = graph.upsert_node(db, "u", "finding", {"t": "B"})["node_id"]
    graph.upsert_edge(db, "u", "contradicts", a, b, {"status": "open"})
    assert graph.get_node(db, node_id=a)["flags"].get("disputed") is True
    assert graph.get_node(db, node_id=b)["flags"].get("disputed") is True
    graph.upsert_edge(db, "u", "contradicts", a, b, {"status": "resolved"})
    assert graph.get_node(db, node_id=a)["flags"].get("disputed") is None


def test_bulk_vs_versioned_edges(db):
    f1 = graph.upsert_node(db, "u", "function", {"n": "f1"})["node_id"]
    f2 = graph.upsert_node(db, "u", "function", {"n": "f2"})["node_id"]
    # bulk edge requires source_tag; creates NO version rows
    graph.upsert_edge(db, "u", "calls", f1, f2, source_tag="kc@26.6")
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) c FROM edge_version").fetchone()["c"] == 0
        assert cur.execute("SELECT COUNT(*) c FROM edge_bulk").fetchone()["c"] == 1
    # bulk_replace swaps the whole source cleanly
    graph.bulk_replace(db, "u", "calls", "kc@26.6", [(f2, f1)])
    with db.read() as cur:
        rows = cur.execute("SELECT src_node_id s,dst_node_id d FROM edge_bulk").fetchall()
    assert [(r["s"], r["d"]) for r in rows] == [(f2, f1)]
    # versioned edge gets a history chain
    r = graph.upsert_edge(db, "u", "refines", f1, f2, {"note": "v1"})
    assert r["created"]
    r2 = graph.upsert_edge(db, "u", "refines", f1, f2, {"note": "v2"})
    assert r2["superseded"] and r2["seq"] == 2

    nb = graph.neighbors(db, f1, edge_types=["refines"], depth=1)["neighbors"]
    assert any(n["node_id"] == f2 for n in nb)


def test_acyclic_guard(db):
    a = graph.upsert_node(db, "u", "component", {})["node_id"]
    b = graph.upsert_node(db, "u", "component", {})["node_id"]
    graph.upsert_edge(db, "u", "depends_on", a, b)
    with pytest.raises(Exception):
        graph.upsert_edge(db, "u", "depends_on", b, a)   # would form a cycle


def test_symmetric_canonicalizes(db):
    a = graph.upsert_node(db, "u", "finding", {"t": "A"})["node_id"]
    b = graph.upsert_node(db, "u", "finding", {"t": "B"})["node_id"]
    r1 = graph.upsert_edge(db, "u", "contradicts", a, b, {"status": "open"})
    r2 = graph.upsert_edge(db, "u", "contradicts", b, a, {"status": "open"})  # same edge
    assert r1["src"] == r2["src"] and r1["dst"] == r2["dst"]
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) c FROM edge WHERE edge_type='contradicts'"
                           ).fetchone()["c"] == 1


def test_unknown_type_rejected(db):
    with pytest.raises(Exception):
        graph.upsert_node(db, "u", "no_such_type", {})
