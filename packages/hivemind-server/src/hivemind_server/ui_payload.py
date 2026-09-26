"""Bound browser JSON before buffering, including chunked requests without a length header."""
from __future__ import annotations

import json

from .db import Invalid


class TooLarge(Exception):
    pass


async def read_json(req, *, max_bytes: int = 128 * 1024):
    declared = req.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise TooLarge("request body exceeds permitted size")
        except ValueError as exc:
            raise Invalid("invalid Content-Length") from exc
    body = bytearray()
    async for piece in req.stream():
        if len(body) + len(piece) > max_bytes:
            raise TooLarge("request body exceeds permitted size")
        body.extend(piece)
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Invalid("invalid JSON request body") from exc
