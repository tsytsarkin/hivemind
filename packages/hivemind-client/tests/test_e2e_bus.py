"""End-to-end bus: MCP tools over HTTP, the long-poll that makes interrupts possible, and the
`hivemind bus wait` exit code that a harness turns into a wake-up."""
import json
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from hivemind import Client


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "d" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    import hivemind_server.config as cfgmod
    cfgmod._cfg = None                       # config() is cached; force a re-read of the env
    from hivemind_server import app as appmod
    from hivemind_server.config import Config
    application = appmod.build_app(Config())
    proj = application.state.registry.all()[0]
    tok = next(iter(json.loads((proj.dir / "tokens.json").read_text())))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                        log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/p/{proj.name}", tok, proj
    srv.should_exit = True; th.join(timeout=5)
    cfgmod._cfg = None


def test_capability_discovery_and_request_round_trip(server):
    base, tok, _ = server
    asker = Client(base, tok, agent="asker")
    worker = Client(base, tok, agent="worker")     # SAME token, different session

    a = asker.call("bus_hello", {"label": "asker"})
    w = worker.call("bus_hello", {"label": "browser-box", "harness": "claude-code",
                                  "interruptible": True,
                                  "capabilities": {"browser.cdp": {"version": "131"}}})
    assert a["session_id"] != w["session_id"], "one token must not collapse into one identity"

    found = asker.call("bus_agents", {"capability": "browser.*"})
    assert [s["session_id"] for s in found["sessions"]] == [w["session_id"]]
    assert found["sessions"][0]["interruptible"] is True
    assert asker.call("bus_capabilities", {})["capabilities"] == [
        {"name": "browser.cdp", "sessions": 1}]

    r = asker.call("bus_request", {"session_id": a["session_id"], "task": "screenshot",
                                   "needs": ["browser.cdp"]})
    assert r["notified"] == 1

    inbox = worker.call("bus_poll", {"session_id": w["session_id"]})
    assert inbox["messages"][0]["request_id"] == r["request_id"]
    assert worker.call("bus_claim", {"session_id": w["session_id"],
                                     "request_id": r["request_id"]})["won"] is True
    worker.call("bus_respond", {"session_id": w["session_id"], "request_id": r["request_id"],
                                "result": {"png": "cafebabe"}})

    done = asker.call("bus_request_get", {"request_id": r["request_id"]})
    assert done["state"] == "done" and done["result"] == {"png": "cafebabe"}


def test_long_poll_returns_as_soon_as_a_message_lands(server):
    """The wake path: /bus/wait blocks, then returns the instant someone posts."""
    base, tok, _ = server
    a = Client(base, tok, agent="a")
    b = Client(base, tok, agent="b")
    sa = a.call("bus_hello", {"label": "a"})["session_id"]
    sb = b.call("bus_hello", {"label": "b"})["session_id"]

    def post_soon():
        time.sleep(0.6)
        a.call("bus_post", {"session_id": sa, "body": "wake up", "to_session": sb})

    threading.Thread(target=post_soon, daemon=True).start()
    t0 = time.time()
    out = b.bus_wait(sb, wait=20, interval=0.2)
    elapsed = time.time() - t0

    assert out["messages"] and out["messages"][0]["body"] == "wake up"
    assert out["timed_out"] is False
    assert elapsed < 10, f"long-poll should return on the message, not the timeout ({elapsed:.1f}s)"


def test_long_poll_times_out_without_consuming(server):
    base, tok, _ = server
    a = Client(base, tok, agent="a")
    sa = a.call("bus_hello", {"label": "a"})["session_id"]
    out = a.bus_wait(sa, wait=1, interval=0.2)
    assert out["timed_out"] is True and out["messages"] == [] and out["consumed"] is False


def test_watcher_peek_does_not_rob_the_agent(server):
    """A watcher that saw a message must leave it for the agent it was trying to wake."""
    base, tok, _ = server
    a, b = Client(base, tok, agent="a"), Client(base, tok, agent="b")
    sa = a.call("bus_hello", {"label": "a"})["session_id"]
    sb = b.call("bus_hello", {"label": "b"})["session_id"]
    a.call("bus_post", {"session_id": sa, "body": "important", "to_session": sb})

    seen = b.bus_wait(sb, wait=5, interval=0.2)          # the watcher
    assert seen["messages"][0]["body"] == "important"
    drained = b.call("bus_poll", {"session_id": sb})     # the agent, afterwards
    assert [m["body"] for m in drained["messages"]] == ["important"]


def test_bus_wait_cli_exit_codes(server):
    """0 = drain me, 75 = nothing yet, re-arm. A harness turns the exit into a wake-up."""
    base, tok, _ = server
    a, b = Client(base, tok, agent="a"), Client(base, tok, agent="b")
    sa = a.call("bus_hello", {"label": "a"})["session_id"]
    sb = b.call("bus_hello", {"label": "b"})["session_id"]
    argv = [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
            "bus", "wait", sb, "--interval", "0.2"]

    timed_out = subprocess.run(argv + ["--wait", "1"], capture_output=True, text=True)
    assert timed_out.returncode == 75
    assert json.loads(timed_out.stdout)["timed_out"] is True

    a.call("bus_post", {"session_id": sa, "body": "go", "to_session": sb})
    woke = subprocess.run(argv + ["--wait", "10"], capture_output=True, text=True)
    assert woke.returncode == 0
    assert json.loads(woke.stdout)["messages"][0]["body"] == "go"


def test_sidecar_absorbs_timeouts_and_exits_only_with_drained_messages(server):
    """The whole point: a quiet bus must not keep waking the agent, and a wake must carry news."""
    base, tok, _ = server
    a, b = Client(base, tok, agent="a"), Client(base, tok, agent="b")
    sa = a.call("bus_hello", {"label": "a"})["session_id"]
    sb = b.call("bus_hello", {"label": "b", "ttl": 900})["session_id"]
    argv = [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
            "bus", "sidecar", sb, "--wait", "1", "--interval", "0.2", "--ping-every", "0.5"]

    # Nothing to say: several wait timeouts pass and the process is still running, where
    # `bus wait` would have exited at the first one and cost the agent a turn.
    proc = subprocess.Popen(argv + ["--max-idle", "30"], stdout=subprocess.PIPE, text=True)
    time.sleep(3.5)
    assert proc.poll() is None, "sidecar exited on a timeout; it should have absorbed it"

    a.call("bus_post", {"session_id": sa, "body": "go", "to_session": sb})
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0
    drained = json.loads(out)
    assert drained["messages"][0]["body"] == "go"
    assert drained["consumed"] is True                 # already drained: no follow-up poll needed
    assert b.call("bus_poll", {"session_id": sb})["count"] == 0     # and the cursor really moved


def test_sidecar_leaves_when_the_harness_dies_rather_than_advertising_a_dead_session(server):
    """The real bound on outliving your agent: watch the process that would have woken it.

    A timer cannot tell "nobody is home" from "nothing happened yet", and taxes long-lived
    sessions for the privilege of guessing.
    """
    base, tok, _ = server
    c = Client(base, tok, agent="c")
    s = c.call("bus_hello", {"label": "c", "ttl": 900})["session_id"]
    stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    side = subprocess.Popen(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "sidecar", s, "--wait", "1", "--interval", "0.2",
         "--parent-pid", str(stand_in.pid)], stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(2)
        assert side.poll() is None                      # parent alive: still watching
        assert c.call("bus_agents", {})["count"] == 1

        stand_in.kill(); stand_in.wait()
        out, _ = side.communicate(timeout=20)
        assert side.returncode == 71, out
        assert json.loads(out)["orphaned"] is True
        # Ended immediately, not left to decay for the rest of its 900s ttl.
        assert c.call("bus_agents", {})["count"] == 0
    finally:
        for p in (stand_in, side):
            if p.poll() is None:
                p.kill()


def test_sidecar_max_idle_is_off_by_default_but_still_available(server):
    """Opt-in, for harnesses where no pid can be watched."""
    base, tok, _ = server
    s = Client(base, tok, agent="e").call("bus_hello", {"label": "e"})["session_id"]
    out = subprocess.run(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "sidecar", s, "--wait", "1", "--interval", "0.2", "--max-idle", "2",
         "--parent-pid", "0"],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 75, out.stderr
    assert json.loads(out.stdout)["idle_exit"] is True


def test_sidecar_reports_a_dead_session_instead_of_asking_to_be_re_armed(server):
    """Exit 69, not 75: re-arming a sidecar on an expired session would just fail again."""
    base, tok, _ = server
    c = Client(base, tok, agent="d")
    s = c.call("bus_hello", {"label": "d"})["session_id"]
    c.call("bus_bye", {"session_id": s})
    out = subprocess.run(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "sidecar", s, "--wait", "1", "--max-idle", "10"],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 69, out.stderr
    assert "session_gone" in json.loads(out.stdout)


def test_bus_cli_hello_parses_capability_attrs(server):
    base, tok, _ = server
    out = subprocess.run(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "hello", "--label", "cli-box", "--capability", "browser.cdp",
         "--capability", 'device.handset.attached={"serial":"XYZ"}'],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["capabilities"] == ["browser.cdp", "device.handset.attached"]

    listed = Client(base, tok, agent="x").call("bus_agents", {"capability": "device.*"})
    assert listed["sessions"][0]["capabilities"]["device.handset.attached"] == {"serial": "XYZ"}


def test_refs_survive_the_wire_and_resolve(server):
    """A request points at a node; the worker sees it labelled and can resolve it in full."""
    base, tok, proj = server
    from hivemind_server import graph, schemas
    with proj.db.write("setup", "types") as tx:
        schemas.define_type(tx.cur, tx, "node", "thing",
                            {"type": "object", "additionalProperties": True}, status="active")
    subject = graph.upsert_node(proj.db, "op", "thing", {"title": "the subject"})["node_id"]

    asker, worker = Client(base, tok, agent="asker"), Client(base, tok, agent="worker")
    a = asker.call("bus_hello", {"label": "asker"})["session_id"]
    w = worker.call("bus_hello", {"label": "w", "capabilities": {"analysis": {}}})["session_id"]

    r = asker.call("bus_request", {"session_id": a, "task": "analyse", "needs": ["analysis"],
                                   "refs": [subject]})
    msg = worker.call("bus_poll", {"session_id": w})["messages"][0]
    assert msg["refs"][0]["target"]["node_id"] == subject
    assert "the subject" in msg["refs"][0]["target"]["snippet"]

    full = worker.call("bus_resolve", {"request_id": r["request_id"]})
    assert full["refs"][0]["node"]["current"]["props"]["title"] == "the subject"

    worker.call("bus_claim", {"session_id": w, "request_id": r["request_id"]})
    out = graph.upsert_node(proj.db, "w", "thing", {"title": "the result"})["node_id"]
    worker.call("bus_respond", {"session_id": w, "request_id": r["request_id"],
                                "result": {"ok": True}, "refs": [out]})

    got = asker.call("bus_request_get", {"request_id": r["request_id"]})
    assert [x["target"]["node_id"] for x in got["produced"]] == [out]

    back = asker.call("bus_node_refs", {"node_id": subject})
    assert back["count"] == 1 and back["refs"][0]["request"]["task"] == "analyse"


def test_bad_ref_is_an_actionable_tool_error_not_a_crash(server):
    base, tok, _ = server
    from hivemind import HivemindError
    c = Client(base, tok, agent="a")
    sid = c.call("bus_hello", {"label": "a"})["session_id"]
    with pytest.raises(HivemindError) as e:
        c.call("bus_post", {"session_id": sid, "body": "x", "refs": ["NOT_A_NODE"]})
    assert e.value.kind == "invalid" and "no node" in str(e.value)


def test_question_and_answers_over_the_wire(server):
    base, tok, _ = server
    asker = Client(base, tok, agent="asker")
    bob, carol = Client(base, tok, agent="bob"), Client(base, tok, agent="carol")
    a = asker.call("bus_hello", {"label": "asker"})["session_id"]
    b = bob.call("bus_hello", {"label": "bob"})["session_id"]
    c = carol.call("bus_hello", {"label": "carol"})["session_id"]

    q = asker.call("bus_post", {"session_id": a, "kind": "question",
                                "body": "anyone seen the parser hang?"})
    assert bob.call("bus_poll", {"session_id": b})["messages"][0]["kind"] == "question"
    carol.call("bus_poll", {"session_id": c})

    bob.call("bus_post", {"session_id": b, "body": "yes, depth > 32", "reply_to": q["seq"]})
    carol.call("bus_post", {"session_id": c, "body": "only on 3.11", "reply_to": q["seq"]})

    t = asker.call("bus_thread", {"seq": q["seq"]})
    assert t["replies"] == 2
    assert [m["body"] for m in t["messages"][1:]] == ["yes, depth > 32", "only on 3.11"]

    # both answers reach the asker, each marked as answering that question
    inbox = asker.call("bus_poll", {"session_id": a})
    assert {m["reply_to"] for m in inbox["messages"]} == {q["seq"]}


def test_bus_thread_cli(server):
    base, tok, _ = server
    c = Client(base, tok, agent="a")
    sid = c.call("bus_hello", {"label": "a"})["session_id"]
    q = c.call("bus_post", {"session_id": sid, "kind": "question", "body": "q?"})
    out = subprocess.run(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "post", sid, "an answer", "--reply-to", str(q["seq"])],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["reply_to"] == q["seq"]

    got = subprocess.run(
        [sys.executable, "-m", "hivemind.cli", "--url", base, "--token", tok,
         "bus", "thread", str(q["seq"])], capture_output=True, text=True)
    assert json.loads(got.stdout)["replies"] == 1


def test_wait_rejects_unknown_session(server):
    base, tok, _ = server
    from hivemind import HivemindError
    c = Client(base, tok, agent="a")
    with pytest.raises(HivemindError):
        c.bus_wait("NOPE", wait=1)
