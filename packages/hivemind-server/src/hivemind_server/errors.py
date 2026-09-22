"""Stable import surface for the engine's error types.

`Conflict`, `NotFound` and `Invalid` are still defined in `db.py` (the low-level modules that
raise them already import from there, and moving the definitions would mean rewriting every one
of those call sites for no behavioural gain). This module just re-exports the same class objects
so callers that only need to catch/raise them — the tool envelope, later the project-access
checks — don't need to import the whole SQLite access layer to do it. Same objects, so
`isinstance` checks are identical either way.
"""
from __future__ import annotations

from .db import Conflict, Invalid, NotFound

__all__ = ["Conflict", "Invalid", "NotFound"]
