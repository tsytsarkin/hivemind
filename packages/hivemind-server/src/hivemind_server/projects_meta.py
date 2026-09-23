"""Per-project metadata and the one access rule.

A project used to be a bare directory; what kept projects apart was an accident — each held its own
tokens.json, so a token for one simply did not verify against another. Server-level identity
removes that accident, so the boundary has to become explicit and it has to live somewhere the
blob REST routes pass through as well (see app.ProjectAuthMiddleware).

This module decides only *what a project is and who may reach it*. It never denies anything itself:
enforcement belongs to the middleware and the project tools, so there is exactly one predicate
(`can_access`) for every surface to agree on.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .db import Invalid, now_iso
from .identity import Identity

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VISIBILITIES = ("shared", "private")


def validate_project_name(name: str) -> str:
    # fullmatch, not match: `$` alone matches just before a trailing newline, so match() would
    # accept "default\n" as if it were "default" — a second, invisible project directory next to
    # the real one. Same trap as identity.validate_username.
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Invalid(f"invalid project name {name!r}: want {NAME_RE.pattern}")
    return name


def check_private_name(user: str, name: str) -> None:
    """A private project must be `<user>.<suffix>`, so ownership is legible and unsquattable."""
    validate_project_name(name)
    prefix = f"{user}."
    if not name.startswith(prefix):
        raise Invalid(f"a private project must be named {user}.<suffix> (got {name!r})")
    suffix = name[len(prefix):]
    if not suffix or any(not part for part in suffix.split(".")):
        raise Invalid(f"private project {name!r} has an empty name segment; "
                      f"use {user}.<suffix> with a non-empty suffix")


@dataclass
class ProjectMeta:
    name: str
    visibility: str = "shared"
    owner: Optional[str] = None
    members: list = field(default_factory=list)
    label: str = ""
    created: str = ""
    session: Optional[str] = None
    last_touched: str = ""

    def as_json(self) -> dict:
        return {"name": self.name, "visibility": self.visibility, "owner": self.owner,
                "members": list(self.members), "label": self.label, "created": self.created,
                "session": self.session, "last_touched": self.last_touched}

    def copy(self) -> "ProjectMeta":
        # as_json() already copies `members`, so this is an independent copy — unlike
        # dataclasses.replace(self), which would hand back a meta sharing the same member list.
        return ProjectMeta(**self.as_json())

    def public(self, who: Optional[Identity] = None) -> dict:
        out = self.as_json()
        # Member lists are the owner's business.
        if not who or (who.user != self.owner and who.user not in self.members):
            out.pop("members", None)
        return out


# (project.json path, name) -> (stamp, meta, problem). The ACL is consulted on every request, so
# re-reading the file each time is not acceptable; same stamp trick as auth.TokenStore. The PATH is
# the key, not the project name: two deployments can hold a same-named project, and on a filesystem
# with coarse mtime granularity two same-sized files would then collide on one cache entry and
# answer with each other's owner.
_CACHE: dict = {}


def _key(project_dir: Path, name: str) -> tuple:
    # One definition of the cache key, so load() and save() cannot drift into two spellings of it
    # and leave save() unable to evict what load() stored.
    return (str(project_dir / "project.json"), name)


def _closed(name: str) -> ProjectMeta:
    """The fail-closed value: private, ownerless, therefore reachable by nobody."""
    return ProjectMeta(name=name, visibility="private", owner=None, members=[])


def _read(path: Path, name: str) -> tuple[ProjectMeta, Optional[str]]:
    """Parse project.json, or fail closed. A metadata file we cannot read is an unreadable ACL, and
    an unreadable ACL denies — a truncated write must make a project unreachable for a moment, never
    make a private one world-readable.

    The second element is why it failed closed, for an operator-facing log; None when it parsed.
    """
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        vis = raw.get("visibility")
        if vis not in VISIBILITIES:
            # Unknown or missing visibility is not a shared project; it is an unreadable ACL.
            return _closed(name), f"project.json has visibility {vis!r}, want one of {VISIBILITIES}"
        members = raw.get("members")
        if members is not None and not isinstance(members, list):
            # NEVER coerce an ill-typed member list: list("ab") is ['a', 'b'] and
            # list({"ana": 1}) is ['ana'], so a hand-corrupted file would GRANT access to the users
            # a, b and ana — single-character usernames are legal. Every other corruption mode here
            # denies; this is the only one that could invert that, so it fails closed too.
            return _closed(name), (f"project.json has a {type(members).__name__} members field, "
                                   f"want a list")
        return ProjectMeta(name=name, visibility=vis, owner=raw.get("owner"),
                           members=list(members or []), label=raw.get("label", ""),
                           created=raw.get("created", ""), session=raw.get("session"),
                           last_touched=raw.get("last_touched", "")), None
    except (OSError, ValueError, TypeError) as e:
        return _closed(name), f"project.json is unreadable: {type(e).__name__}: {e}"


def load(project_dir: Path, name: str) -> ProjectMeta:
    return load_with_problem(project_dir, name)[0]


def load_with_problem(project_dir: Path, name: str) -> tuple[ProjectMeta, Optional[str]]:
    """load(), plus WHY it failed closed. One code path, so the two cannot drift.

    The reason is for an operator-facing log and must never reach a response body: a caller told
    "this project's metadata is corrupt" has learned the project exists, which is the oracle the
    single denial in app.PROJECT_DENIED exists to remove. But an operator who typos project.json
    would otherwise see nothing but a generic 404 with no way to find out why.
    """
    path = project_dir / "project.json"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        # No metadata file at all: nothing to cache, nobody gets in. Project.__init__ writes one, so
        # by the time a request arrives this means someone deleted it.
        return _closed(name), "project.json is missing"
    key = _key(project_dir, name)
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp:
        # A copy, because callers mutate what they get (project_tools.share appends to `members`
        # before saving) and a mutation that is never saved must not linger as granted access.
        return cached[1].copy(), cached[2]
    meta, problem = _read(path, name)
    # A fail-closed result is cached like any other: a corrupt file must not cost a parse on every
    # request, and the stamp still notices the moment someone fixes it.
    _CACHE[key] = (stamp, meta, problem)
    return meta.copy(), problem


def save(project_dir: Path, meta: ProjectMeta) -> None:
    if meta.visibility not in VISIBILITIES:
        # Failing closed is the right answer for a CORRUPT FILE on read, but a first-party write is
        # a bug in the caller: silently persisting it would produce a project that is unreachable on
        # the next read with nothing pointing at the cause. Refuse before anything is written.
        raise Invalid(f"visibility must be one of {VISIBILITIES} (got {meta.visibility!r})")
    if not isinstance(meta.members, list) or not all(isinstance(m, str) for m in meta.members):
        # The read side refuses to coerce an ill-typed members field because list("ab") is
        # ['a', 'b'] — real single-character usernames. as_json() does exactly that coercion, so the
        # write side has to refuse too: a caller that assigned members="ab" in memory would
        # otherwise persist a file that GRANTS access to the users a and b one read later.
        raise Invalid(f"members must be a list of usernames (got a "
                      f"{type(meta.members).__name__}: {meta.members!r})")
    project_dir.mkdir(parents=True, exist_ok=True)
    if not meta.created:
        meta.created = now_iso()
    meta.last_touched = now_iso()   # db.now_iso, so every timestamp the server writes matches
    path = project_dir / "project.json"
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(meta.as_json(), indent=2))
    os.replace(tmp, path)            # atomic: a reader sees the old file or the new one
    # Drop the entry rather than trusting the new stamp: an edit that keeps the size (a member
    # added and removed again) within one mtime tick would otherwise be invisible to readers.
    _CACHE.pop(_key(project_dir, meta.name), None)


def can_access(who: Optional[Identity], meta: ProjectMeta) -> bool:
    """shared, or owner, or member — and a legacy token reaches only the project that holds it.

    Deliberately blind to `who.is_admin`: an admin who could open anyone's private project would
    make the private tier decorative (spec A16), which is also why the sharing tools are owner-only.
    """
    if who is None:
        return False
    # Before the shared shortcut: a legacy token lives in one project's tokens.json and must not
    # gain every other shared project on the server just because the endpoint became neutral.
    if who.legacy and who.project_scope != meta.name:
        return False
    if meta.visibility == "shared":
        return True
    if meta.owner is None:
        return False                 # private with no owner is reachable by nobody
    return who.user == meta.owner or who.user in meta.members
