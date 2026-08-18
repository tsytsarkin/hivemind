"""ID + hashing helpers. ULIDs give lexicographically-sortable, time-ordered ids with no deps."""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def ulid() -> str:
    """26-char Crockford-base32 ULID: 48-bit ms timestamp + 80 random bits."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _b32(ms, 10) + _b32(rand, 16)


def content_hash(obj: Any) -> str:
    """sha256 of canonical JSON; used for version dedup/idempotency."""
    from .db import canonical_json

    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
