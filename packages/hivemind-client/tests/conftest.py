"""Fixtures for the client tests: a hermetic transport seam.

`Client` reaches the server through one `httpx.Client` held on `self._http`, so swapping that for
an `httpx.MockTransport` keeps everything the tests care about — retry policy, status handling,
JSON-RPC unwrapping, `ok: false` handling — under test while touching no socket.
"""
import json

import httpx
import pytest

from hivemind import Client


class QueueExhausted(BaseException):
    """The client made more calls than the test queued replies for.

    A BaseException on purpose: an `AssertionError` raised in the handler is an `Exception`, and a
    client that catches broadly (`call_many` did) would swallow it and turn over-calling into an
    ordinary failed-call row — a test could then pass while the client made calls it should not
    have. Nothing catches BaseException, so this always reaches pytest.
    """


def _stub_client(handler, **kw) -> Client:
    c = Client("http://stub.invalid/p/t", "tok", agent="test", **kw)
    c._http.close()                       # drop the real one built in __init__
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


@pytest.fixture()
def fake_transport():
    """Build a Client that answers tool calls with the queued payloads, in order.

    Each payload is handed back as the tool result's `structuredContent`, which is the shape the
    server sends and the first thing `_tool_payload` looks at. `status` sets the HTTP status of
    every reply, for the HTTP-level failure cases. With `raw=True` each queued item is instead the
    whole JSON-RPC response body, verbatim — that is how a test reaches a reply that is not a
    `structuredContent` dict: a bare text content block, or a body carrying neither `result` nor
    `error`. A queued **string** is sent as the response body verbatim with `text/html`, i.e. a
    reply that is not JSON at all (queue `{"structuredContent": "..."}` with `raw=True` if what
    you want is a string *payload*). The returned client carries `.sent`, the list of `params`
    actually put on the wire, so a test can check what was and was not sent.
    """
    made = []

    def make(payloads, *, status=200, raw=False, **kw):
        replies = list(payloads)
        sent = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent.append(body["params"])
            if not replies:
                raise QueueExhausted(f"call {len(sent)} to {body['params']['name']!r} has no "
                                     f"queued reply")
            item = replies.pop(0)
            if isinstance(item, str):
                return httpx.Response(status, text=item,
                                      headers={"content-type": "text/html; charset=utf-8"})
            return httpx.Response(status, json=item if raw else {
                "jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": item}})

        c = _stub_client(handler, **kw)
        c.sent = sent
        made.append(c)
        return c

    yield make
    for c in made:
        c.close()


@pytest.fixture()
def fake_transport_raising():
    """Build a Client whose transport always fails, as a dead server or a DNS failure would.

    `max_retries=0` so `_request` re-raises on the first attempt: with retries on, the client
    would sleep through its backoff before the test could see the failure.
    """
    made = []

    def make(exc=None):
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc or httpx.ConnectError("connection refused", request=request)

        c = _stub_client(handler, max_retries=0)
        made.append(c)
        return c

    yield make
    for c in made:
        c.close()
