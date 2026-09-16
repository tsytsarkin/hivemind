"""Agent bus tests.

The properties worth defending here are the ones that break silently in production: that exactly
one claimant wins a contested request, that a watcher peeking cannot swallow a message on the
agent's behalf, that an expired session stops being offered work, and that a cursor never rewinds.
"""
import concurrent.futures as cf
import time

import pytest
from hivemind_server import bus, graph, schemas
from hivemind_server.db import Database, Invalid, NotFound


@pytest.fixture()
def bdb(tmp_path):
    """A project with NO schema at all — the bus must not depend on any graph vocabulary."""
    return Database(tmp_path / "bus.db")


@pytest.fixture()
def gdb(tmp_path):
    """A project that also has a graph, for the reference tests."""
    d = Database(tmp_path / "busgraph.db")
    with d.write("test-setup", "seed types") as tx:
        obj = {"type": "object", "additionalProperties": True}
        schemas.define_type(tx.cur, tx, "node", "thing", obj, status="active")
        schemas.define_type(tx.cur, tx, "edge", "relates", obj, status="active",
                            traits={"versioned": True})
    return d


def _hello(db, label, caps=None, **kw):
    return bus.hello(db, label=label, capabilities=caps or {}, **kw)


# ── sessions and capabilities ─────────────────────────────────────────────────────

def test_identity_is_per_session_not_per_token(bdb):
    """Two agents behind one token are two identities with disjoint capabilities."""
    a = _hello(bdb, "phone-box", {"device.iphone.attached": {"udid": "abc"}})
    b = _hello(bdb, "browser-box", {"browser.cdp": {}})
    assert a["session_id"] != b["session_id"]

    phones = bus.sessions(bdb, capability="device.iphone.attached")["sessions"]
    assert [s["session_id"] for s in phones] == [a["session_id"]]
    assert phones[0]["capabilities"]["device.iphone.attached"] == {"udid": "abc"}

    browsers = bus.sessions(bdb, capability="browser.cdp")["sessions"]
    assert [s["session_id"] for s in browsers] == [b["session_id"]]


def test_capability_prefix_match_is_strict(bdb):
    s = _hello(bdb, "w", {"browser.cdp.headless": {}})
    sid = s["session_id"]
    assert bus.sessions(bdb, capability="browser.*")["count"] == 1
    assert bus.sessions(bdb, capability="browser.cdp.headless")["count"] == 1
    # a bare prefix without '*' is NOT a prefix match — eligibility must be predictable
    assert bus.sessions(bdb, capability="browser")["count"] == 0
    assert bus.sessions(bdb, capability="browser.cdp")["count"] == 0
    assert sid


def test_capability_pattern_cannot_be_smuggled_via_like_wildcards(bdb):
    _hello(bdb, "w", {"browser.cdp": {}})
    # '%' is a LIKE wildcard; it must be escaped to a literal, not match everything
    assert bus.sessions(bdb, capability="%")["count"] == 0
    assert bus.sessions(bdb, capability="%*")["count"] == 0


def test_capability_index_counts_only_live_sessions(bdb):
    a = _hello(bdb, "a", {"browser.cdp": {}})
    _hello(bdb, "b", {"browser.cdp": {}})
    assert bus.capability_index(bdb)["capabilities"] == [{"name": "browser.cdp", "sessions": 2}]
    bus.bye(bdb, a["session_id"])
    assert bus.capability_index(bdb)["capabilities"] == [{"name": "browser.cdp", "sessions": 1}]


def test_expired_session_is_rejected_and_leaves_directory(bdb):
    s = _hello(bdb, "brief", {"x.y": {}}, ttl=-1)      # already past its deadline
    with pytest.raises(Invalid, match="expired"):
        bus.post(bdb, sender=s["session_id"], body="hi")
    assert bus.sessions(bdb)["count"] == 0
    assert bus.capability_index(bdb)["count"] == 0


def test_ping_replaces_the_whole_capability_set(bdb):
    s = _hello(bdb, "a", {"old.cap": {}})
    bus.ping(bdb, s["session_id"], capabilities={"new.cap": {}})
    caps = bus.sessions(bdb)["sessions"][0]["capabilities"]
    assert set(caps) == {"new.cap"}


def test_polling_keeps_a_session_alive_without_pinging(bdb):
    """Working is a heartbeat. Otherwise an agent that never stops draining still gets reaped."""
    s = _hello(bdb, "worker", ttl=2)
    before = bus.sessions(bdb)["sessions"][0]["expires_at"]
    time.sleep(1.1)
    bus.poll(bdb, s["session_id"])
    after = bus.sessions(bdb)["sessions"][0]["expires_at"]
    assert after > before
    time.sleep(1.1)                                    # past the ORIGINAL deadline
    assert bus.sessions(bdb)["count"] == 1             # still listed, on the poll's extension
    bus.post(bdb, sender=s["session_id"], body="still here")


def test_poll_extends_by_the_sessions_own_ttl_not_a_default(bdb):
    """A session that asked for a long ttl must not be downgraded by its own poll."""
    s = _hello(bdb, "long-lived", ttl=4000)
    bus.poll(bdb, s["session_id"])
    row = bus.sessions(bdb)["sessions"][0]
    assert row["expires_at"] > s["expires_at"]         # extended, and by the 4000s it asked for,
    assert row["expires_at"] > bus._ts(3000)           # not by the 900s default

    bus.ping(bdb, s["session_id"], ttl=60)             # a ping re-sets the ttl poll will use
    bus.poll(bdb, s["session_id"])
    assert bus.sessions(bdb)["sessions"][0]["expires_at"] < bus._ts(3000)


def test_poll_does_not_resurrect_an_already_expired_session(bdb):
    """Extending on activity must not become a way to un-expire something the reap should take.

    The wording is loose because poll reaps before it reads: whether the session is still merely
    'expired' or has already been reaped to 'ended' depends on the rate-limited _maybe_reap, and
    both are the same refusal.
    """
    s = _hello(bdb, "brief", ttl=-1)
    with pytest.raises(Invalid, match="expired"):
        bus.poll(bdb, s["session_id"])
    assert bus.sessions(bdb)["count"] == 0


def test_hello_rejects_unusable_capability_shapes(bdb):
    with pytest.raises(Invalid, match="space-free"):
        _hello(bdb, "a", {"has iphone": {}})
    with pytest.raises(Invalid, match="attrs must be an object"):
        _hello(bdb, "a", {"a.b": "not-a-dict"})
    with pytest.raises(Invalid, match="label is required"):
        _hello(bdb, "   ")


# ── messages and cursors ──────────────────────────────────────────────────────────

def test_new_session_starts_at_head_and_gets_no_backlog(bdb):
    a = _hello(bdb, "a")
    bus.post(bdb, sender=a["session_id"], body="before you arrived")
    b = _hello(bdb, "b")
    assert bus.poll(bdb, b["session_id"])["count"] == 0


def test_direct_message_reaches_only_its_addressee(bdb):
    a, b, c = (_hello(bdb, n) for n in "abc")
    bus.post(bdb, sender=a["session_id"], body="just for b", to_session=b["session_id"])
    assert [m["body"] for m in bus.poll(bdb, b["session_id"])["messages"]] == ["just for b"]
    assert bus.poll(bdb, c["session_id"])["count"] == 0


def test_room_broadcast_requires_membership(bdb):
    a, b = _hello(bdb, "a", rooms=["build"]), _hello(bdb, "b")
    bus.post(bdb, sender=a["session_id"], body="in build", room="build")
    assert bus.poll(bdb, b["session_id"])["count"] == 0
    bus.join(bdb, b["session_id"], "build")
    # joining must not retroactively deliver: an empty poll already marked b caught up to head,
    # so a join cannot dump the room's backlog into the next poll
    bus.post(bdb, sender=a["session_id"], body="after join", room="build")
    assert [m["body"] for m in bus.poll(bdb, b["session_id"])["messages"]] == ["after join"]


def test_empty_poll_advances_the_cursor_to_head(bdb):
    """Otherwise a stale cursor makes later-visible traffic retroactively deliverable."""
    a, b = _hello(bdb, "a", rooms=["build"]), _hello(bdb, "b")
    bus.post(bdb, sender=a["session_id"], body="invisible to b", room="build")
    got = bus.poll(bdb, b["session_id"])
    assert got["count"] == 0
    assert got["next_cursor"] == bus.stats(bdb)["head_seq"]


def test_truncated_poll_does_not_skip_the_remainder(bdb):
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    for i in range(5):
        bus.post(bdb, sender=a["session_id"], body=f"m{i}", to_session=b["session_id"])
    first = bus.poll(bdb, b["session_id"], limit=2)
    assert [m["body"] for m in first["messages"]] == ["m0", "m1"] and first["has_more"] is True
    rest = bus.poll(bdb, b["session_id"], limit=10)
    assert [m["body"] for m in rest["messages"]] == ["m2", "m3", "m4"]


def test_history_reads_a_room_without_touching_the_cursor(bdb):
    a, b = _hello(bdb, "a", rooms=["build"]), _hello(bdb, "b")
    for i in range(3):
        bus.post(bdb, sender=a["session_id"], body=f"m{i}", room="build")
    bus.post(bdb, sender=a["session_id"], body="private", to_session=b["session_id"])

    before = bus.peek(bdb, b["session_id"])["cursor"]
    h = bus.history(bdb, "build")
    assert [m["body"] for m in h["messages"]] == ["m0", "m1", "m2"]   # oldest-first
    assert "private" not in [m["body"] for m in h["messages"]]        # DMs are not room record
    assert bus.peek(bdb, b["session_id"])["cursor"] == before

    page = bus.history(bdb, "build", limit=2)
    assert [m["body"] for m in page["messages"]] == ["m1", "m2"] and page["has_more"] is True
    assert [m["body"] for m in bus.history(bdb, "build", limit=2,
                                           before=page["oldest_seq"])["messages"]] == ["m0"]


def test_own_messages_are_excluded_unless_asked_for(bdb):
    a = _hello(bdb, "a")
    bus.post(bdb, sender=a["session_id"], body="mine")
    assert bus.poll(bdb, a["session_id"])["count"] == 0
    assert bus.peek(bdb, a["session_id"], after=0, include_self=True)["count"] == 1


def test_peek_does_not_consume_but_poll_does(bdb):
    """The watcher must not be able to eat a message the agent has not read."""
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    bus.post(bdb, sender=a["session_id"], body="wake up", to_session=b["session_id"])

    for _ in range(3):
        seen = bus.peek(bdb, b["session_id"])
        assert seen["count"] == 1 and seen["consumed"] is False

    got = bus.poll(bdb, b["session_id"])
    assert got["count"] == 1 and got["consumed"] is True
    assert bus.poll(bdb, b["session_id"])["count"] == 0     # now it is drained


def test_cursor_never_rewinds(bdb):
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    for i in range(3):
        bus.post(bdb, sender=a["session_id"], body=f"m{i}", to_session=b["session_id"])
    drained = bus.poll(bdb, b["session_id"])["next_cursor"]

    # an explicit `after` in the past re-reads, but must not move the stored cursor backwards
    replay = bus.poll(bdb, b["session_id"], after=0)
    assert replay["count"] == 3
    assert bus.poll(bdb, b["session_id"])["count"] == 0
    assert bus.ack(bdb, b["session_id"], 1)["cursor"] == drained


def test_post_rejects_bad_input(bdb):
    a = _hello(bdb, "a")
    with pytest.raises(Invalid, match="kind must be"):
        bus.post(bdb, sender=a["session_id"], body="x", kind="nonsense")
    with pytest.raises(Invalid, match="body is required"):
        bus.post(bdb, sender=a["session_id"], body="  ")
    with pytest.raises(NotFound, match="to address"):
        bus.post(bdb, sender=a["session_id"], body="x", to_session="nope")


# ── open-claim dispatch ───────────────────────────────────────────────────────────

def test_request_notifies_every_matching_session_and_not_others(bdb):
    asker = _hello(bdb, "asker")
    b1 = _hello(bdb, "browser-1", {"browser.cdp": {}})
    b2 = _hello(bdb, "browser-2", {"browser.cdp": {}})
    phone = _hello(bdb, "phone", {"device.iphone.attached": {}})

    r = bus.request(bdb, requester=asker["session_id"], task="screenshot example.com",
                    needs=["browser.cdp"])
    assert set(r["notified_sessions"]) == {b1["session_id"], b2["session_id"]}
    assert bus.poll(bdb, phone["session_id"])["count"] == 0
    for w in (b1, b2):
        msgs = bus.poll(bdb, w["session_id"])["messages"]
        assert [m["kind"] for m in msgs] == ["request"]
        assert msgs[0]["request_id"] == r["request_id"]


def test_requester_is_never_asked_to_serve_itself(bdb):
    a = _hello(bdb, "a", {"browser.cdp": {}})
    _hello(bdb, "b", {"browser.cdp": {}})
    r = bus.request(bdb, requester=a["session_id"], task="t", needs=["browser.cdp"])
    assert a["session_id"] not in r["notified_sessions"]


def test_needs_requires_all_capabilities_not_any(bdb):
    asker = _hello(bdb, "asker")
    _hello(bdb, "partial", {"browser.cdp": {}})
    both = _hello(bdb, "both", {"browser.cdp": {}, "os.macos": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t",
                    needs=["browser.cdp", "os.macos"])
    assert r["notified_sessions"] == [both["session_id"]]


def test_exactly_one_claim_wins_under_contention(bdb):
    """The whole point of the design: no scheduler, and still a single winner."""
    asker = _hello(bdb, "asker")
    workers = [_hello(bdb, f"w{i}", {"browser.cdp": {}})["session_id"] for i in range(8)]
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["browser.cdp"])

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda sid: bus.claim(bdb, r["request_id"], sid)["won"], workers))

    assert results.count(True) == 1
    assert bus.request_get(bdb, r["request_id"])["state"] == "claimed"


def test_claim_requires_the_advertised_capability(bdb):
    asker = _hello(bdb, "asker")
    _hello(bdb, "real", {"browser.cdp": {}})
    faker = _hello(bdb, "faker", {"something.else": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["browser.cdp"])
    with pytest.raises(Invalid, match="does not advertise"):
        bus.claim(bdb, r["request_id"], faker["session_id"])


def test_directed_request_cannot_be_claimed_by_a_bystander(bdb):
    asker, target, other = (_hello(bdb, n) for n in ("asker", "target", "other"))
    r = bus.request(bdb, requester=asker["session_id"], task="t",
                    to_session=target["session_id"])
    with pytest.raises(Invalid, match="addressed to"):
        bus.claim(bdb, r["request_id"], other["session_id"])
    assert bus.claim(bdb, r["request_id"], target["session_id"])["won"] is True


def test_unaddressed_request_is_refused(bdb):
    """Not because nobody matches — empty needs matches everyone — but because first-claim
    would then lock an open question to one responder."""
    a = _hello(bdb, "a")
    with pytest.raises(Invalid, match="locked by whichever claimed it first"):
        bus.request(bdb, requester=a["session_id"], task="t")
    with pytest.raises(Invalid, match="bus_post to a room instead"):
        bus.request(bdb, requester=a["session_id"], task="t", needs=[])


def test_respond_is_restricted_to_the_claimant_and_reaches_the_requester(bdb):
    asker = _hello(bdb, "asker")
    w = _hello(bdb, "w", {"browser.cdp": {}})
    other = _hello(bdb, "other", {"browser.cdp": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["browser.cdp"])
    bus.claim(bdb, r["request_id"], w["session_id"])

    with pytest.raises(Invalid, match="claimed by"):
        bus.respond(bdb, r["request_id"], other["session_id"], result={"x": 1})

    bus.respond(bdb, r["request_id"], w["session_id"], result={"png": "deadbeef"})
    got = bus.request_get(bdb, r["request_id"])
    assert got["state"] == "done" and got["result"] == {"png": "deadbeef"}

    reply = [m for m in bus.poll(bdb, asker["session_id"])["messages"] if m["kind"] == "response"]
    assert reply and reply[0]["data"]["result"] == {"png": "deadbeef"}


def test_error_response_marks_failed(bdb):
    asker = _hello(bdb, "asker")
    w = _hello(bdb, "w", {"c": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["c"])
    bus.claim(bdb, r["request_id"], w["session_id"])
    bus.respond(bdb, r["request_id"], w["session_id"], error="no such element")
    assert bus.request_get(bdb, r["request_id"])["state"] == "failed"


def test_release_reopens_for_someone_else(bdb):
    asker = _hello(bdb, "asker")
    w1 = _hello(bdb, "w1", {"c": {}})
    w2 = _hello(bdb, "w2", {"c": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["c"])
    bus.claim(bdb, r["request_id"], w1["session_id"])
    with pytest.raises(Invalid, match="do not hold the claim"):
        bus.release(bdb, r["request_id"], w2["session_id"])
    bus.release(bdb, r["request_id"], w1["session_id"], "busy")
    assert bus.claim(bdb, r["request_id"], w2["session_id"])["won"] is True
    assert bus.request_get(bdb, r["request_id"])["attempts"] == 1


def test_claimable_only_filters_to_what_this_session_could_win(bdb):
    asker = _hello(bdb, "asker")
    w = _hello(bdb, "w", {"browser.cdp": {}})
    bus.request(bdb, requester=asker["session_id"], task="browser job", needs=["browser.cdp"])
    bus.request(bdb, requester=asker["session_id"], task="phone job",
                needs=["device.iphone.attached"])
    mine = bus.requests(bdb, session_id=w["session_id"], claimable_only=True)
    assert [r["task"] for r in mine["requests"]] == ["browser job"]


# ── questions and threading ───────────────────────────────────────────────────────

def test_a_question_gathers_its_answers(bdb):
    """The point of reply_to: two answers in a busy room are attributable to one question."""
    asker, bob, carol = (_hello(bdb, n) for n in ("asker", "bob", "carol"))
    q = bus.post(bdb, sender=asker["session_id"], kind="question",
                 body="anyone seen the parser hang on nested arrays?")
    bus.post(bdb, sender=bob["session_id"], body="yes, depth > 32", reply_to=q["seq"])
    bus.post(bdb, sender=carol["session_id"], body="unrelated chatter")     # noise in between
    bus.post(bdb, sender=carol["session_id"], body="only on 3.11", reply_to=q["seq"])

    t = bus.thread(bdb, q["seq"])
    assert [m["body"] for m in t["messages"]] == [
        "anyone seen the parser hang on nested arrays?", "yes, depth > 32", "only on 3.11"]
    assert t["replies"] == 2
    assert [m["depth"] for m in t["messages"]] == [0, 1, 1]


def test_poll_flags_that_a_message_has_replies(bdb):
    asker, bob = _hello(bdb, "asker"), _hello(bdb, "bob")
    q = bus.post(bdb, sender=asker["session_id"], kind="question", body="q?")
    got = bus.poll(bdb, bob["session_id"])
    assert "reply_count" not in got["messages"][0]
    bus.post(bdb, sender=bob["session_id"], body="a", reply_to=q["seq"])
    seen = bus.peek(bdb, _hello(bdb, "late")["session_id"], after=0)
    q_msg = [m for m in seen["messages"] if m["seq"] == q["seq"]][0]
    assert q_msg["reply_count"] == 1 and "bus_thread" in q_msg["thread_hint"]


def test_reply_inherits_the_parents_room(bdb):
    a, b = _hello(bdb, "a", rooms=["build"]), _hello(bdb, "b", rooms=["build"])
    q = bus.post(bdb, sender=a["session_id"], body="q?", room="build", kind="question")
    r = bus.post(bdb, sender=b["session_id"], body="a", reply_to=q["seq"])
    assert r["room"] == "build", "an answer must not drift into a different room from its question"
    assert [m["body"] for m in bus.history(bdb, "build")["messages"]] == ["q?", "a"]


def test_reply_to_a_direct_message_answers_the_sender(bdb):
    a, b, c = (_hello(bdb, n) for n in "abc")
    dm = bus.post(bdb, sender=a["session_id"], body="just you", to_session=b["session_id"])
    r = bus.post(bdb, sender=b["session_id"], body="ack", reply_to=dm["seq"])
    assert r["to_session"] == a["session_id"]
    assert [m["body"] for m in bus.poll(bdb, a["session_id"])["messages"]] == ["ack"]
    assert bus.poll(bdb, c["session_id"])["count"] == 0


def test_thread_excludes_private_answers(bdb):
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    q = bus.post(bdb, sender=a["session_id"], kind="question", body="public q")
    bus.post(bdb, sender=b["session_id"], body="public answer", reply_to=q["seq"])
    bus.post(bdb, sender=b["session_id"], body="quiet word", reply_to=q["seq"],
             to_session=a["session_id"])
    bodies = [m["body"] for m in bus.thread(bdb, q["seq"])["messages"]]
    assert bodies == ["public q", "public answer"], "a DM reply must stay private"


def test_threads_nest_and_are_depth_capped(bdb):
    a = _hello(bdb, "a")
    root = bus.post(bdb, sender=a["session_id"], body="0", kind="question")["seq"]
    parent = root
    for i in range(1, 12):
        parent = bus.post(bdb, sender=a["session_id"], body=str(i), reply_to=parent)["seq"]
    t = bus.thread(bdb, root)
    assert max(m["depth"] for m in t["messages"]) == bus.MAX_THREAD_DEPTH
    assert [m["body"] for m in t["messages"]] == [str(i) for i in range(bus.MAX_THREAD_DEPTH + 1)]


def test_reply_to_a_missing_message_is_rejected(bdb):
    a = _hello(bdb, "a")
    with pytest.raises(NotFound, match="to reply to"):
        bus.post(bdb, sender=a["session_id"], body="a", reply_to=99999)


def test_expiring_a_question_orphans_its_answers_rather_than_deleting_them(bdb):
    """TTL is per message, so a question expires before the answers it provoked."""
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    q = bus.post(bdb, sender=a["session_id"], kind="question", body="q?", ttl=-1)
    ans = bus.post(bdb, sender=b["session_id"], body="an answer", reply_to=q["seq"])
    bus.reap(bdb)
    with bdb.read() as cur:
        row = cur.execute("SELECT body, reply_to FROM bus_message WHERE seq=?",
                          (ans["seq"],)).fetchone()
    assert row is not None and row["body"] == "an answer", "the answer must survive"
    assert row["reply_to"] is None, "ON DELETE SET NULL should clear the dangling link"


def test_reply_to_migrates_onto_an_existing_bus_message_table(tmp_path):
    """A server already running the pre-threading bus must upgrade in place.

    Regression: schema.sql indexes reply_to, and on an existing table CREATE TABLE IF NOT EXISTS
    is a silent no-op while CREATE INDEX is not — so migrations have to run BEFORE the script.
    """
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p, isolation_level=None)
    con.execute("""CREATE TABLE bus_message (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL DEFAULT 'lobby',
      sender TEXT NOT NULL, to_session TEXT, kind TEXT NOT NULL DEFAULT 'chat',
      body TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', request_id TEXT,
      created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
    con.execute("INSERT INTO bus_message(sender,body,created_at,expires_at) "
                "VALUES('s','pre-existing','t','9999')")
    con.close()

    db = Database(p)                                    # must not raise
    with db.read() as cur:
        assert "reply_to" in [r[1] for r in cur.execute("PRAGMA table_info(bus_message)")]
        assert cur.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                           "AND name='ix_bus_msg_reply'").fetchone()
        assert cur.execute("SELECT body FROM bus_message WHERE seq=1").fetchone()[0] \
            == "pre-existing"

    a, b = _hello(db, "a"), _hello(db, "b")
    q = bus.post(db, sender=a["session_id"], kind="question", body="q?", ttl=-1)
    ans = bus.post(db, sender=b["session_id"], body="ans", reply_to=q["seq"])
    assert bus.thread(db, q["seq"])["replies"] == 1
    bus.reap(db)
    with db.read() as cur:                              # the FK came across with the ALTER
        assert cur.execute("SELECT reply_to FROM bus_message WHERE seq=?",
                           (ans["seq"],)).fetchone()[0] is None


def test_thread_on_a_message_with_no_replies(bdb):
    a = _hello(bdb, "a")
    q = bus.post(bdb, sender=a["session_id"], body="lonely")
    t = bus.thread(bdb, q["seq"])
    assert t["count"] == 1 and t["replies"] == 0


def test_question_is_a_valid_kind_and_chat_still_default(bdb):
    a = _hello(bdb, "a")
    assert bus.post(bdb, sender=a["session_id"], body="x")["kind"] == "chat"
    assert bus.post(bdb, sender=a["session_id"], body="x", kind="question")["kind"] == "question"


# ── graph references ──────────────────────────────────────────────────────────────

def test_request_refs_reach_the_worker_with_labels(gdb):
    """A worker must see WHAT it is being asked to act on, not just a prose task."""
    n = graph.upsert_node(gdb, "a", "thing", {"title": "the subject"})
    asker = _hello(gdb, "asker")
    w = _hello(gdb, "w", {"c": {}})

    r = bus.request(gdb, requester=asker["session_id"], task="look at this", needs=["c"],
                    refs=[n["node_id"]])
    assert r["refs"] == [{"kind": "node", "role": "context", "anchor": n["node_id"]}]

    msg = bus.poll(gdb, w["session_id"])["messages"][0]
    assert msg["refs"][0]["target"]["node_id"] == n["node_id"]
    assert msg["refs"][0]["target"]["node_type"] == "thing"
    assert "the subject" in msg["refs"][0]["target"]["snippet"]


def test_all_ref_kinds_validate_and_resolve(gdb):
    a = graph.upsert_node(gdb, "a", "thing", {"title": "A"}, subject_key="S", subject_version="1")
    b = graph.upsert_node(gdb, "a", "thing", {"title": "B"})
    graph.upsert_edge(gdb, "a", "relates", a["node_id"], b["node_id"])
    s = _hello(gdb, "s")

    posted = bus.post(gdb, sender=s["session_id"], body="everything", refs=[
        {"kind": "node", "id": b["node_id"], "role": "target", "note": "the target"},
        {"kind": "version", "id": a["version_id"]},
        {"kind": "subject", "key": "S", "version": "1"},
        {"kind": "traversal", "id": a["node_id"], "edge_types": ["relates"], "depth": 2},
        {"kind": "search", "query": "A"},
    ])
    assert [r["kind"] for r in posted["refs"]] == ["node", "version", "subject", "traversal",
                                                   "search"]

    got = bus.resolve(gdb, seq=posted["seq"])
    kinds = {r["kind"]: r for r in got["refs"]}
    assert kinds["node"]["node"]["node_id"] == b["node_id"]
    assert kinds["node"]["role"] == "target" and kinds["node"]["note"] == "the target"
    assert kinds["version"]["pinned_version_id"] == a["version_id"]
    assert kinds["subject"]["node"]["node_id"] == a["node_id"]
    assert any(x["node_id"] == b["node_id"]
               for x in kinds["traversal"]["neighbors"]["neighbors"])
    assert kinds["search"]["results"]["results"]


def test_bad_refs_are_rejected_at_post_time(gdb):
    s = _hello(gdb, "s")
    n = graph.upsert_node(gdb, "a", "thing", {"title": "A"})
    for bad, match in [
        ("NOPE", "no node"),
        ({"kind": "version", "id": "NOPE"}, "no node version"),
        ({"kind": "subject", "key": "missing"}, "no node for subject_key"),
        ({"kind": "search", "query": "  "}, "non-empty query"),
        ({"kind": "traversal", "id": n["node_id"], "depth": 9}, "depth must be 1..4"),
        ({"kind": "wat", "id": n["node_id"]}, "ref kind must be"),
        ({"kind": "node", "id": n["node_id"], "role": "wat"}, "ref role must be"),
        (123, "must be a node_id string or an object"),
    ]:
        with pytest.raises(Invalid, match=match):
            bus.post(gdb, sender=s["session_id"], body="x", refs=[bad])


def test_rejected_ref_leaves_no_message_behind(gdb):
    """The whole post is one transaction: a bad ref must not leave an orphan message."""
    s = _hello(gdb, "s")
    before = bus.stats(gdb)["head_seq"]
    with pytest.raises(Invalid):
        bus.post(gdb, sender=s["session_id"], body="doomed", refs=["NOPE"])
    with gdb.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM bus_message WHERE seq > ?",
                           (before,)).fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM bus_ref").fetchone()[0] == 0


def test_too_many_refs_rejected(gdb):
    s = _hello(gdb, "s")
    n = graph.upsert_node(gdb, "a", "thing", {"title": "A"})["node_id"]
    with pytest.raises(Invalid, match="at most 25 refs"):
        bus.post(gdb, sender=s["session_id"], body="x", refs=[n] * 26)


def test_response_refs_close_the_provenance_loop(gdb):
    """Work produces a node; the requester gets its id, not a copy that expires."""
    asker = _hello(gdb, "asker")
    w = _hello(gdb, "w", {"c": {}})
    subject = graph.upsert_node(gdb, "a", "thing", {"title": "input"})["node_id"]
    r = bus.request(gdb, requester=asker["session_id"], task="analyse", needs=["c"],
                    refs=[subject])
    bus.claim(gdb, r["request_id"], w["session_id"])
    produced = graph.upsert_node(gdb, "w", "thing", {"title": "output"})["node_id"]
    bus.respond(gdb, r["request_id"], w["session_id"], result={"ok": True}, refs=[produced])

    got = bus.request_get(gdb, r["request_id"])
    assert [x["target"]["node_id"] for x in got["refs"]] == [subject]
    assert [x["target"]["node_id"] for x in got["produced"]] == [produced]
    assert got["produced"][0]["role"] == "result"      # defaulted, distinguishing it from context


def test_reverse_lookup_finds_traffic_about_a_node(gdb):
    n = graph.upsert_node(gdb, "a", "thing", {"title": "hot topic"})["node_id"]
    other = graph.upsert_node(gdb, "a", "thing", {"title": "quiet"})["node_id"]
    asker = _hello(gdb, "asker")
    _hello(gdb, "w", {"c": {}})
    bus.post(gdb, sender=asker["session_id"], body="talking about it", refs=[n])
    bus.request(gdb, requester=asker["session_id"], task="work on it", needs=["c"], refs=[n])

    hits = bus.refs_for_node(gdb, n)
    assert hits["count"] == 2
    assert any(h.get("body") == "talking about it" for h in hits["refs"])
    assert any(h.get("request", {}).get("task") == "work on it" for h in hits["refs"])
    assert bus.refs_for_node(gdb, other)["count"] == 0


def test_refs_follow_a_merge_tombstone(gdb):
    """A ref to a merged node must resolve to the surviving node, not the tombstone."""
    old = graph.upsert_node(gdb, "a", "thing", {"title": "old"})["node_id"]
    new = graph.upsert_node(gdb, "a", "thing", {"title": "new"})["node_id"]
    with gdb.write("a", "merge") as tx:
        tx.cur.execute("UPDATE node SET redirect_to=? WHERE node_id=?", (new, old))
    s = _hello(gdb, "s")
    posted = bus.post(gdb, sender=s["session_id"], body="x", refs=[old])
    assert posted["refs"][0]["anchor"] == new


def test_reaping_a_message_reaps_its_refs(gdb):
    n = graph.upsert_node(gdb, "a", "thing", {"title": "A"})["node_id"]
    a, b = _hello(gdb, "a"), _hello(gdb, "b")
    bus.post(gdb, sender=a["session_id"], body="x", to_session=b["session_id"], refs=[n], ttl=-1)
    assert bus.refs_for_node(gdb, n)["count"] == 1
    bus.reap(gdb)
    with gdb.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM bus_ref").fetchone()[0] == 0, \
            "ON DELETE CASCADE should take the refs with the message"
    assert bus.refs_for_node(gdb, n)["count"] == 0


def test_resolve_reports_a_rotted_ref_instead_of_failing(gdb):
    """A ref valid at post time can rot; one bad ref must not sink the whole resolve."""
    n = graph.upsert_node(gdb, "a", "thing", {"title": "A"})["node_id"]
    good = graph.upsert_node(gdb, "a", "thing", {"title": "B"})["node_id"]
    s = _hello(gdb, "s")
    posted = bus.post(gdb, sender=s["session_id"], body="x", refs=[n, good])
    with gdb.write("a", "remove") as tx:      # simulate the node going away underneath us
        tx.cur.execute("DELETE FROM node_version WHERE node_id=?", (n,))
        tx.cur.execute("DELETE FROM node WHERE node_id=?", (n,))
    got = bus.resolve(gdb, seq=posted["seq"])
    assert "error" in got["refs"][0] and "node" in got["refs"][1]


def test_resolve_needs_exactly_one_selector(gdb):
    with pytest.raises(Invalid, match="exactly one"):
        bus.resolve(gdb)
    with pytest.raises(Invalid, match="exactly one"):
        bus.resolve(gdb, request_id="x", seq=1)


def test_bus_still_works_with_no_graph_schema(bdb):
    """Refs are optional: the bus must stay usable on a project with zero node types."""
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    bus.post(bdb, sender=a["session_id"], body="no refs here", to_session=b["session_id"])
    assert bus.poll(bdb, b["session_id"])["messages"][0].get("refs") is None
    with pytest.raises(Invalid, match="no node"):
        bus.post(bdb, sender=a["session_id"], body="x", refs=["ANYTHING"])


# ── reaping ───────────────────────────────────────────────────────────────────────

def test_dead_claimant_lease_reopens_the_request(bdb):
    asker = _hello(bdb, "asker")
    w = _hello(bdb, "w", {"c": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["c"])
    bus.claim(bdb, r["request_id"], w["session_id"], lease_sec=-1)   # already expired

    assert bus.reap(bdb)["leases_reopened"] >= 1
    got = bus.request_get(bdb, r["request_id"])
    assert got["state"] == "open" and got["claimed_by"] is None and got["attempts"] == 1
    assert any(m["kind"] == "system" for m in bus.poll(bdb, asker["session_id"])["messages"])


def test_bye_releases_claims_immediately(bdb):
    asker = _hello(bdb, "asker")
    w = _hello(bdb, "w", {"c": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["c"])
    bus.claim(bdb, r["request_id"], w["session_id"])
    assert bus.bye(bdb, w["session_id"])["requests_released"] == 1
    assert bus.request_get(bdb, r["request_id"])["state"] == "open"


def test_expired_session_release_and_message_reap(bdb):
    a = _hello(bdb, "a")
    b = _hello(bdb, "b", ttl=-1)
    bus.post(bdb, sender=a["session_id"], body="x", to_session=b["session_id"], ttl=-1)
    out = bus.reap(bdb)
    assert out["sessions_expired"] == 1
    assert out["messages_deleted"] == 1
    assert bus.stats(bdb)["messages"] == 0


def test_reap_is_idempotent(bdb):
    _hello(bdb, "a", ttl=-1)
    first = bus.reap(bdb)
    second = bus.reap(bdb)
    assert first["sessions_expired"] == 1
    assert second == {"sessions_expired": 0, "leases_reopened": 0, "requests_expired": 0,
                      "messages_deleted": 0}


def test_seq_is_not_reused_after_a_reap(bdb):
    """AUTOINCREMENT, not rowid: a reaped tail must not rewind every live cursor."""
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    bus.post(bdb, sender=a["session_id"], body="old", to_session=b["session_id"], ttl=-1)
    high = bus.stats(bdb)["head_seq"]
    bus.reap(bdb)
    bus.post(bdb, sender=a["session_id"], body="new", to_session=b["session_id"])
    assert bus.stats(bdb)["head_seq"] > high


def test_expired_request_is_marked_not_deleted(bdb):
    asker = _hello(bdb, "asker")
    _hello(bdb, "w", {"c": {}})
    r = bus.request(bdb, requester=asker["session_id"], task="t", needs=["c"], ttl=-1)
    bus.reap(bdb)
    assert bus.request_get(bdb, r["request_id"])["state"] == "expired"


def test_stats_reflect_activity(bdb):
    a = _hello(bdb, "a")
    w = _hello(bdb, "w", {"c": {}})
    bus.request(bdb, requester=a["session_id"], task="t", needs=["c"])
    s = bus.stats(bdb)
    assert s["live_sessions"] == 2 and s["open_requests"] == 1 and s["messages"] >= 1
    assert w["session_id"]


def test_concurrent_posts_all_land_with_distinct_seqs(bdb):
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: bus.post(bdb, sender=a["session_id"], body=f"m{i}",
                                         to_session=b["session_id"]), range(24)))
    msgs = bus.poll(bdb, b["session_id"], limit=500)["messages"]
    seqs = [m["seq"] for m in msgs]
    assert len(msgs) == 24
    assert len(set(seqs)) == 24 and seqs == sorted(seqs)


def test_write_light_leaves_no_provenance_rows(bdb):
    """Bus traffic must not accumulate tx rows that outlive the TTL-reaped messages."""
    with bdb.read() as cur:
        before = cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
    a, b = _hello(bdb, "a"), _hello(bdb, "b")
    for i in range(5):
        bus.post(bdb, sender=a["session_id"], body=f"m{i}", to_session=b["session_id"])
    with bdb.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == before


def test_write_light_rolls_back_on_error(bdb):
    a = _hello(bdb, "a")
    with pytest.raises(RuntimeError):
        with bdb.write_light() as cur:
            cur.execute("INSERT INTO bus_membership(session_id,room,joined_at) VALUES(?,?,?)",
                        (a["session_id"], "ghost", "now"))
            raise RuntimeError("boom")
    with bdb.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM bus_membership WHERE room='ghost'"
                           ).fetchone()[0] == 0
    # the connection is usable afterwards
    assert bus.join(bdb, a["session_id"], "real")["joined"] is True


def test_lazy_reap_is_rate_limited(bdb, monkeypatch):
    calls = []
    real = bus.reap
    monkeypatch.setattr(bus, "reap", lambda db: (calls.append(1), real(db))[1])
    bus._last_reap.clear()
    _hello(bdb, "a")
    bus.sessions(bdb)
    bus.sessions(bdb)
    bus.sessions(bdb)
    assert len(calls) == 1, "reap should be gated, not run on every read"
    time.sleep(0)
