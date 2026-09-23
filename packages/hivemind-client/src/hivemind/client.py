"""Sync client for a hivemind project endpoint. One dependency: httpx.

base_url points at a project: http://host:8787/p/<project> . MCP tools are invoked over the
same /mcp endpoint the plugin uses (2026-07-28 streamable HTTP); artifacts stream over REST.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

PROTO = "2026-07-28"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class HivemindError(Exception):
    def __init__(self, message: str, *, kind: Optional[str] = None):
        super().__init__(message)
        self.kind = kind


class Client:
    def __init__(self, base_url: str, token: str, *, agent: str = "client",
                 timeout: float = 600.0, max_retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent = agent
        self.max_retries = max_retries
        self._http = httpx.Client(timeout=timeout)
        from .artifacts import Artifacts
        self.artifacts = Artifacts(self)
        from . import tools as _tools
        self._tools = _tools

    # ── low-level ────────────────────────────────────────────────────────────────
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = self.base_url + path
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._http.request(method, url, headers={**self._auth(),
                                                             **kw.pop("headers", {})}, **kw)
            except httpx.TransportError:
                if attempt > self.max_retries:
                    raise
                time.sleep(_backoff(attempt))
                continue
            if r.status_code in _RETRY_STATUS and attempt <= self.max_retries:
                delay = _retry_after(r) or _backoff(attempt)
                time.sleep(delay)
                continue
            return r

    # ── MCP tool calls ───────────────────────────────────────────────────────────
    def call(self, tool: str, arguments: Optional[dict] = None, *, _id: int = 1,
             raise_on_error: bool = True) -> Any:
        """Invoke one MCP tool and return its payload.

        A tool that refuses — `{"ok": false, ...}`, e.g. a value outside a type's enum — raises
        `HivemindError` by default, which is what every wrapper below and the CLI are built on.
        Pass `raise_on_error=False` to get that envelope back as data instead; it changes nothing
        below the envelope, so auth, HTTP and JSON-RPC failures still raise either way — there is
        no reply from the tool to return. For a batch, use `call_many`, which records a failed
        call instead of raising it.
        """
        arguments = dict(arguments or {})
        arguments.setdefault("agent", self.agent)
        params = {"name": tool, "arguments": arguments,
                  "_meta": {"io.modelcontextprotocol/protocolVersion": PROTO,
                            "io.modelcontextprotocol/clientInfo": {"name": "hivemind-client",
                                                                    "version": "0.1.0"},
                            "io.modelcontextprotocol/clientCapabilities": {}}}
        body = {"jsonrpc": "2.0", "id": _id, "method": "tools/call", "params": params}
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTO, "Mcp-Method": "tools/call",
                   "Mcp-Name": tool}
        r = self._request("POST", "/mcp", json=body, headers=headers)
        if r.status_code == 401:
            raise HivemindError("unauthorized (bad token)", kind="auth")
        if r.status_code >= 400:
            raise HivemindError(f"HTTP {r.status_code}: {r.text[:300]}")
        result = _parse_rpc(r)
        if "error" in result:
            raise HivemindError(result["error"].get("message", "rpc error"), kind="rpc")
        payload = _tool_payload(result["result"])
        if raise_on_error and isinstance(payload, dict) and payload.get("ok") is False:
            raise HivemindError(payload.get("error", "tool error"),
                                kind=payload.get("error_kind"))
        return payload

    def call_many(self, calls: Iterable[Tuple[str, Optional[dict]]], *,
                  stop_on_error: bool = False) -> list:
        """Run a batch to completion, returning one reply per call, in order.

        A single refused row must not be able to abandon the rest: the failure this exists for was
        one out-of-enum value aborting a whole reaper batch mid-run, leaving the graph
        half-updated with no record of where it stopped. So a call that fails is recorded here
        rather than raised. A tool's reply is passed through exactly as the server sent it,
        refusal included; a call that raised instead is recorded in its place as
        `{"ok": false, "error_kind": ..., "error": ...}` — the `HivemindError.kind` when it has
        one (`auth`, `rpc`, and whatever the tool's envelope named), `"error"` when it has none
        (an HTTP-status failure carries no kind), and `"transport"` when no usable reply came back
        at all.

        What still raises is the caller's own bug: an item that is not a `(tool, args)` pair,
        `args` that is not a mapping, or an argument that will not serialise. Recording those as
        failed calls would send whoever reads the batch looking at the network for a mistake in
        their own arguments.

        With `stop_on_error=True` the batch stops after the first reply that is not `ok` — however
        it failed — and that reply is in the returned list: the record of where it stopped is the
        point.
        """
        out: list = []
        for tool, args in calls:
            try:
                out.append(self.call(tool, args, raise_on_error=False))
            except HivemindError as e:
                out.append({"ok": False, "error_kind": e.kind or "error", "error": str(e)})
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                # No usable reply: the connection or timeout (httpx), a body that would not
                # decode, or one carrying neither `result` nor `error` (the KeyError). Narrow on
                # purpose — a broad `except Exception` here stamped `TypeError: Object of type
                # object is not JSON serializable` as a transport failure, which is a caller's bug
                # wearing a network's clothes. The type name is kept because `str(KeyError)` is
                # just `'result'`.
                out.append({"ok": False, "error_kind": "transport",
                            "error": f"{type(e).__name__}: {e}"})
            if stop_on_error and not _is_ok(out[-1]):
                break
        return out

    # ── convenience wrappers ─────────────────────────────────────────────────────
    def upsert(self, type: str, props: dict, **kw) -> dict:
        return self.call("graph_upsert", {"type": type, "props": props, **kw})

    def get(self, node_id: Optional[str] = None, **kw) -> dict:
        return self.call("graph_get", {"node_id": node_id, **kw})

    def link(self, edge_type: str, src: str, dst: str, props: Optional[dict] = None, **kw) -> dict:
        return self.call("graph_link", {"edge_type": edge_type, "src": src, "dst": dst,
                                        "props": props or {}, **kw})

    def search(self, query: str = "", **kw) -> dict:
        return self.call("graph_search", {"query": query, **kw})

    def schema(self, **kw) -> dict:
        return self.call("schema_get", kw)

    def guide(self, section: Optional[str] = None) -> dict:
        return self.call("guide_get", {"section": section} if section else {})

    def health(self) -> dict:
        # /healthz lives at server root, above the project prefix
        root = self.base_url.rsplit("/p/", 1)[0]
        return self._http.get(root + "/healthz").json()


    def tool_publish(self, path, *, id, version, **kw):
        return self._tools.publish(self, path, id=id, version=version, **kw)

    def tool_get(self, id, *, constraint="", dest_dir=".", **kw):
        return self._tools.get(self, id, constraint=constraint, dest_dir=dest_dir, **kw)

    def tool_search(self, query="", **kw):
        return self._tools.search(self, query, **kw)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _is_ok(reply: Any) -> bool:
    """Did a reply from `call_many` succeed? The same test `call` raises on, negated.

    Deliberately `is False` rather than a truth test on a missing key. Two replies a truth test
    gets wrong: `_tool_payload` hands back a bare string when a tool answers with an unstructured
    content block, and `.get` on that is an AttributeError out of a method whose whole contract is
    that it records failures rather than raising them; and a dict with no `ok` key at all is a
    success to `call` (it raises only on `ok is False`), so treating it as a failure here would
    stop a batch on a reply the single-call path calls fine. The server's envelope does add
    `ok: true` to a dict reply that carries no `ok` of its own — but the two paths have to agree
    by construction, not because of what the other side of the wire happens to send.
    """
    return not (isinstance(reply, dict) and reply.get("ok") is False)


def _backoff(attempt: int) -> float:
    return min(0.25 * (2 ** attempt), 8.0) * (0.5 + random.random())


def _retry_after(r: httpx.Response) -> Optional[float]:
    v = r.headers.get("retry-after")
    if v and v.isdigit():
        return float(v)
    return None


def _parse_rpc(r: httpx.Response) -> dict:
    ct = r.headers.get("content-type", "")
    if ct.startswith("text/event-stream"):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise HivemindError("empty SSE response")
    return r.json()


def _tool_payload(result: dict) -> Any:
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    content = result.get("content") or []
    for block in content:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (ValueError, KeyError):
                return block["text"]
    return result
