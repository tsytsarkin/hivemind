"""Identity is what the token says, not what the caller claims."""
import json

import httpx
import pytest
from conftest import Lifespan, _call, _post

from hivemind_server import graph
from hivemind_server.identity import Identity, set_identity


@pytest.fixture(autouse=True)
def as_nik():
    set_identity(Identity(user="nik", device="mac-studio"))
    yield
    set_identity(None)


def test_the_author_is_the_token_not_the_agent_argument(db):
    """`agent` used to be believed verbatim: agent="verify-1.0.0" was recorded as fact."""
    out = graph.upsert_node(db, "root", "component", {"title": "x"}, reason="claiming to be root")
    got = graph.get_node(db, node_id=out["node_id"])
    assert got["author"] == "nik"
    assert got["agent_label"] == "root"


def test_created_by_and_last_author_are_distinguished(db):
    a = graph.upsert_node(db, "job-a", "component", {"title": "x"}, reason="create")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "job-b", "component", {"title": "x2"},
                      node_id=a["node_id"], expected_head=a["version_id"], reason="revise")
    got = graph.get_node(db, node_id=a["node_id"])
    assert got["created_by"] == "nik"
    assert got["author"] == "ana"


def test_the_contributor_chain_names_everyone(db):
    out = graph.upsert_node(db, "j", "component", {"title": "v1"}, reason="create")
    head = out["version_id"]
    for user in ("ana", "bo", "cy", "di"):
        set_identity(Identity(user=user, device="x"))
        out = graph.upsert_node(db, "j", "component", {"title": f"v-{user}"},
                                node_id=out["node_id"], expected_head=head, reason="revise")
        head = out["version_id"]
    got = graph.get_node(db, node_id=out["node_id"])
    assert set(got["contributors"]) == {"nik", "ana", "bo", "cy", "di"}


def test_history_entries_carry_their_own_author(db):
    out = graph.upsert_node(db, "j", "component", {"title": "v1"}, reason="create")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "v2"}, node_id=out["node_id"],
                      expected_head=out["version_id"], reason="revise")
    hist = graph.get_node(db, node_id=out["node_id"], history=True)["history"]
    assert [h["author"] for h in hist] == ["ana", "nik"]


def test_an_unresolvable_identity_is_recorded_as_legacy_and_the_write_proceeds(db):
    """Blocking would take the fleet down mid-migration."""
    set_identity(None)
    out = graph.upsert_node(db, "cli", "component", {"title": "x"}, reason="no identity")
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:unknown"


def test_bulk_edges_are_attributed_through_tx_only(db):
    """edge_bulk has no version row, so there is no author_user column to fill."""
    a = graph.upsert_node(db, "j", "function", {"title": "a"}, reason="x")
    b = graph.upsert_node(db, "j", "function", {"title": "b"}, reason="x")
    graph.bulk_replace(db, "import-job", "calls", "kernelcache@x",
                       [[a["node_id"], b["node_id"], {}]])
    with db.read() as cur:
        rows = cur.execute(
            "SELECT agent_id, user_id FROM tx ORDER BY tx_id DESC LIMIT 1").fetchall()
    # The label is the caller's free-form string; the user is the token's. Both are asserted so a
    # regression that wrote the label into user_id cannot pass.
    assert rows[0][0] == "import-job"
    assert rows[0][1] == "nik"
    with db.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM edge_bulk").fetchone()[0] == 1


def test_the_device_is_recorded_alongside_the_user(db):
    graph.upsert_node(db, "j", "component", {"title": "x"}, reason="create")
    with db.read() as cur:
        row = cur.execute("SELECT user_id, device FROM tx ORDER BY tx_id DESC LIMIT 1").fetchone()
    assert (row["user_id"], row["device"]) == ("nik", "mac-studio")


def test_an_edge_version_carries_its_author(db):
    a = graph.upsert_node(db, "j", "component", {"title": "a"}, reason="x")["node_id"]
    b = graph.upsert_node(db, "j", "component", {"title": "b"}, reason="x")["node_id"]
    set_identity(Identity(user="ana", device="laptop"))
    e = graph.upsert_edge(db, "linker", "refines", a, b, {})
    with db.read() as cur:
        row = cur.execute("SELECT author_user FROM edge_version WHERE version_id=?",
                          (e["version_id"],)).fetchone()
    assert row["author_user"] == "ana"


def test_a_skill_a_trap_and_a_tool_all_record_the_user_beside_the_label(db, tmp_path):
    from hivemind_server import blobs, registry, skills, traps
    skills.publish(db, "publish-job", id="re/x", version="1.0.0", title="X",
                   description="a procedure for x", body="step 1\nstep 2")
    t = traps.record(db, "trap-job", title="dead end", what_failed="tried x", symptom="hung")
    store = blobs.BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)
    dig = store.put_stream([b"#!/bin/sh\n"], agent_id="upload-job")["digest"]
    registry.publish(db, "tool-job", {"id": "org.x/t", "version": "1.0.0", "runtime": "shell",
                                      "entrypoint": "t.sh"}, dig)
    with db.read() as cur:
        assert cur.execute("SELECT author, author_user FROM skill_version WHERE id='re/x'"
                           ).fetchone()[:] == ("publish-job", "nik")
        assert cur.execute("SELECT author, author_user FROM trap WHERE trap_id=?",
                           (t["trap_id"],)).fetchone()[:] == ("trap-job", "nik")
        assert cur.execute("SELECT author_user FROM tool_version WHERE id='org.x/t'"
                           ).fetchone()[0] == "nik"
        # A blob row has no author column of its own — content-addressed and deduplicated — so the
        # upload is attributed through its tx.
        assert cur.execute("SELECT user_id FROM tx WHERE tx_id=("
                           "SELECT created_tx FROM blob WHERE digest=?)", (dig,)).fetchone()[0] \
            == "nik"
    # ...and the read paths hand it back, so each names a person and not just a label.
    assert registry.resolve(db, "org.x/t")["author_user"] == "nik"
    assert skills.get(db, "re/x")["author_user"] == "nik"
    assert traps.get(db, t["trap_id"])["author_user"] == "nik"


def test_a_read_that_returns_no_version_has_no_author_at_all(db):
    """legacy:unknown means "written before authorship existed" — never "this row is absent".

    An as_of predating the node returns current=None, so claiming an anonymous author there would
    also disagree with agent_label, which is None for the same nonexistent row.
    """
    with db.read() as cur:
        before = cur.execute("SELECT MAX(tx_id) FROM tx").fetchone()[0]
    out = graph.upsert_node(db, "j", "component", {"title": "x"}, reason="create")
    got = graph.get_node(db, node_id=out["node_id"], as_of=before)
    assert got["current"] is None
    assert got["author"] is None
    assert got["agent_label"] is None
    # ...while the node-level facts, which are not about one version, still answer.
    assert got["created_by"] == "nik" and got["contributors"] == ["nik"]


def test_an_admin_cli_write_names_the_operator_not_legacy_unknown(projects_dir, monkeypatch):
    """legacy:unknown is for the principal-less paths; a human ran hivemind-admin.

    Drives admin.main() rather than _cli_identity() directly: the claim is about the WIRING, and a
    test that called the helper itself passed even with the set_identity() call deleted.
    """
    from hivemind_server import admin
    from hivemind_server.db import Database
    from hivemind_server.identity import USERNAME_RE
    monkeypatch.setattr(admin.getpass, "getuser", lambda: "opsperson")
    assert admin.main(["reindex"]) in (0, None)
    d = Database(projects_dir / "default" / "hivemind.db")
    with d.read() as cur:
        rows = [tuple(r) for r in cur.execute(
            "SELECT agent_id, user_id FROM tx ORDER BY tx_id DESC LIMIT 1")]
    assert rows[0] == ("reindex", "cli:opsperson")
    # The prefix is what makes it uncollidable with a real username, same as legacy:.
    assert not USERNAME_RE.fullmatch("cli:opsperson")


def test_the_admin_cli_does_not_leave_its_identity_set(projects_dir, monkeypatch):
    """The identity contextvar is process-wide. main() used to set it and never clear it, so any
    later write in the same process — the next test module collected, an embedded caller — was
    attributed to whoever ran the CLI."""
    from hivemind_server import admin
    from hivemind_server.identity import current_identity
    monkeypatch.setattr(admin.getpass, "getuser", lambda: "opsperson")
    assert admin.main(["reindex"]) in (0, None)
    assert current_identity() is None


def test_an_orphan_upload_names_the_person_not_just_the_job(db, tmp_path):
    """A 94 GB leak attributed only to a self-chosen label names a job, not anyone answerable."""
    from hivemind_server import blobs
    store = blobs.BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)
    store.put_stream([b"nobody attached me"], agent_id="upload-job")
    rows = store.orphans()["by_uploader"]
    assert [(r["user"], r["agent"], r["blobs"]) for r in rows] == [("nik", "upload-job", 1)]


def test_a_guide_proposal_records_the_user(db):
    from hivemind_server import guide
    p = guide.propose_section(db, "guide-job", "core", "body text", why="because")
    with db.read() as cur:
        row = cur.execute("SELECT agent_id, author_user FROM guide_proposal WHERE id=?",
                          (p["proposal_id"],)).fetchone()
    assert (row["agent_id"], row["author_user"]) == ("guide-job", "nik")


@pytest.mark.anyio
async def test_over_the_real_transport_the_agent_argument_cannot_claim_an_author(env):
    """The end-to-end shape of the property: a tool call that says agent="root" is still nik's.

    Pinned over HTTP because the whole design rests on the identity contextvar surviving the MCP
    transport into a db.write deep inside a tool body — an in-process set_identity() cannot show
    that.
    """
    application, proj, _ = env
    from hivemind_server.identity import IdentityStore
    tok = IdentityStore(application.state.cfg.identities_path).mint("nik", "mac-studio")
    transport = httpx.ASGITransport(app=application)
    base = f"/p/{proj.name}"
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        _call(await _post(c, base, tok, "tools/call", {"name": "schema_propose", "arguments": {
            "kind": "node", "name": "note", "json_schema": {"type": "object"},
            "agent": "root"}}, 2))
        up = _call(await _post(c, base, tok, "tools/call", {"name": "graph_upsert", "arguments": {
            "type": "note", "props": {"text": "hi"}, "agent": "root"}}, 3))
        got = _call(await _post(c, base, tok, "tools/call", {
            "name": "graph_get", "arguments": {"node_id": up["node_id"]}}, 4))
    assert got["agent_label"] == "root"
    assert got["author"] == "nik"
    assert got["created_by"] == "nik"
    assert got["contributors"] == ["nik"]


@pytest.mark.anyio
async def test_a_legacy_project_token_is_attributed_to_its_client_id(env):
    """The live fleet's own credential. It resolves to legacy:<client_id>, not legacy:unknown.

    legacy:unknown is for a write with NO identity at all (a CLI, a startup task). A deployed
    legacy token still names something, and the migration is only readable if the two are distinct.
    """
    application, proj, legacy_tok = env
    client_id = json.loads((proj.dir / "tokens.json").read_text())[legacy_tok]["client_id"]
    transport = httpx.ASGITransport(app=application)
    base = f"/p/{proj.name}"
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        _call(await _post(c, base, legacy_tok, "tools/call", {
            "name": "schema_propose",
            "arguments": {"kind": "node", "name": "note",
                          "json_schema": {"type": "object"}, "agent": "boot"}}, 2))
        up = _call(await _post(c, base, legacy_tok, "tools/call", {
            "name": "graph_upsert",
            "arguments": {"type": "note", "props": {"text": "hi"}, "agent": "boot"}}, 3))
        got = _call(await _post(c, base, legacy_tok, "tools/call", {
            "name": "graph_get", "arguments": {"node_id": up["node_id"]}}, 4))
    assert got["author"] == f"legacy:{client_id}"
    assert got["author"] != "legacy:unknown"
    assert got["agent_label"] == "boot"


# ── search: structured props instead of a truncated snippet ─────────────────────────
def test_fields_projects_only_the_named_keys(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "status": "open",
                                             "notes": "x" * 5000}, reason="x")
    hit = graph.search_nodes(db, "alpha", fields=["title", "status"])["results"][0]
    assert hit["props"] == {"title": "alpha", "status": "open"}
    assert "notes" not in hit["props"], "a field not asked for must not be shipped"
    assert "snippet" not in hit, "fields replaces the snippet rather than adding to it"


def test_a_missing_field_is_omitted_not_nulled(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha"}, reason="x")
    hit = graph.search_nodes(db, "alpha", fields=["title", "nope"])["results"][0]
    assert hit["props"] == {"title": "alpha"}


def test_props_true_returns_the_whole_dict(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "status": "open"}, reason="x")
    hit = graph.search_nodes(db, "alpha", props=True)["results"][0]
    assert hit["props"] == {"title": "alpha", "status": "open"}


def test_props_true_clamps_the_page_size(db):
    for i in range(15):
        graph.upsert_node(db, "j", "component", {"title": f"alpha {i}"}, reason="x")
    out = graph.search_nodes(db, "alpha", props=True, limit=25)
    assert len(out["results"]) == graph.PROPS_LIMIT == 10
    assert out["props_clamped"] is True
    assert out["has_more"] is True, "clamping must not look like the end of the results"
    assert out["next_cursor"] == 10, "...and the next page has to start where this one stopped"


def test_an_oversized_props_is_truncated_with_a_marker(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "big": "x" * 9000}, reason="x")
    out = graph.search_nodes(db, "alpha", props=True)
    hit = out["results"][0]
    assert hit["props"]["_truncated"] is True
    assert hit["props"]["_chars"] > graph.PROPS_MAX_CHARS
    assert len(json.dumps(hit["props"])) < 5000, "the whole point is a bounded payload"
    assert out["props_clamped"] is True, "a truncated hit is a clamped reply"


def test_an_oversized_projection_is_truncated_the_same_way(db):
    """fields is the cheap mode, but the caller picks the keys — one can hold a whole document."""
    graph.upsert_node(db, "j", "component", {"title": "alpha", "big": "x" * 9000}, reason="x")
    out = graph.search_nodes(db, "alpha", fields=["big"])
    assert out["results"][0]["props"]["_truncated"] is True
    assert len(json.dumps(out["results"][0]["props"])) < 5000
    assert out["props_clamped"] is True


def test_a_page_stops_when_the_props_budget_is_spent(db):
    """20 hits x 3000 chars would be a 60k-character reply; the page ends early and says so."""
    for i in range(20):
        graph.upsert_node(db, "j", "component", {"title": f"alpha {i}", "body": "y" * 3000},
                          reason="x")
    out = graph.search_nodes(db, "alpha", fields=["title", "body"], limit=25)
    assert 0 < len(out["results"]) < 20
    assert out["props_clamped"] is True
    assert out["has_more"] is True and out["next_cursor"] == len(out["results"])
    # ...and paging on from there is what returns the rest, rather than losing it.
    nxt = graph.search_nodes(db, "alpha", fields=["title"], limit=25, cursor=out["next_cursor"])
    seen = {h["node_id"] for h in out["results"]} | {h["node_id"] for h in nxt["results"]}
    assert len(seen) == 20


def test_the_default_shape_is_unchanged(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha"}, reason="x")
    hit = graph.search_nodes(db, "alpha")["results"][0]
    assert "snippet" in hit and "props" not in hit
    assert "props_clamped" not in graph.search_nodes(db, "alpha")


def test_fields_wins_over_props(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "status": "open"}, reason="x")
    hit = graph.search_nodes(db, "alpha", fields=["title"], props=True)["results"][0]
    assert hit["props"] == {"title": "alpha"}


# ── search: filter by author ────────────────────────────────────────────────────────
def test_search_filters_by_author(db):
    graph.upsert_node(db, "j", "component", {"title": "nik wrote this"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "ana wrote this"}, reason="x")

    nik_only = graph.search_nodes(db, "wrote", author="nik", fields=["title"])["results"]
    assert [r["props"]["title"] for r in nik_only] == ["nik wrote this"]
    assert len(graph.search_nodes(db, "wrote")["results"]) == 2


def test_the_author_filter_composes_with_types(db):
    graph.upsert_node(db, "j", "component", {"title": "c"}, reason="x")
    graph.upsert_node(db, "j", "finding", {"title": "f"}, reason="x")
    out = graph.search_nodes(db, "", types=["finding"], author="nik")["results"]
    assert len(out) == 1 and out[0]["node_type"] == "finding"


def test_an_author_with_no_writes_returns_nothing_rather_than_everything(db):
    graph.upsert_node(db, "j", "component", {"title": "c"}, reason="x")
    assert graph.search_nodes(db, "", author="nobody")["results"] == []


def test_fields_composes_with_the_author_filter(db):
    graph.upsert_node(db, "j", "component", {"title": "mine", "status": "open"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "theirs", "status": "open"}, reason="x")
    out = graph.search_nodes(db, "", author="nik", fields=["title"])["results"]
    assert [h["props"] for h in out] == [{"title": "mine"}]


def test_the_author_filter_is_applied_before_the_row_cap(db):
    """A filter applied after the row cap is the bug already fixed once for types."""
    graph.upsert_node(db, "j", "component", {"title": "the one nik wrote"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    for i in range(6):
        graph.upsert_node(db, "j", "component", {"title": f"ana {i}"}, reason="x")
    # Browsing pages the node table in SQL (newest first), so with limit=5 nik's node is outside
    # the rows a post-filter would ever see.
    out = graph.search_nodes(db, "", author="nik", limit=5, fields=["title"])
    assert [h["props"]["title"] for h in out["results"]] == ["the one nik wrote"]
    assert out["total_of_type"] == 1, "...and the count has to answer for the filter too"


def test_the_legacy_author_name_finds_the_rows_that_report_it(db):
    """A NULL author_user reads out as legacy:unknown, so that name has to match those rows."""
    out = graph.upsert_node(db, "j", "component", {"title": "predates authorship"}, reason="x")
    with db.write("migration-sim", "blank an author column, as a pre-authorship row has") as tx:
        tx.cur.execute("UPDATE node_version SET author_user=NULL WHERE node_id=?",
                       (out["node_id"],))
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:unknown"
    hits = graph.search_nodes(db, "", author="legacy:unknown", fields=["title"])["results"]
    assert [h["props"]["title"] for h in hits] == ["predates authorship"]


def test_skill_trap_and_tool_search_all_filter_by_author(db, tmp_path):
    from hivemind_server import blobs, registry, skills, traps
    store = blobs.BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)

    def publish(who, sid, title, desc, tid, entry, tool_desc, payload):
        skills.publish(db, "j", id=sid, version="1.0.0", title=title, description=desc,
                       body="step 1\nstep 2")
        traps.record(db, "j", title=f"{who} dead end", what_failed="tried it", symptom="hung")
        dig = store.put_stream([payload], agent_id="j")["digest"]
        registry.publish(db, "j", {"id": tid, "version": "1.0.0", "runtime": "shell",
                                   "entrypoint": entry, "description": tool_desc}, dig)

    publish("nik", "ops/restart-the-box", "Restart the box",
            "power-cycle the lab machine cleanly", "org.x/restart", "restart.sh",
            "reboots a machine and waits for it", b"#!/bin/sh\n1")
    set_identity(Identity(user="ana", device="laptop"))
    publish("ana", "net/trace-a-socket", "Trace a socket",
            "watch traffic on one file descriptor", "org.x/trace", "trace.sh",
            "prints every packet crossing a helper port", b"#!/bin/sh\n2")

    # browse (no query)
    assert [s["id"] for s in skills.search(db, author="nik")["skills"]] == ["ops/restart-the-box"]
    assert [t["title"] for t in traps.search(db, author="nik")["traps"]] == ["nik dead end"]
    assert [t["id"] for t in registry.search(db, author="nik")["tools"]] == ["org.x/restart"]
    # ...and the query path, which draws its candidates from FTS (and embeddings) instead
    assert [s["id"] for s in skills.search(db, "socket", author="ana")["skills"]] == \
        ["net/trace-a-socket"]
    assert [t["title"] for t in traps.search(db, "dead end", author="ana")["traps"]] == \
        ["ana dead end"]
    assert [t["id"] for t in registry.search(db, "helper", author="ana")["tools"]] == \
        ["org.x/trace"]
    # an author who wrote none of them gets nothing, not everything
    assert skills.search(db, author="nobody")["count"] == 0
    assert traps.search(db, "dead end", author="nobody")["count"] == 0
    assert registry.search(db, "helper", author="nobody")["count"] == 0


@pytest.mark.anyio
async def test_over_the_real_transport_a_search_projects_props_and_filters_by_author(env):
    """The parameters have to reach the TOOL: its input schema is built from the signature, so a
    parameter missing there cannot be passed at all, however well the library supports it."""
    application, proj, _ = env
    from hivemind_server.identity import IdentityStore
    store = IdentityStore(application.state.cfg.identities_path)
    nik, ana = store.mint("nik", "mac-studio"), store.mint("ana", "laptop")
    transport = httpx.ASGITransport(app=application)
    base = f"/p/{proj.name}"
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        _call(await _post(c, base, nik, "tools/call", {"name": "schema_propose", "arguments": {
            "kind": "node", "name": "note", "json_schema": {"type": "object"}}}, 2))
        for tok, who in ((nik, "nik"), (ana, "ana")):
            _call(await _post(c, base, tok, "tools/call", {"name": "graph_upsert", "arguments": {
                "type": "note", "props": {"title": f"{who} wrote this", "bulk": "x" * 500}}}, 3))
        out = _call(await _post(c, base, nik, "tools/call", {"name": "graph_search", "arguments": {
            "query": "wrote", "fields": ["title"], "author": "nik"}}, 4))
        every = _call(await _post(c, base, nik, "tools/call", {"name": "graph_search",
                                                              "arguments": {"query": "wrote"}}, 5))
    assert [h["props"] for h in out["results"]] == [{"title": "nik wrote this"}]
    assert len(every["results"]) == 2 and "snippet" in every["results"][0]


def test_semantic_search_with_an_author_is_not_a_post_filter_over_the_vector_pool(db):
    """mode="semantic" drew its candidates from the vector index, which took no author filter, so
    the author was applied to the top limit*3 rows AFTER the cut: a rare author's skill sat outside
    the pool and the reply was empty while a lexical search on the same query returned it."""
    from hivemind_server import registry, skills
    set_identity(Identity(user="ana", device="laptop"))
    for i in range(70):                       # 70 > the limit*3 = 60 candidate pool
        skills.publish(db, "j", id=f"pack/socket-{i}", version="1.0.0",
                       title=f"socket socket {i}", description="socket socket socket socket",
                       body="socket socket socket", force=True)
    set_identity(Identity(user="nik", device="mac-studio"))
    # One of nik's is the single best vector match in the whole library, the other two are far
    # enough down to fall outside the pool. That combination is what separates "ranked WITHIN this
    # author's items" from "ranked globally, then filtered": the latter returns the one and loses
    # the two, and cannot be rescued by the empty-semantic fallback to lexical.
    skills.publish(db, "j", id="socket", version="1.0.0", title="socket", description="socket",
                   body="socket", force=True)
    for name in ("rare/one-socket", "rare/socket-aside"):
        skills.publish(db, "j", id=name, version="1.0.0", title="one socket note",
                       description="a socket mentioned once among unrelated words",
                       body="cluster orbit lantern socket meadow trombone glacier", force=True)

    got = {m: sorted(s["id"] for s in skills.search(db, "socket", mode=m, author="nik")["skills"])
           for m in ("lexical", "semantic", "hybrid")}
    assert got["lexical"] == ["rare/one-socket", "rare/socket-aside", "socket"]
    assert got["semantic"] == got["lexical"], "the vector pool must not hide the rare author"
    assert got["hybrid"] == got["lexical"]
    # ...and the filter still excludes: asking for the prolific author never returns nik's.
    assert "rare/one-socket" not in [
        s["id"] for s in skills.search(db, "socket", mode="semantic", author="ana",
                                       limit=100)["skills"]]


def test_semantic_tool_search_with_an_author_is_not_a_post_filter_either(db, tmp_path):
    from hivemind_server import blobs, registry
    store = blobs.BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)
    set_identity(Identity(user="ana", device="laptop"))
    for i in range(80):                       # 80 > the limit*3 = 75 pool at limit=25
        dig = store.put_stream([f"#!/bin/sh\n{i}".encode()], agent_id="j")["digest"]
        registry.publish(db, "j", {"id": f"org.x/socket-{i}", "version": "1.0.0",
                                   "runtime": "shell", "entrypoint": "s.sh",
                                   "description": "socket socket socket socket"}, dig, force=True)
    set_identity(Identity(user="nik", device="mac-studio"))
    # As in the skill case: one best-in-library match plus two that fall outside the pool, so a
    # filter applied after the top-N cut loses the two instead of returning nothing and falling
    # back to lexical.
    dig = store.put_stream([b"#!/bin/sh\nbest"], agent_id="j")["digest"]
    registry.publish(db, "j", {"id": "socket", "version": "1.0.0", "runtime": "shell",
                               "entrypoint": "b.sh", "description": "socket"}, dig, force=True)
    for name in ("org.rare/one-socket", "org.rare/socket-aside"):
        dig = store.put_stream([f"#!/bin/sh\n{name}".encode()], agent_id="j")["digest"]
        registry.publish(db, "j", {"id": name, "version": "1.0.0", "runtime": "shell",
                                   "entrypoint": "r.sh",
                                   "description": "a socket mentioned once among unrelated words"},
                         dig, force=True)
    got = {m: sorted(t["id"] for t in registry.search(db, "socket", mode=m,
                                                      author="nik")["tools"])
           for m in ("lexical", "semantic", "hybrid")}
    assert got["lexical"] == ["org.rare/one-socket", "org.rare/socket-aside", "socket"]
    assert got["semantic"] == got["lexical"] and got["hybrid"] == got["lexical"]


def test_the_reply_names_which_bound_shortened_it(db):
    """One boolean for three different bounds left the caller guessing which to answer."""
    for i in range(15):
        graph.upsert_node(db, "j", "component", {"title": f"alpha {i}", "big": "x" * 9000},
                          reason="x")
    out = graph.search_nodes(db, "alpha", props=True, limit=25)
    assert out["props_clamped"] is True
    assert set(out["props_clamped_by"]) == {"page_limit", "node_truncated"}
    # the budget is its own reason, and it is the one a fields= page hits
    fld = graph.search_nodes(db, "alpha", fields=["big"], limit=25)
    assert fld["props_clamped_by"] == ["node_truncated", "page_budget"]
    # ...and an unclamped page in the same mode says nothing at all
    graph.upsert_node(db, "j", "component", {"title": "beta"}, reason="x")
    assert "props_clamped_by" not in graph.search_nodes(db, "beta", fields=["title"])


def test_a_hit_in_the_new_modes_names_its_author(db):
    """`author` is on the version row, never in props, so fields= cannot project it — and browsing
    BY author is exactly the case that needs to see whose each hit is."""
    graph.upsert_node(db, "j", "component", {"title": "mine"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "theirs"}, reason="x")
    by_author = {h["props"]["title"]: h["author"]
                 for h in graph.search_nodes(db, "", fields=["title"])["results"]}
    assert by_author == {"mine": "nik", "theirs": "ana"}
    assert graph.search_nodes(db, "", props=True)["results"][0]["author"] == "ana"
    # the default reply shape stays exactly as it was: no props, no author, just the snippet
    assert "author" not in graph.search_nodes(db, "")["results"][0]


# ── backfilling the rows that predate authorship ────────────────────────────────

# Every authorship column the identity migration added, as (table, column). A row is in scope for
# the backfill only while its column is NULL — see _blank_every_author_column.
_AUTHOR_COLUMNS = (("node_version", "author_user"), ("edge_version", "author_user"),
                   ("node", "created_by"), ("skill_version", "author_user"),
                   ("trap", "author_user"), ("tool_version", "author_user"),
                   ("guide_proposal", "author_user"))


def _blank_every_author_column(db):
    """Make every authorship column look the way it does on a row written before the column was."""
    with db.write_light() as cur:
        for table, col in _AUTHOR_COLUMNS:
            cur.execute(f"UPDATE {table} SET {col}=NULL")


def _authors(db, table, col):
    with db.read() as cur:
        return sorted({r[0] for r in cur.execute(f"SELECT {col} FROM {table}")},
                      key=lambda v: (v is not None, v))


def test_backfill_attributes_pre_identity_writes_to_legacy_never_to_a_person(db):
    """Attributing old self-declared strings to `nik` would be inventing provenance."""
    from hivemind_server.admin import backfill_authors
    set_identity(None)
    out = graph.upsert_node(db, "cli", "component", {"title": "old"}, reason="pre-identity")
    with db.write_light() as cur:          # simulate a row written before the column existed
        cur.execute("UPDATE node_version SET author_user=NULL WHERE node_id=?", (out["node_id"],))

    report = backfill_authors(db, dry_run=True)
    assert report["would_update"] >= 1
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:unknown"

    backfill_authors(db, dry_run=False)
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:cli"
    assert "nik" not in str(report)


def test_a_dry_run_reports_per_table_and_writes_nothing(db):
    """At this scale the report is the only thing an operator can check before committing to it."""
    from hivemind_server.admin import backfill_authors
    graph.upsert_node(db, "atlas-migration", "component", {"title": "old"}, reason="x")
    _blank_every_author_column(db)
    report = backfill_authors(db, dry_run=True)
    assert report["dry_run"] is True and "updated" not in report
    assert report["node_versions"] == 1 and report["nodes"] == 1
    assert report["would_update"] == sum(report[t + "s"] for t, _ in _AUTHOR_COLUMNS)
    assert _authors(db, "node_version", "author_user") == [None]
    assert _authors(db, "node", "created_by") == [None]
    # ...and it says so in words too, because the form that does nothing is the default one
    assert "nothing was written" in report["hint"]
    assert "hint" not in backfill_authors(db, dry_run=False)


def test_running_the_backfill_twice_does_not_double_prefix(db):
    """A second pass must not turn `legacy:cli` into `legacy:legacy:cli`.

    It cannot, and for a better reason than the NULL filter: the value is computed from the tx's
    agent label and never from the column being written, so recomputing it over a row that is
    already filled yields the identical string. Pinned over the mixed state an interrupted sweep
    leaves behind, since that is where a filled row meets a running sweep. (What the NULL filter
    protects is the values this command did not write — see the "left alone" test.)
    """
    from hivemind_server.admin import backfill_authors
    out = graph.upsert_node(db, "cli", "component", {"title": "old"}, reason="x")
    other = graph.upsert_node(db, "cli", "component", {"title": "older"}, reason="x")
    _blank_every_author_column(db)
    with db.write_light() as cur:      # as a sweep killed between batches leaves it
        cur.execute("UPDATE node_version SET author_user=\'legacy:cli\' WHERE node_id=?",
                    (other["node_id"],))
    first = backfill_authors(db, dry_run=False)
    again = backfill_authors(db, dry_run=False)
    assert first["updated"] >= 2 and again["updated"] == 0
    assert again["dry_run"] is False
    assert _authors(db, "node_version", "author_user") == ["legacy:cli"]
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:cli"
    # The one doubled prefix that IS reachable: a label that declared itself legacy. Recorded as
    # `legacy:legacy:cli` on purpose — the prefix says what the server can vouch for, and this
    # label was still just a string its writer chose. It is no more mintable than any other.
    claimed = graph.upsert_node(db, "legacy:cli", "component", {"title": "self-declared"},
                                reason="x")
    with db.write_light() as cur:
        cur.execute("UPDATE node_version SET author_user=NULL WHERE node_id=?",
                    (claimed["node_id"],))
    backfill_authors(db, dry_run=False)
    assert graph.get_node(db, node_id=claimed["node_id"])["author"] == "legacy:legacy:cli"


def test_rows_written_since_authorship_landed_are_left_alone(db):
    """NULL and the literal legacy:unknown must stay distinguishable: NULL predates the column,
    while the literal is a write that DID happen since, by no resolvable principal. Only NULL is
    the backfill's business — rewriting the rest would relabel writes whose author is already
    recorded."""
    from hivemind_server.admin import backfill_authors
    mine = graph.upsert_node(db, "cli", "component", {"title": "nik wrote this"}, reason="x")
    set_identity(None)
    nobody = graph.upsert_node(db, "cli", "component", {"title": "no principal"}, reason="x")
    # A third row that really does predate the column, so the sweep has work and actually runs:
    # with nothing to do it returns before opening a transaction, and a filter-less UPDATE would
    # then never get the chance to relabel the two rows above.
    older = graph.upsert_node(db, "atlas-migration", "component", {"title": "predates"},
                              reason="x")
    with db.write_light() as cur:
        cur.execute("UPDATE node_version SET author_user=NULL WHERE node_id=?",
                    (older["node_id"],))
        cur.execute("UPDATE node SET created_by=NULL WHERE node_id=?", (older["node_id"],))

    assert backfill_authors(db, dry_run=True)["would_update"] == 2
    assert backfill_authors(db, dry_run=False)["updated"] == 2
    assert graph.get_node(db, node_id=mine["node_id"])["author"] == "nik"
    assert graph.get_node(db, node_id=nobody["node_id"])["author"] == "legacy:unknown"
    assert graph.get_node(db, node_id=mine["node_id"])["created_by"] == "nik"
    assert graph.get_node(db, node_id=older["node_id"])["author"] == "legacy:atlas-migration"


def test_a_missing_tx_or_a_blank_agent_label_still_gets_an_honest_author(db):
    """`legacy:None` is a lie dressed as a name, and a crash mid-sweep would leave the fleet's
    provenance half-written."""
    from hivemind_server.admin import backfill_authors
    orphan = graph.upsert_node(db, "cli", "component", {"title": "its tx is gone"}, reason="x")
    blank = graph.upsert_node(db, "   ", "component", {"title": "blank label"}, reason="x")
    tabbed = graph.upsert_node(db, "\t\n", "component", {"title": "tab label"}, reason="x")
    _blank_every_author_column(db)
    # FK enforcement is per-connection and a PRAGMA inside a transaction is a no-op, so this goes
    # on the connection first. It simulates the one row shape the SQL has to survive: a version
    # whose tx row is not there any more.
    db.conn().execute("PRAGMA foreign_keys=OFF")
    try:
        with db.write_light() as cur:
            cur.execute("DELETE FROM tx WHERE tx_id=(SELECT tx_from FROM node_version "
                        "WHERE node_id=?)", (orphan["node_id"],))
    finally:
        db.conn().execute("PRAGMA foreign_keys=ON")

    backfill_authors(db, dry_run=False)
    assert graph.get_node(db, node_id=orphan["node_id"])["author"] == "legacy:unknown"
    assert graph.get_node(db, node_id=blank["node_id"])["author"] == "legacy:unknown"
    # SQLite's one-argument TRIM strips U+0020 only, so this label used to survive as the literal
    # `legacy:<tab><newline>` while the space-only one next to it read as blank.
    assert graph.get_node(db, node_id=tabbed["node_id"])["author"] == "legacy:unknown"
    assert "None" not in str(_authors(db, "node_version", "author_user"))
    assert None not in _authors(db, "node_version", "author_user")


def test_nothing_the_backfill_writes_could_be_mistaken_for_a_real_identity(db):
    """The live graph's agent labels are self-declared strings and some of them ARE usernames, so
    a row whose label was "nik" must not come out attributable to the person nik."""
    from hivemind_server.admin import backfill_authors
    from hivemind_server.identity import USERNAME_RE
    labels = ("nik", "root", "cli", "atlas-migration", "B6-FENCECENSUS", "verify-1.0.0", "   ")
    made = [graph.upsert_node(db, label, "component", {"title": label}, reason="x")
            for label in labels]
    _blank_every_author_column(db)
    backfill_authors(db, dry_run=False)

    wrote = _authors(db, "node_version", "author_user") + _authors(db, "node", "created_by")
    assert len(wrote) == 2 * len(set(labels))
    assert all(v is not None and not USERNAME_RE.fullmatch(v) for v in wrote), wrote
    assert graph.get_node(db, node_id=made[0]["node_id"])["author"] == "legacy:nik"


def test_the_backfill_reaches_every_authorship_column(db, tmp_path):
    """Each table gets the label from ITS OWN tx, so a copied-and-not-edited statement fails."""
    from hivemind_server import blobs, guide, registry, skills, traps
    from hivemind_server.admin import backfill_authors
    a = graph.upsert_node(db, "graph-job", "component", {"title": "a"}, reason="x")["node_id"]
    b = graph.upsert_node(db, "graph-job", "component", {"title": "b"}, reason="x")["node_id"]
    graph.upsert_edge(db, "edge-job", "refines", a, b, {})
    skills.publish(db, "skill-job", id="re/x", version="1.0.0", title="X",
                   description="a procedure for x", body="step 1")
    traps.record(db, "trap-job", title="dead end", what_failed="tried x", symptom="hung")
    store = blobs.BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)
    dig = store.put_stream([b"#!/bin/sh\n"], agent_id="tool-job")["digest"]
    registry.publish(db, "tool-job", {"id": "org.x/t", "version": "1.0.0", "runtime": "shell",
                                      "entrypoint": "t.sh"}, dig)
    guide.propose_section(db, "guide-job", "core", "body text", why="because")
    _blank_every_author_column(db)

    report = backfill_authors(db, dry_run=False)
    assert report["updated"] == sum(report[t + "s"] for t, _ in _AUTHOR_COLUMNS) > 6
    assert {table: _authors(db, table, col) for table, col in _AUTHOR_COLUMNS} == {
        "node_version": ["legacy:graph-job"], "edge_version": ["legacy:edge-job"],
        "node": ["legacy:graph-job"], "skill_version": ["legacy:skill-job"],
        "trap": ["legacy:trap-job"], "tool_version": ["legacy:tool-job"],
        "guide_proposal": ["legacy:guide-job"]}


def test_the_cli_backfill_is_a_dry_run_unless_it_is_told_otherwise(projects_dir, monkeypatch,
                                                                  capsys):
    """An operator who ran it to see what it would do cannot undo 380k rewritten author columns,
    so the writing form is the one you have to ask for — as with `gc --yes`."""
    from hivemind_server import admin, skills
    from hivemind_server.db import Database
    monkeypatch.setattr(admin.getpass, "getuser", lambda: "opsperson")
    assert admin.main(["reindex"]) in (0, None)          # lays the project down on disk
    d = Database(projects_dir / "default" / "hivemind.db")
    skills.publish(d, "old-job", id="re/x", version="1.0.0", title="X",
                   description="a procedure for x", body="step 1")
    with d.write_light() as cur:
        cur.execute("UPDATE skill_version SET author_user=NULL")
    capsys.readouterr()

    assert admin.main(["backfill-authors"]) in (0, None)
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["skill_versions"] == 1
    assert skills.get(d, "re/x")["author_user"] == "legacy:unknown"

    # ...and asking for both forms at once resolves to the one that cannot be undone by mistake
    assert admin.main(["backfill-authors", "--dry-run", "--yes"]) in (0, None)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert skills.get(d, "re/x")["author_user"] == "legacy:unknown"

    assert admin.main(["backfill-authors", "--yes"]) in (0, None)
    done = json.loads(capsys.readouterr().out)
    assert done["dry_run"] is False and done["updated"] == 1
    assert skills.get(d, "re/x")["author_user"] == "legacy:old-job"


def test_the_sweep_crosses_batch_boundaries(db):
    """Every other test here fits inside one batch, so the LIMIT binding, the running total and
    the loop bound each run exactly once — and a sweep that quietly stopped after its first batch
    would satisfy all of them."""
    from hivemind_server.admin import backfill_authors
    made = [graph.upsert_node(db, f"job-{i}", "component", {"title": f"n{i}"}, reason="x")
            for i in range(5)]
    _blank_every_author_column(db)

    report = backfill_authors(db, dry_run=False, batch=2)   # 5 rows = 3 passes, not 1
    assert report["node_versions"] == 5 and report["nodes"] == 5
    assert report["updated"] == 10
    assert _authors(db, "node_version", "author_user") == [f"legacy:job-{i}" for i in range(5)]
    assert _authors(db, "node", "created_by") == [f"legacy:job-{i}" for i in range(5)]
    assert backfill_authors(db, dry_run=True, batch=2)["would_update"] == 0
    assert graph.get_node(db, node_id=made[4]["node_id"])["author"] == "legacy:job-4"


def test_the_backfill_covers_every_authorship_column_the_migration_adds(db):
    """Three hand-written lists have to agree, and none of them is derived from another: the
    migration in db.py is the ground truth, admin's is what the sweep walks, and this module's is
    what the tests blank. An eighth column added to one and not the others is exactly the defect
    this task found in its own brief — a command that skips columns nobody notices."""
    from hivemind_server.admin import _AUTHOR_COLUMNS as swept
    from hivemind_server.db import Database
    migrated = {(t, c) for t, c, _ in Database._MIGRATIONS
                if c in ("author_user", "created_by", "user_id")}
    # tx.user_id is the one authorship column the sweep deliberately leaves alone: a tx row
    # already carries agent_id beside it, so 'legacy:' || agent_id there restates its neighbour.
    assert migrated - {("tx", "user_id")} == {(t, c) for t, c, _ in swept} == set(_AUTHOR_COLUMNS)


def test_a_re_run_on_a_filled_database_takes_no_write_lock(db, monkeypatch):
    """The command is meant to be safe to repeat, and on the live database repeating it would
    otherwise open the single writer lock once per table to change nothing."""
    from hivemind_server.admin import backfill_authors
    graph.upsert_node(db, "cli", "component", {"title": "old"}, reason="x")
    _blank_every_author_column(db)
    backfill_authors(db, dry_run=False)

    opened = []
    real = db.write_light
    monkeypatch.setattr(db, "write_light", lambda: (opened.append(1), real())[1])
    assert backfill_authors(db, dry_run=False)["updated"] == 0
    assert opened == []
