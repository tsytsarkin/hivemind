"""A refused write must not be able to kill a batch.

The failure behind these tests: a batch of writes was running, one row carried a value outside its
type's enum, the tool answered `{"ok": false}`, `call()` raised, and the exception took down the
whole batch mid-run — leaving the graph half-updated with no record of where it stopped.

`call()` still raises by default, because every convenience wrapper and the whole CLI are built on
that and would otherwise return refusals nobody checks. The batch path is `call_many`, which never
raises: every item gets a reply, refusal or not.
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
