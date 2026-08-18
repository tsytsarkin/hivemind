"""Sync client for a hivemind project endpoint. One dependency: httpx.

base_url points at a project: http://host:8787/p/<project> . MCP tools are invoked over the
same /mcp endpoint the plugin uses (2026-07-28 streamable HTTP); artifacts stream over REST.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, Optional

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
    def call(self, tool: str, arguments: Optional[dict] = None, *, _id: int = 1) -> Any:
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
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise HivemindError(payload.get("error", "tool error"),
                                kind=payload.get("error_kind"))
        return payload

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

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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
