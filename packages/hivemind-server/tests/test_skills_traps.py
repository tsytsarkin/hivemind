import pytest
from hivemind_server.db import Database, Invalid, Conflict, NotFound
from hivemind_server import schemas, graph, skills, traps


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "st.db")
    with d.write("t", "seed") as tx:
        schemas.define_type(tx.cur, tx, "node", "thing", {"type": "object"}, status="active")
    return d


# ── mini-skills ────────────────────────────────────────────────────────────────
def test_skill_publish_immutable_and_search(db):
    skills.publish(db, "a", id="re/unpack", version="1.0.0", title="Unpack a cache",
                   description="How to unpack a shared cache. Use when you need raw images.",
                   body="1. run x\n2. run y", tags=["re", "cache"],
                   verified_how="ran it end to end on two builds")
    with pytest.raises(Conflict):                       # immutable
        skills.publish(db, "a", id="re/unpack", version="1.0.0", title="t",
                       description="d", body="b")
    with pytest.raises(Invalid):                        # ranges rejected
        skills.publish(db, "a", id="re/unpack", version="^1.0", title="t",
                       description="d", body="b")
    skills.publish(db, "a", id="re/unpack", version="1.1.0", title="Unpack a cache",
                   description="Faster method.", body="1. run z", tags=["re"])
    found = skills.search(db, "unpack")
    assert found["count"] == 1 and found["skills"][0]["version"] == "1.1.0"   # newest only
    got = skills.get(db, "re/unpack")
    assert got["version"] == "1.1.0" and "run z" in got["body"]
    assert skills.get(db, "re/unpack", "1.0.0")["version"] == "1.0.0"          # exact pin


def test_skill_requires_substance(db):
    with pytest.raises(Invalid):
        skills.publish(db, "a", id="x/y", version="1.0.0", title="", description="d", body="b")
    with pytest.raises(Invalid):                        # keep mini-skills mini
        skills.publish(db, "a", id="x/y", version="1.0.0", title="t", description="d",
                       body="z" * 20001)


def test_skill_yank_hides_from_search_but_keeps_pin(db):
    skills.publish(db, "a", id="x/y", version="1.0.0", title="T", description="D", body="B")
    skills.yank(db, "a", "x/y", "1.0.0", "wrong approach")
    assert skills.search(db, "T")["count"] == 0
    assert skills.get(db, "x/y", "1.0.0")["yanked"] is True   # still fetchable by exact pin


# ── traps ──────────────────────────────────────────────────────────────────────
def test_trap_requires_evidence_shape(db):
    with pytest.raises(Invalid) as e:
        traps.record(db, "a", title="t", what_failed="", symptom="s")
    assert "opinion" in str(e.value)                      # a trap must be an observation
    with pytest.raises(Invalid):
        traps.record(db, "a", title="t", what_failed="w", symptom="")


def test_trap_project_wide_and_node_scoped(db):
    n = graph.upsert_node(db, "a", "thing", {"x": 1})["node_id"]
    p = traps.record(db, "a", title="Don't parse with regex", what_failed="regex parse",
                     symptom="silently drops nested entries", instead="use the real parser",
                     cost_minutes=90, verified_how="reproduced")
    assert p["scope"] == "project"
    t = traps.record(db, "a", title="Node-specific dead end", what_failed="tried flag -z",
                     symptom="hangs forever", node_id=n)
    assert t["scope"] == "node"
    attached = traps.for_node(db, n)
    assert [x["trap_id"] for x in attached] == [t["trap_id"]]
    assert traps.search(db, "regex")["count"] == 1
    with pytest.raises(Invalid):                          # unknown node rejected
        traps.record(db, "a", title="t", what_failed="w", symptom="s", node_id="nope")


def test_trap_is_falsifiable(db):
    t = traps.record(db, "a", title="T", what_failed="w", symptom="s")["trap_id"]
    traps.set_status(db, "b", t, "disputed", "works fine on 26.6, see measurement")
    assert traps.get(db, t)["status"] == "disputed"
    assert traps.search(db, "T")["count"] == 1            # disputed stays VISIBLE
    traps.set_status(db, "b", t, "retired", "fixed upstream")
    assert traps.search(db, "T")["count"] == 0            # retired drops out
    assert traps.search(db, "T", include_retired=True)["count"] == 1
    with pytest.raises(Invalid):
        traps.set_status(db, "b", t, "bogus")


def test_trap_scoped_to_subject_version(db):
    n = graph.upsert_node(db, "a", "thing", {"v": 1}, subject_key="X",
                          subject_version="26.6")["node_id"]
    traps.record(db, "a", title="Only on 26.6", what_failed="w", symptom="s",
                 subject_key="X", subject_version="26.6")
    assert len(traps.for_node(db, n, subject_key="X", subject_version="26.6")) == 1
    # a different subject version must not inherit it
    n2 = graph.upsert_node(db, "a", "thing", {"v": 2}, subject_key="X",
                           subject_version="27.0")["node_id"]
    assert traps.for_node(db, n2, subject_key="X", subject_version="27.0") == []
