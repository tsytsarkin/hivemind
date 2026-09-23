"""A refused write must not be able to kill a batch.

The failure behind these tests: a batch of writes was running, one row carried a value outside its
type's enum, the tool answered `{"ok": false}`, `call()` raised, and the exception took down the
whole batch mid-run — leaving the graph half-updated with no record of where it stopped.

`call()` still raises by default, because every convenience wrapper and the whole CLI are built on
that and would otherwise return refusals nobody checks. The batch path is `call_many`: every item
gets a reply, refusal or failure alike. It does still raise the caller's own bugs — a malformed
item, an unserialisable argument — because recording those as failed calls hides them.
"""
import pytest

from hivemind.client import HivemindError


def test_a_refusal_raises_by_default(fake_transport):
    """Unchanged behaviour for every existing caller: `ok: false` is an exception."""
    c = fake_transport([{"ok": False, "error_kind": "invalid", "error": "bad verdict"}])
    with pytest.raises(HivemindError) as e:
        c.call("graph_upsert", {"type": "stage_run"})
    assert e.value.kind == "invalid"


def test_raise_on_error_false_returns_the_refusal_as_data(fake_transport):
    """The opt-out: a refusal is data, and the whole envelope survives."""
    c = fake_transport([{"ok": False, "error_kind": "invalid", "error": "bad verdict"}])
    out = c.call("graph_upsert", {"type": "stage_run"}, raise_on_error=False)
    assert out["ok"] is False and out["error_kind"] == "invalid" and out["error"] == "bad verdict"


def test_raise_on_error_false_still_raises_below_the_envelope(fake_transport):
    """The opt-out covers tool refusals only. An HTTP failure has no envelope to return."""
    c = fake_transport([{"ok": True}], status=400)
    with pytest.raises(HivemindError):
        c.call("graph_upsert", {"type": "stage_run"}, raise_on_error=False)


def test_call_many_completes_every_item_despite_a_refusal(fake_transport):
    c = fake_transport([{"ok": True, "node_id": "a"},
                        {"ok": False, "error_kind": "invalid", "error": "bad verdict",
                         "project": "nik.private"},
                        {"ok": True, "node_id": "c"}])
    results = c.call_many([("graph_upsert", {"i": 0}), ("graph_upsert", {"i": 1}),
                           ("graph_upsert", {"i": 2})])
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[1]["error_kind"] == "invalid"
    assert [r.get("node_id") for r in results] == ["a", None, "c"]
    # the refusal is the server's own envelope, not one rebuilt from an exception: a tool's reply
    # echoes the `project` it ran in, refusal included, and rebuilding it from the three fields
    # `HivemindError` carries would drop that
    assert results[1]["project"] == "nik.private"
    # every call reached the wire, in order, after the refusal as well as before it
    assert [p["arguments"]["i"] for p in c.sent] == [0, 1, 2]


def test_call_many_surfaces_a_transport_failure_per_item(fake_transport_raising):
    c = fake_transport_raising()
    results = c.call_many([("graph_upsert", {"i": 0})])
    assert results[0]["ok"] is False and results[0]["error_kind"] == "transport"
    assert "connection refused" in results[0]["error"]


def test_call_many_does_not_raise_when_the_whole_batch_fails(fake_transport_raising):
    c = fake_transport_raising()
    results = c.call_many([("graph_upsert", {"i": i}) for i in range(3)])
    assert [r["error_kind"] for r in results] == ["transport"] * 3


def test_call_many_labels_an_unkinded_failure_error_not_transport(fake_transport):
    """An HTTP failure raises a HivemindError with `kind` None; it must not be called transport."""
    c = fake_transport([{"ok": True}], status=400)
    results = c.call_many([("graph_upsert", {"i": 0})])
    assert results[0]["ok"] is False and results[0]["error_kind"] == "error"
    assert "HTTP 400" in results[0]["error"]


def test_call_many_round_trips_the_kind_of_a_kinded_failure(fake_transport):
    """401 raises kind='auth'; the envelope must say auth, not the generic fallback."""
    c = fake_transport([{"ok": True}], status=401)
    results = c.call_many([("graph_upsert", {"i": 0})])
    assert results[0]["error_kind"] == "auth"


def test_call_many_stops_after_the_first_refusal_when_asked(fake_transport):
    """stop_on_error records the refusal that stopped it — that is the missing record."""
    c = fake_transport([{"ok": True, "node_id": "a"},
                        {"ok": False, "error_kind": "invalid", "error": "bad verdict"}])
    results = c.call_many([("graph_upsert", {"i": 0}), ("graph_upsert", {"i": 1}),
                           ("graph_upsert", {"i": 2})], stop_on_error=True)
    assert [r["ok"] for r in results] == [True, False]
    assert len(c.sent) == 2                      # the third call was never made


def test_call_many_passes_the_agent_through_like_call_does(fake_transport):
    c = fake_transport([{"ok": True}])
    c.call_many([("graph_upsert", {"i": 0})])
    assert c.sent[0]["arguments"]["agent"] == "test"


def test_call_many_of_nothing_is_an_empty_list(fake_transport):
    c = fake_transport([])
    assert c.call_many([]) == []
    assert c.sent == []


def test_call_many_does_not_choke_on_a_reply_that_is_not_a_dict(fake_transport):
    """A tool answering with a text block yields a bare string. That is a reply, not a refusal.

    `.get("ok")` on it is an AttributeError, and a truth test on it is a false failure — either
    would crash or silently truncate a batch of otherwise fine calls.
    """
    text = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hi"}]}}
    c = fake_transport([text, text], raw=True)
    results = c.call_many([("guide_get", {}), ("guide_get", {})], stop_on_error=True)
    assert results == ["hi", "hi"]
    assert len(c.sent) == 2                      # it neither raised nor stopped early


def test_call_many_does_not_treat_an_ok_less_dict_as_a_refusal(fake_transport):
    """`call` raises only on `ok is False`, so a dict with no `ok` is a success. Both agree."""
    c = fake_transport([{"node_id": "a"}, {"node_id": "b"}])
    results = c.call_many([("graph_get", {}), ("graph_get", {})], stop_on_error=True)
    assert [r["node_id"] for r in results] == ["a", "b"]
    assert len(c.sent) == 2


def test_call_many_stops_on_a_failure_that_raised_too(fake_transport):
    """stop_on_error is the only control against an N-item batch re-failing N times.

    A bad token 401s every call and 401 is not retried, so without this the batch quietly makes
    every remaining request. The stop has to apply to failures that raised, not only to the
    server's own refusals.
    """
    c = fake_transport([{"ok": True}], status=401)
    results = c.call_many([("graph_upsert", {"i": i}) for i in range(3)], stop_on_error=True)
    assert [r["error_kind"] for r in results] == ["auth"]
    assert len(c.sent) == 1                      # calls 2 and 3 were never attempted


def test_call_many_raises_a_caller_bug_instead_of_calling_it_transport(fake_transport):
    """Arguments that cannot be sent are the caller's bug; `error_kind: transport` would send the
    reader of the batch to look at the network for it."""
    c = fake_transport([{"ok": True}])
    with pytest.raises(TypeError):
        c.call_many([("graph_upsert", {"x": object()})])      # will not serialise
    with pytest.raises(TypeError):
        c.call_many([("graph_upsert", 5)])                    # args are not a mapping
    assert c.sent == []                          # neither one reached the wire


def test_call_many_records_a_reply_that_is_neither_result_nor_error(fake_transport):
    """A body with no `result` is a protocol failure, not a caller bug: it becomes a row."""
    c = fake_transport([{"jsonrpc": "2.0", "id": 1}], raw=True)
    results = c.call_many([("graph_upsert", {"i": 0})])
    assert results[0]["ok"] is False and results[0]["error_kind"] == "transport"
    assert "KeyError" in results[0]["error"]     # str(KeyError) alone is just 'result'


def test_call_many_records_a_body_that_is_not_json_and_carries_on(fake_transport):
    """The ordinary case for the decode branch: something between client and server answers with
    an HTML page where the JSON-RPC reply should be. Without that branch the decode failure
    propagates and takes the rest of the batch with it — the exact failure call_many exists for."""
    c = fake_transport(["<html><body>502 Bad Gateway</body></html>",
                        {"ok": True, "node_id": "b"}])
    results = c.call_many([("graph_upsert", {"i": 0}), ("graph_upsert", {"i": 1})])
    assert results[0]["ok"] is False and results[0]["error_kind"] == "transport"
    assert "JSONDecodeError" in results[0]["error"]
    assert results[1]["node_id"] == "b"          # and the batch carried on past it
