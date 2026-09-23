"""Create, list, inspect and share projects — the lifecycle, callable from a Claude session.

Sharing is owner-only on purpose. An admin who could share any project could grant themselves read
access to it, which would make the private tier decorative rather than a boundary (spec A16); a
member who could re-share would spread a graph past the people its owner approved, so the grant is
non-transitive too. `projects_meta.can_access` is blind to `is_admin` for the same reason.

Every refusal that concerns a project the caller may not reach uses ONE wording, byte-identical to
app.PROJECT_DENIED: "does not exist" and "not yours" must not be distinguishable, or the pair is the
existence oracle the private tier exists to remove.
"""
from __future__ import annotations

import shutil
import threading
from typing import Optional

from . import projects_meta as pm
from . import schemas
from .config import config
from .db import Invalid
from .identity import Identity, validate_username

# The cap is enforced here, but its name and default belong to config (HIVEMIND_MAX_PROJECTS_PER_USER
# / Config.max_projects_per_user) — a second copy of "50" in this module would drift from it. Set
# this to an int to override; None means "ask the config".
MAX_PER_USER: Optional[int] = None


def _cap() -> int:
    return MAX_PER_USER if MAX_PER_USER is not None else config().max_projects_per_user


SCHEMA_MODES = ("inherit", "interview", "bare")

DENIED = "unknown project or not accessible with this token"

# Creation is serialized in-process. The atomic mkdir below is what makes two *processes* safe; this
# makes the ordinary case — one server, several agent sessions on one token — deterministic, so a
# racing session adopts the winner's project instead of failing a create it cannot see the reason
# for. Held across the build (a few DB writes), which is acceptable: creation is rare.
_CREATE_LOCK = threading.Lock()


def _require_minted(who: Identity) -> None:
    if who.legacy:
        raise Invalid("creating a project needs a minted server-level identity; this token is a "
                      "legacy project token (hivemind-admin mint-token --user <you>)")


def _owned_count(reg, user: str) -> int:
    return sum(1 for p in reg.all() if p.meta.owner == user)


def create(reg, who: Identity, name: str, visibility: str = "private", *, label: str = "",
           session: Optional[str] = None, schema: str = "inherit", source=None) -> dict:
    _require_minted(who)
    if visibility not in pm.VISIBILITIES:
        raise Invalid(f"visibility must be one of {pm.VISIBILITIES}")
    if schema not in SCHEMA_MODES:
        raise Invalid(f"schema must be one of {SCHEMA_MODES}")
    # Before anything touches the disk: a pathological name must never reach mkdir.
    pm.validate_project_name(name)
    # At BOTH tiers, so a dotted namespace is genuinely unsquattable: without this anybody could
    # pre-create a SHARED nik.scratch and nik's own private create would resolve onto it.
    pm.check_name_prefix(who.user, name)
    if visibility == "private":
        pm.check_private_name(who.user, name)

    with _CREATE_LOCK:
        existing = reg.get(name)
        if existing is not None:
            if not pm.can_access(who, existing.meta):
                raise Invalid(DENIED)
            return _existing(name, existing.meta, visibility)
        cap = _cap()
        if _owned_count(reg, who.user) >= cap:
            raise Invalid(f"per-user project cap reached ({cap}); "
                          f"reuse an existing project or raise HIVEMIND_MAX_PROJECTS_PER_USER")
        return _build(reg, who, name, visibility, label=label, session=session, schema=schema,
                      source=source)


def _existing(name: str, meta, requested: str) -> dict:
    """The answer for a name that exists already and the caller may use.

    Refuses when the visibility asked for is not the visibility it HAS. Silently handing back a
    shared project to a caller that asked for a private one is how private work lands in a graph
    every user can read: the agent would have to re-read this field to notice, and the caller — not
    the server — is the one who can decide which it wanted.
    """
    if meta.visibility != requested:
        raise Invalid(f"{name} already exists and is {meta.visibility}, not {requested}. Use it as "
                      f"it is (pass project={name}) or choose another name.")
    return {"project": name, "existing": True, "visibility": meta.visibility}


def _build(reg, who: Identity, name: str, visibility: str, *, label: str, session: Optional[str],
           schema: str, source) -> dict:
    # Claim the name with an atomic mkdir BEFORE building anything, so two sessions racing on the
    # same name cannot both proceed and leave a half-built directory behind.
    target = reg.root / name
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _adopt(reg, who, name, visibility)
    try:
        # The creator is recorded as owner whatever the visibility: can_access ignores `owner` for a
        # shared project, so this grants nothing extra, but it is what makes the per-user cap count
        # everything a caller created — without it, visibility='shared' is an unbounded-creation
        # bypass.
        meta = pm.ProjectMeta(name=name, visibility=visibility, owner=who.user,
                              label=label[:200], session=session)
        # The metadata goes down BEFORE the Project is constructed: Project.__init__ stamps
        # visibility=shared when project.json is absent, and ProjectRegistry.create publishes the
        # project on return — so the other order would let a concurrently served request read a
        # brand-new private project as shared, i.e. as readable by everyone.
        pm.save(target, meta)
        project = reg.create(name)
        from . import guide
        guide.ensure_core_guide(project.db)
        copied = 0
        if schema == "inherit":
            src = source if source is not None else reg.get(reg.default_name)
            if src is not None and src.name != name:
                copied = schemas.copy_types(src.db, project.db, agent=who.user)
    except Exception:
        # The name was claimed before the build, so an unwind has to release both halves of the
        # claim or the name stays dead until the server restarts.
        shutil.rmtree(target, ignore_errors=True)
        reg.forget(name)
        raise
    # A project's types ARE its meaning, so the response says how to get them rather than leaving
    # the agent to invent a vocabulary before it understands the work.
    nxt = {
        "inherit": f"pass project={name} on your Hivemind calls; it inherited {copied} node/edge "
                   f"type(s) from the source project",
        "interview": f"this project has NO schema yet. Load the `hivemind-schema` skill and run "
                     f"the interview with the user, then apply the result. Pass project={name} on "
                     f"your Hivemind calls.",
        "bare": f"this project has no schema by choice — define types with schema_propose as the "
                f"work demands them. Pass project={name} on your Hivemind calls.",
    }[schema]
    return {"project": name, "existing": False, "visibility": visibility, "schema": schema,
            "next": nxt,
            "note": "the project-neutral /mcp endpoint serves it now; its own /p/<name>/ REST "
                    "routes (blob upload, bus) appear after the next server restart"}


def _adopt(reg, who: Identity, name: str, visibility: str) -> dict:
    """The mkdir claim lost: something already holds this name on disk.

    Either a concurrent creation in another process, or a project created since the registry last
    discovered. Deliberately does NOT construct a Project before the ACL passes: Project.__init__
    would stamp `shared` over a private project's metadata — the one write here that could turn
    somebody else's private graph into a world-readable one. Once can_access has passed, project.json
    demonstrably parsed (a missing or corrupt one reads back private and ownerless, which nobody can
    access), so constructing it re-stamps nothing.
    """
    meta = pm.load(reg.root / name, name)
    if not pm.can_access(who, meta):
        raise Invalid(DENIED)
    out = _existing(name, meta, visibility)     # same mismatch guard as the registry branch
    reg.create(name)
    return out


def listing(reg, who: Identity) -> dict:
    shared, mine, with_me = [], [], []
    for p in reg.all():
        meta = p.meta
        if not pm.can_access(who, meta):
            continue
        if meta.visibility == "shared":
            shared.append(meta.name)
        elif meta.owner == who.user:
            mine.append(meta.name)
        else:
            with_me.append(meta.name)
    return {"shared": sorted(shared), "mine": sorted(mine), "shared_with_me": sorted(with_me),
            "hint": "pass project=<name> on your Hivemind calls"}


def info(reg, who: Identity, name: str) -> dict:
    project = reg.get(name)
    if project is None or not pm.can_access(who, project.meta):
        raise Invalid(DENIED)
    return project.meta.public(who)


def _owner_only(reg, who: Identity, name: str):
    project = reg.get(name)
    if project is None or not pm.can_access(who, project.meta):
        raise Invalid(DENIED)
    meta = project.meta
    if meta.visibility == "shared":
        raise Invalid(f"{name} is already shared with everyone; there is nothing to grant")
    if meta.owner != who.user:
        # Not "and you are not the owner of X" — the caller can reach this project, so naming it
        # leaks nothing; what this must not do is accept the change from a member or an admin.
        raise Invalid(f"only the owner of {name} can change who it is shared with")
    return project, meta


def share(reg, who: Identity, name: str, user: str, identities=None) -> dict:
    validate_username(user)
    project, meta = _owner_only(reg, who, name)
    if identities is not None and not identities.has_user(user):
        # A share is silent until the grantee calls, so a typo would look like it worked and grant
        # nobody.
        raise Invalid(f"no such user {user!r}; mint them a token first "
                      f"(hivemind-admin mint-token --user {user})")
    if user == meta.owner:
        raise Invalid("the owner already has access")
    if user not in meta.members:
        meta.members.append(user)
        pm.save(project.dir, meta)
    return {"project": name, "members": meta.members}


def unshare(reg, who: Identity, name: str, user: str) -> dict:
    validate_username(user)
    project, meta = _owner_only(reg, who, name)
    if user == meta.owner:
        raise Invalid("the owner cannot be removed from their own project")
    if user not in meta.members:
        # A no-op here tells the owner access was revoked when it was not, which they learn only
        # when the ex-member is still reading. The caller is the owner, so naming the current
        # members leaks nothing they cannot already read with project_info.
        raise Invalid(f"{user} is not a member of {name}; its members are "
                      f"{', '.join(meta.members) or '(nobody)'}")
    meta.members = [m for m in meta.members if m != user]
    pm.save(project.dir, meta)
    return {"project": name, "members": meta.members}


def attach(mcp, registry, identities) -> None:
    """Register the lifecycle tools. `mcp` must be the REAL MCPServer, not envelope.ProjectAware:
    these tools are ABOUT projects rather than IN one, and their own `project` argument means a
    different thing from the injected per-call one, which would shadow it."""
    from .envelope import RO, WRITE, current_project, envelope as _envelope, visible_projects
    from .identity import current_identity

    def _caller(action: str) -> Identity:
        who = current_identity()
        if who is None:
            # HIVEMIND_REQUIRE_AUTH=0: there is no person to own a project or to grant it to, so
            # these tools have nothing to act as. Refused explicitly rather than reaching
            # can_access(None, …), which denies with a message about projects instead of about the
            # deployment's mode.
            raise Invalid(f"{action} needs a bearer token that names a person; this deployment is "
                          f"running with authentication off "
                          f"(hivemind-admin mint-token --user <you>)")
        return who

    @mcp.tool(annotations=RO, description="List the projects you can use, grouped: shared with "
                                          "everyone, yours, and shared with you. Pass the name as "
                                          "project=<name> on other calls.")
    @_envelope
    def project_list() -> dict:
        who = current_identity()
        if who is None:
            # The one tool that still answers with auth off: every agent is told to call it to
            # learn the names it may pass, so it must not be a dead end. visible_projects is the
            # single definition of "projects you can use" — GET /projects answers with the same one.
            return {"shared": visible_projects(), "mine": [], "shared_with_me": [],
                    "hint": "pass project=<name> on your Hivemind calls"}
        return listing(registry, who)

    @mcp.tool(annotations=WRITE,
              description="Create a project. visibility='private' (only you, must be named "
                          "<you>.<suffix>) or 'shared' (everyone). schema='inherit' copies the "
                          "node/edge types of the project you are working in, else of the default "
                          "one (default); 'interview' creates it empty and tells you to run the "
                          "hivemind-schema skill, which asks the user about their work and builds "
                          "a schema from the answers; 'bare' leaves it empty for you to define "
                          "types as you go. ASK THE USER which they want rather than choosing for "
                          "them.")
    @_envelope
    def project_create(name: str, visibility: str = "private", label: str = "",
                       session: Optional[str] = None, schema: str = "inherit") -> dict:
        # current_project() is the project this request's URL named, if any — the one the agent is
        # working in. None on the neutral endpoint, where create falls back to the default project.
        return create(registry, _caller("creating a project"), name, visibility, label=label,
                      session=session, schema=schema, source=current_project())

    @mcp.tool(annotations=RO, description="Metadata for one project you can access.")
    @_envelope
    def project_info(project: str) -> dict:
        return info(registry, _caller("reading a project's metadata"), project)

    @mcp.tool(annotations=WRITE, description="Grant another user access to a private project you "
                                             "own. Owner only — members cannot re-share.")
    @_envelope
    def project_share(project: str, user: str) -> dict:
        return share(registry, _caller("sharing a project"), project, user, identities)

    @mcp.tool(annotations=WRITE, description="Revoke a user's access to a private project you own. "
                                             "Effective on their next call.")
    @_envelope
    def project_unshare(project: str, user: str) -> dict:
        return unshare(registry, _caller("unsharing a project"), project, user)
