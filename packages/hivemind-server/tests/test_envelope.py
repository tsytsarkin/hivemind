import pytest
from hivemind_server.envelope import RO, WRITE, envelope
from hivemind_server.db import Conflict, Invalid, NotFound


def test_success_gets_an_ok_flag():
    @envelope
    def f():
        return {"value": 1}
    assert f() == {"ok": True, "value": 1}


def test_ok_is_not_overwritten_when_the_body_sets_it():
    @envelope
    def f():
        return {"ok": False, "error": "mine"}
    assert f() == {"ok": False, "error": "mine"}


@pytest.mark.parametrize("exc,kind", [(Conflict("stale"), "conflict"),
                                      (NotFound("gone"), "not_found"),
                                      (Invalid("bad"), "invalid")])
def test_engine_errors_become_actionable_results(exc, kind):
    @envelope
    def f():
        raise exc
    out = f()
    assert out["ok"] is False and out["error_kind"] == kind and out["error"]


def test_the_conflict_hint_is_domain_neutral():
    """registry.py/skills.py raise Conflict for a duplicate immutable publish; a graph_get
    pointer would send that caller on a useless detour, so the hint must not name it."""
    @envelope
    def f():
        raise Conflict("tool/x@1.0.0 already published (immutable). Bump the version.")
    out = f()
    assert out["ok"] is False and out["error_kind"] == "conflict"
    assert "graph_get" not in out["error"]


def test_the_conflict_hint_is_punctuation_aware():
    """graph.py's Conflict messages (e.g. "head is X, you sent Y") don't end in a period, so the
    appended hint needs its own separator there — but must not double up when the raiser's
    message (registry.py/skills.py) already ends in one."""
    @envelope
    def no_period():
        raise Conflict("head is 01ABC, you sent 01XYZ")
    assert ". Re-read" in no_period()["error"]

    @envelope
    def has_period():
        raise Conflict("already published (immutable). Bump the version.")
    assert ".. " not in has_period()["error"]


def test_bus_errors_are_handled_too():
    """bus_ws_tools had its own envelope for this; the consolidated one must keep it."""
    from hivemind_server.bus_ws import BusError

    @envelope
    def f():
        raise BusError("no peer 'x'")
    out = f()
    assert out["ok"] is False and out["error_kind"] == "bus"


def test_the_wrapped_signature_is_preserved():
    """The SDK generates each tool's schema from the signature, so wrapping must not erase it."""
    import inspect

    @envelope
    def f(node_id: str, limit: int = 5) -> dict:
        return {}
    assert list(inspect.signature(f).parameters) == ["node_id", "limit"]


def test_annotations_are_distinguishable():
    assert RO.read_only_hint is True and WRITE.read_only_hint is False


def test_an_unrelated_exception_is_not_swallowed():
    @envelope
    def f():
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        f()
