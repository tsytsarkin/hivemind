"""Streaming artifact upload/download for the hivemind client. Bytes go over REST /blobs; the
digest is content-addressed sha256. Upload hashes first (to form the URL), then streams; download
verifies the digest while writing.
"""
from __future__ import annotations

import hashlib
import os
from typing import Iterator, Optional

import httpx

_CHUNK = 1024 * 1024


def sha256_file(path: str) -> "tuple[str, int]":
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            size += len(b)
            h.update(b)
    return "sha256:" + h.hexdigest(), size


def _stream_file(path: str) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            yield b


class Artifacts:
    def __init__(self, client):
        self._c = client

    def _url(self, digest: str) -> str:
        return f"/blobs/{digest.replace(':', '/', 1)}"

    def put(self, path: str, *, media_type: Optional[str] = None,
            attach_to: Optional[str] = None, role: str = "attachment") -> dict:
        """Upload a file; returns {digest,size,deduplicated}. Idempotent (digest-addressed).

        Pass attach_to=<version_id> to attach in the same request — an upload that is never
        attached is invisible to other agents and is garbage-collected.
        """
        digest, size = sha256_file(path)
        url = self._url(digest)
        if attach_to:
            import urllib.parse as _u
            url += "?" + _u.urlencode({"attach_to": attach_to, "role": role,
                                       "filename": os.path.basename(path)})
        # HEAD first: skip the upload if the server already has these bytes
        head = self._c._request("HEAD", self._url(digest))
        if head.status_code == 200:
            return {"digest": digest, "size": size, "deduplicated": True, "skipped_upload": True}
        headers = {"Content-Type": media_type or "application/octet-stream",
                   "X-Hivemind-Agent": self._c.agent}
        r = self._c._request("PUT", url, content=_stream_file(path), headers=headers)
        if r.status_code not in (200, 201):
            from .client import HivemindError
            raise HivemindError(f"upload failed HTTP {r.status_code}: {r.text[:300]}")
        out = r.json()
        out.setdefault("digest", digest)
        out.setdefault("size", size)
        return out

    def get(self, digest: str, dest: str) -> dict:
        """Download to dest, verifying the sha256 as it writes. Returns {digest,size,path}."""
        url = self._url(digest)
        h = hashlib.sha256()
        size = 0
        tmp = dest + ".part"
        with self._c._http.stream("GET", self._c.base_url + url,
                                  headers=self._c._auth()) as r:
            if r.status_code == 404:
                from .client import HivemindError
                raise HivemindError(f"artifact {digest} not found", kind="not_found")
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(_CHUNK):
                    size += len(chunk)
                    h.update(chunk)
                    f.write(chunk)
        computed = "sha256:" + h.hexdigest()
        if computed != digest:
            os.unlink(tmp)
            from .client import HivemindError
            raise HivemindError(f"integrity check failed: got {computed}, expected {digest}")
        os.replace(tmp, dest)
        return {"digest": digest, "size": size, "path": dest}
