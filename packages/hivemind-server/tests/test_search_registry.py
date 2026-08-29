import pytest
from hivemind_server.db import Database, Invalid, Conflict
from hivemind_server import schemas, graph, registry, semver, search


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "s.db")
    with d.write("t", "seed") as tx:
        schemas.define_type(tx.cur, tx, "node", "component", {"type": "object"}, status="active")
    return d


def test_semver_basics():
    assert semver.is_range("^1.2") and semver.is_range(">=1.0.0") and not semver.is_range("1.2.3")
    assert semver.compare("1.2.0", "1.10.0") < 0
    assert semver.compare("1.0.0-rc.1", "1.0.0") < 0        # prerelease sorts before release
    assert semver.satisfies("1.4.2", "^1.2")
    assert not semver.satisfies("2.0.0", "^1.2")
    assert semver.satisfies("1.2.5", "~1.2.0") and not semver.satisfies("1.3.0", "~1.2.0")
    assert semver.latest(["1.0.0", "1.2.0", "1.10.0", "2.0.0-rc.1"]) == "1.10.0"


def test_fts_search_prose_and_symbols(db):
    graph.upsert_node(db, "a", "component", {"title": "IOSurfaceRootUserClient",
                                             "note": "kernel UAF in s_set_value"})
    graph.upsert_node(db, "a", "component", {"title": "AppleAVE2", "note": "unrelated codec"})
    r = search.search(db, "kernel")
    assert any("IOSurface" in x["snippet"] for x in r["results"])
    assert r["backend"] == "fts5+rrf"
    # trigram catches a symbol substring the prose tokenizer would mangle
    r2 = search.search(db, "set_value")
    assert any("IOSurface" in x["snippet"] for x in r2["results"])
    # supersede changes what's indexed
    nid = r["results"][0]["node_id"]
    graph.upsert_node(db, "a", "component", {"title": "IOSurfaceRootUserClient",
                                             "note": "now patched"}, node_id=nid)
    assert search.search(db, "patched")["results"]


def _publish_blob(db, digest="sha256:" + "a" * 64):
    with db.write("t", "fake blob") as tx:
        tx.cur.execute("INSERT INTO blob(digest,size,created_tx) VALUES(?,?,?) "
                       "ON CONFLICT(digest) DO NOTHING", (digest, 10, tx.tx_id))
    return digest


def test_tool_publish_immutable_and_resolve(db):
    dg = _publish_blob(db)
    man = {"id": "org.x/unpack", "version": "1.0.0", "runtime": "python",
           "entrypoint": "unpack.py", "runtime_hint": "uv", "description": "unpacks stuff"}
    registry.publish(db, "a", man, dg)
    # immutable: same version rejected
    with pytest.raises(Conflict):
        registry.publish(db, "a", man, dg)
    # range as a version is rejected
    with pytest.raises(Invalid):
        registry.publish(db, "a", {**man, "version": "^1.0"}, dg)
    # publish a newer version, resolve picks latest
    registry.publish(db, "a", {**man, "version": "1.2.0"}, dg)
    registry.publish(db, "a", {**man, "version": "2.0.0"}, dg)
    r = registry.resolve(db, "org.x/unpack", constraint="^1.0")
    assert r["version"] == "1.2.0"                       # ^1.0 excludes 2.0.0
    assert r["run"] == "uv run --script unpack.py"
    assert r["newer_incompatible"]["version"] == "2.0.0"
    # yank 1.2.0 -> resolve ^1.0 falls back to 1.0.0
    registry.yank(db, "a", "org.x/unpack", "1.2.0", "bad")
    assert registry.resolve(db, "org.x/unpack", constraint="^1.0")["version"] == "1.0.0"
    # search finds it
    assert any(t["id"] == "org.x/unpack" for t in registry.search(db)["tools"])


def test_search_cursor_pages_do_not_repeat_or_skip(db):
    """graph_search accepted a cursor and silently ignored it: every page returned page 1, so an
    agent paginating looped forever. Pages must partition the result set exactly."""
    ids = [graph.upsert_node(db, "a", "component",
                             {"title": f"paginated widget {i}", "note": "walkable corpus"}
                             )["node_id"] for i in range(12)]

    seen, cursor, pages = [], 0, 0
    while True:
        page = search.search(db, "paginated", limit=5, cursor=cursor)
        assert page["cursor"] == cursor
        seen.extend(r["node_id"] for r in page["results"])
        pages += 1
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] == cursor + len(page["results"])
        cursor = page["next_cursor"]
        assert pages < 10, "pagination did not terminate"

    assert len(seen) == len(set(seen)), "a node appeared on two pages"
    assert set(seen) == set(ids), "pagination lost rows"
    assert pages == 3                                    # 12 rows at 5 per page

    # page 2 must differ from page 1 (the exact symptom reported)
    p1 = search.search(db, "paginated", limit=5, cursor=0)["results"]
    p2 = search.search(db, "paginated", limit=5, cursor=5)["results"]
    assert [r["node_id"] for r in p1] != [r["node_id"] for r in p2]

    # cursor survives the graph_search entry point too, not just the search module
    g1 = graph.search_nodes(db, "paginated", limit=5, cursor=0)
    g2 = graph.search_nodes(db, "paginated", limit=5, cursor=5)
    assert [r["node_id"] for r in g1["results"]] != [r["node_id"] for r in g2["results"]]

    # a cursor past the end is empty, not an error or a wrapped-around page
    tail = search.search(db, "paginated", limit=5, cursor=99)
    assert tail["results"] == [] and tail["has_more"] is False


def test_search_cursor_respects_type_filter(db):
    """The cursor indexes the FILTERED list, so paging with a type filter stays consistent."""
    with db.write("t", "seed finding type") as tx:
        schemas.define_type(tx.cur, tx, "node", "finding", {"type": "object"}, status="active")
    for i in range(6):
        graph.upsert_node(db, "a", "component", {"title": f"mixed corpus component {i}"})
        graph.upsert_node(db, "a", "finding", {"title": f"mixed corpus finding {i}"})
    seen, cursor = [], 0
    while True:
        p = search.search(db, "mixed corpus", types=["finding"], limit=2, cursor=cursor)
        assert all(r["node_type"] == "finding" for r in p["results"])
        seen.extend(r["node_id"] for r in p["results"])
        if not p["has_more"]:
            break
        cursor = p["next_cursor"]
    assert len(seen) == len(set(seen)) == 6
