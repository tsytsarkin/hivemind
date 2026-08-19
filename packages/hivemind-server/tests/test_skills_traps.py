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


# ── discovery: catalog, duplicate prevention, node linking ──────────────────────
def _pub(db, sid, title, desc, tags=None, force=False):
    return skills.publish(db, "a", id=sid, version="1.0.0", title=title, description=desc,
                          body="steps", tags=tags or [], force=force)


def test_catalog_lists_topics_and_entries(db):
    _pub(db, "ops/restart", "Restart the server", "How to restart it safely.", ["ops"])
    _pub(db, "re/unpack", "Unpack a cache", "Pull raw images out of a shared cache.",
         ["re", "binary"])
    cat = skills.catalog(db)
    assert cat["total_skills"] == 2
    topics = {t["topic"]: t["skills"] for t in cat["topics"]}
    assert topics == {"binary": 1, "ops": 1, "re": 1}
    assert {s["id"] for s in cat["skills"]} == {"ops/restart", "re/unpack"}
    narrowed = skills.catalog(db, topic="ops")
    assert [s["id"] for s in narrowed["skills"]] == ["ops/restart"]


def test_duplicate_new_skill_is_refused_but_revision_is_not(db):
    _pub(db, "ops/restart-server", "Restart the server", "How to restart the server safely.")
    # a near-identical NEW id is refused, and the error names the skill to revise instead
    with pytest.raises(Invalid) as e:
        _pub(db, "ops/restart-the-server", "Restart the server",
             "How to restart the server safely.")
    msg = str(e.value)
    assert "ops/restart-server@1.0.0" in msg and "NEW VERSION" in msg
    # force is the explicit override
    r = _pub(db, "ops/restart-the-server", "Restart the server",
             "How to restart the server safely.", force=True)
    assert r["warnings"] and r["warnings"][0]["similar_skills"]
    # revising the ORIGINAL skill must never be blocked by its own similarity
    r2 = skills.publish(db, "a", id="ops/restart-server", version="1.1.0",
                        title="Restart the server", description="How to restart it safely.",
                        body="better steps")
    assert r2["version"] == "1.1.0" and r2["new_skill"] is False
    # an unrelated skill sails through
    assert _pub(db, "re/decode-firmware", "Decode firmware", "Turn a blob into images.")


def test_skill_linked_to_node_is_discoverable_from_it(db):
    n = graph.upsert_node(db, "a", "thing", {"name": "IOSurface"})["node_id"]
    _pub(db, "re/trace-iosurface", "Trace IOSurface calls", "How to trace the client.")
    skills.link(db, "a", "re/trace-iosurface", n, relation="about", note="entry point")
    got = skills.for_node(db, n)
    assert [s["id"] for s in got] == ["re/trace-iosurface"]
    assert got[0]["relation"] == "about"
    assert skills.catalog(db)["skills"][0]["linked_nodes"] == 1
    with pytest.raises(Invalid):
        skills.link(db, "a", "re/trace-iosurface", "no-such-node")
    with pytest.raises(NotFound):
        skills.link(db, "a", "no/such-skill", n)


def test_catalog_counts_are_not_the_page_size(db):
    # deliberately unrelated names/descriptions — the dedup guard rejects near-identical ones
    fixtures = [("ops/rotate-logs", "Rotate logs", "Trim and archive server log files."),
                ("re/decode-firmware", "Decode firmware", "Turn a packed blob into images."),
                ("ops/mint-token", "Mint a token", "Issue credentials for a new machine."),
                ("re/diff-binaries", "Diff binaries", "Compare two builds for changed functions."),
                ("ops/backup-db", "Back up the database", "Snapshot and replicate storage."),
                ("re/trace-syscalls", "Trace syscalls", "Watch kernel entry points at runtime."),
                ("ops/prune-blobs", "Prune blobs", "Reclaim unreferenced artifact storage.")]
    for i, (sid, title, desc) in enumerate(fixtures):
        _pub(db, sid, title, desc, ["ops"] if sid.startswith("ops") else ["re"])
    page = skills.catalog(db, limit=3)
    assert page["total_skills"] == 7          # the library, not the page
    assert page["returned"] == 3 and page["next_offset"] == 3
    rest = skills.catalog(db, limit=3, offset=3)
    assert rest["returned"] == 3 and rest["next_offset"] == 6
    last = skills.catalog(db, limit=3, offset=6)
    assert last["returned"] == 1 and last["next_offset"] is None
    ops = skills.catalog(db, topic="ops", limit=100)
    assert ops["matched"] == 4 and ops["total_skills"] == 7
