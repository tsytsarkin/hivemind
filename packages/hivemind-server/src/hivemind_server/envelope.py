"""The single decorator every MCP tool wears.

It existed three times over (mcp_tools, registry_tools, bus_ws_tools), each handling a different
subset of engine exceptions. Consolidated here because the per-call project resolution in Task 6
has to be injected in exactly one place — three copies would mean three chances to miss a tool.
"""
from __future__ import annotations

import functools
from typing import Callable

from mcp.types import ToolAnnotations

from .db import Conflict, Invalid, NotFound

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)


def envelope(fn: Callable) -> Callable:
    """Run a tool body; convert engine exceptions into an actionable, self-correctable result."""
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            out = fn(*a, **k)
            if isinstance(out, dict) and "ok" not in out:
                out = {"ok": True, **out}
            return out
        except Conflict as e:
            # Deliberately NOT graph-specific: registry.py and skills.py raise Conflict for a
            # duplicate immutable publish, where advising a graph_get would send the caller on a
            # useless detour. The raiser's own message carries the specific remedy.
            return {"ok": False, "error_kind": "conflict",
                    "error": f"{e} Re-read the current state and retry against the current version."}
        except NotFound as e:
            return {"ok": False, "error_kind": "not_found", "error": str(e)}
        except Invalid as e:
            return {"ok": False, "error_kind": "invalid", "error": str(e)}
        except Exception as e:                       # BusError and friends
            # Imported lazily: bus_ws imports nothing from here, and a module-level import would
            # make the dependency circular.
            from .bus_ws import BusError
            if isinstance(e, BusError):
                return {"ok": False, "error_kind": "bus", "error": str(e)}
            raise
    return wrap
