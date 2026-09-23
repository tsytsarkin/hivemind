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
