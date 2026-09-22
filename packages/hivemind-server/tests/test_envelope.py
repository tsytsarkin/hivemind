import pytest
from hivemind_server.envelope import RO, WRITE, envelope
from hivemind_server.errors import Conflict, Invalid, NotFound


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
