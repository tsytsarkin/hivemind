"""The single decorator every MCP tool wears.

It existed three times over (mcp_tools, registry_tools, bus_ws_tools), each handling a different
subset of engine exceptions. Consolidated here because the per-call project argument injected
into every tool's schema has to be wired in exactly one place — three copies would mean three
chances to miss a tool.
"""
from __future__ import annotations

import functools
import inspect
from contextvars import ContextVar
from typing import Annotated, Callable, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

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
            msg = str(e)
            sep = " " if msg.endswith((".", "!", "?")) else ". "
            return {"ok": False, "error_kind": "conflict",
                    "error": f"{msg}{sep}Re-read the current state and retry against the "
                             f"current version."}
        except NotFound as e:
            return {"ok": False, "error_kind": "not_found", "error": str(e)}
        except Invalid as e:
            return _invalid(str(e))
        except Exception as e:                       # BusError and friends
            # Imported inside the handler so this leaf module stays free of the bus/asyncio
            # import chain; nothing here depends on bus_ws at import time.
            from .bus_ws import BusError
            if isinstance(e, BusError):
                return {"ok": False, "error_kind": "bus", "error": str(e)}
            raise
    return wrap


def _invalid(msg: str) -> dict:
    """The envelope's `invalid` shape, produced without a tool body having run.

    One definition, because project resolution happens OUTSIDE the body's own `envelope` (it
    decides which project the body may touch at all) and so cannot rely on that decorator to
    convert its refusal.
    """
    return {"ok": False, "error_kind": "invalid", "error": msg}


# ── per-call project resolution ────────────────────────────────────────────────────
# One MCP server now serves every project, so "which project" is an answer per CALL rather than
# per mount. It travels on contextvars, not on server-side session state: one token belongs to one
# person who runs several agents at once, and a "current project" held on the session would make
# two of their concurrent calls overwrite each other's destination.

# What the tool wrapper resolved for the call in flight. Set only by with_project, and the MCP
# transport runs each tool in its own copied context, so parallel calls cannot see each other's.
_PROJECT: ContextVar = ContextVar("hivemind_project", default=None)
# The project named by the URL this request arrived on (/p/<name>/…), or None on the neutral
# endpoint. Published per request by app.ProjectAuthMiddleware — after its ACL has passed.
_MOUNT_DEFAULT: ContextVar = ContextVar("hivemind_mount_default", default=None)
# Where names are resolved. A module global rather than a contextvar because there is one app per
# process: build_app -> build_mcp -> set_registry. Two apps built in one process (tests do this)
# share it, and the last one built wins; harmless there because each points at its own data dir
# and only one app serves requests at a time.
_REGISTRY = None

_PROJECT_ARG = Annotated[Optional[str], Field(
    description="Which project to act in. Required for tools that WRITE, unless the URL already "
                "names one; reads fall back to the project in the URL. Names you may use come "
                "back from the projects listing.")]


def set_registry(registry) -> None:
    global _REGISTRY
    _REGISTRY = registry


def set_mount_default(name: Optional[str]) -> None:
    _MOUNT_DEFAULT.set(name)


def current_project():
    """The project this call is for: what the tool wrapper resolved, else the mount it arrived on.

    The mount fallback is what the REST routes run on — a blob GET never passes through a tool
    wrapper, so nothing sets _PROJECT for it. It is safe because ProjectAuthMiddleware has already
    run the project ACL for that /p/<name> prefix before publishing the name, and it is not a way
    into the write path: resolve_project decides that separately, and refuses a write that named
    no project of its own.
    """
    proj = _PROJECT.get()
    if proj is not None:
        return proj
    name = _MOUNT_DEFAULT.get()
    return _REGISTRY.get(name) if (name and _REGISTRY is not None) else None


def visible_projects(who=None, registry=None) -> list:
    """Names the caller may actually use — safe to put in an error message, and the same list
    `GET /projects` answers with. Both arguments default to the request's own caller and the
    registry this process serves; a caller that already holds them passes them in rather than
    trusting process-wide state.
    """
    from .identity import current_identity
    from .projects_meta import can_access
    who = who or current_identity()
    registry = _REGISTRY if registry is None else registry
    if registry is None or who is None:
        return []
    return sorted(p.name for p in registry.all() if can_access(who, p.meta))


def _denied() -> Invalid:
    # Same wording whether the project is missing or forbidden — naming it would confirm it exists,
    # which is the oracle app.PROJECT_DENIED exists to remove.
    return Invalid("unknown project or not accessible with this token. "
                   f"Projects you can use: {', '.join(visible_projects()) or '(none)'}")


def resolve_project(explicit: Optional[str], *, requires: bool):
    """explicit argument -> the project in the URL -> refuse (writes) or explain (reads).

    There is deliberately no fall-back to the configured default project: an agent working in
    nik.private that forgot the argument would have had its write land in the shared graph, with
    no error to notice. A mount default is not that — the caller's own URL named it.
    """
    from .identity import current_identity
    from .projects_meta import can_access

    who = current_identity()
    name = explicit or _MOUNT_DEFAULT.get()
    if name is None:
        if requires:
            raise Invalid(
                "this tool writes, so it needs an explicit project= argument. Writing into a "
                "defaulted project is how private work ends up in the shared graph. "
                f"Projects you can use: {', '.join(visible_projects()) or '(none)'}")
        raise Invalid("no project for this call; pass project=<name> "
                      f"(available: {', '.join(visible_projects()) or 'none'})")
    project = _REGISTRY.get(name) if _REGISTRY is not None else None
    if project is None:
        raise _denied()
    if who is None:
        # HIVEMIND_REQUIRE_AUTH=0, the supported no-auth local mode: with no credential there is
        # nobody to authorize, so there is no ACL either — by construction, not by omission. Same
        # rule as ProjectAuthMiddleware._authorize, which is also what makes this branch
        # unreachable under auth: it 401s an unauthenticated call on the neutral endpoint and never
        # lets one reach a tool under /p/. test_the_neutral_endpoint_still_demands_a_token and
        # test_health_open_and_auth_required are what pin that.
        return project
    if not can_access(who, project.meta):
        raise _denied()
    return project


def with_project(fn: Callable, *, requires: bool) -> Callable:
    """Append `project` to the exposed signature, resolve it per call, publish it.

    The signature is what the SDK builds each tool's input schema from, so setting __signature__
    on this outer wrapper is what makes `project` a parameter the model can actually pass.
    """
    # eval_str=True because every tool module uses `from __future__ import annotations`: the
    # annotations reach us as strings, and a signature carrying strings makes pydantic build the
    # argument model against its OWN namespace, where `Optional` is undefined (measured:
    # "`graph_searchArguments` is not fully defined"). Evaluating here resolves them in the tool
    # module's namespace, where they were written.
    sig = inspect.signature(fn, eval_str=True)
    if "project" in sig.parameters:
        raise TypeError(f"{getattr(fn, '__name__', fn)!r} already takes a `project` argument; "
                        f"it would be shadowed by the injected one")

    @functools.wraps(fn)
    def wrap(*a, project: Optional[str] = None, **k):
        try:
            proj = resolve_project(project, requires=requires)
        except Invalid as e:
            return _invalid(str(e))
        token = _PROJECT.set(proj)
        try:
            out = fn(*a, **k)
        finally:
            _PROJECT.reset(token)
        if isinstance(out, dict):
            out.setdefault("project", proj.name)     # echoed so drift is visible
        return out

    params = list(sig.parameters.values())
    params.append(inspect.Parameter("project", inspect.Parameter.KEYWORD_ONLY,
                                    default=None, annotation=_PROJECT_ARG))
    wrap.__signature__ = sig.replace(parameters=params)
    return wrap


class ProjectAware:
    """Stands in for MCPServer so every tool registered through it gets project resolution.

    Wrapping registration rather than decorating 47 bodies: one wrapper, and a tool physically
    cannot be added to this surface without it.
    """

    def __init__(self, mcp):
        self._mcp = mcp

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def tool(self, *a, annotations=None, **kw):
        inner = self._mcp.tool(*a, annotations=annotations, **kw)
        # Unannotated means "treated as a write": a tool added without annotations then demands an
        # explicit project rather than quietly defaulting one. Loud beats leaky.
        requires = annotations is None or not annotations.read_only_hint

        def deco(fn):
            inner(with_project(fn, requires=requires))
            return fn
        return deco


class _Current:
    """Attribute proxy onto the project resolved for the current call.

    This is what lets `db = CurrentDb()` at module build time keep working inside all 47 tool
    bodies without touching any of them: the attribute is fetched during the call, when the
    project is known. Every use is a plain method call (db.read/db.write/db.meta_get,
    store.stat/...), which is why a proxy is enough — nothing type-checks the database, keys a
    dict by it, or compares it with `is`. Check that before adding a use.
    """

    def __init__(self, attr: Optional[str] = None):
        self._attr = attr

    def _target(self):
        p = current_project()
        if p is None:
            raise Invalid("no project resolved for this call; pass project=<name>")
        return getattr(p, self._attr) if self._attr else p

    def __getattr__(self, name):
        return getattr(self._target(), name)


def CurrentProject() -> _Current:
    return _Current()


def CurrentDb() -> _Current:
    return _Current("db")


def CurrentBlobs() -> _Current:
    return _Current("blobs")
