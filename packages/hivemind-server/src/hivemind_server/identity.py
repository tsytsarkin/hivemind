"""Who is calling.

Identity used to be a per-project `client_id` naming a machine. Two things forced it up to the
server: a project-neutral MCP endpoint has to authenticate before it knows which project is meant,
and authorship has to name a person rather than a host. The token is the authority — no tool
argument can override it, which is the whole point (before this, `agent="anything"` was recorded
verbatim).
"""
from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from .db import Invalid
from .jsonstore import JsonFileStore

# No dots: project names are `<user>.<suffix>`, and a dotted username would make the prefix
# ambiguous between user `nik` owning `nik.x` and a user literally named `nik.x`.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_SCOPES = ["hivemind:rw"]
ROLES = ("member", "admin")


def validate_username(name: str) -> str:
    # fullmatch, not match: `$` alone matches just before a trailing newline, so match() would
    # accept "nik\n" as if it were "nik" — a distinct, invisible username riding a truncated cap.
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise Invalid(f"invalid username {name!r}: want {USERNAME_RE.pattern} "
                      f"(lowercase, no dots — dots are reserved for project ownership prefixes)")
    return name


@dataclass
class Identity:
    user: str
    device: str
    role: str = "member"
    token_id: str = ""
    legacy: bool = False
    project_scope: Optional[str] = None      # legacy tokens reach ONLY this project

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and not self.legacy


class IdentityStore(JsonFileStore):
    """identities.json: token -> {user, device, role, scopes}.

    File discipline (re-read on change, atomic write) lives in JsonFileStore, shared with
    auth.TokenStore, so a token minted by `hivemind-admin` in another process works with no
    restart, and removing one revokes it immediately.

    identities.json is meant to be hand-editable by an operator (that IS the revocation path), so
    a single malformed row — e.g. one missing "user" — must not deny service to everyone else: it
    is dropped rather than raised.
    """

    def verify(self, token: str) -> Optional[Identity]:
        self.refresh_if_changed()
        info = self._tokens.get(token)
        if info is None:
            return None
        user = info.get("user")
        if not user:
            return None                # malformed row (operator typo) — refuse just this token
        return Identity(user=user, device=info.get("device", "?"),
                        role=info.get("role", "member"), token_id=token[:12])

    def mint(self, user: str, device: str = "?", role: str = "member") -> str:
        validate_username(user)
        if role not in ROLES:
            raise Invalid(f"unknown role {role!r}: want one of {ROLES}")
        self.refresh_if_changed()
        token = "hm_" + secrets.token_urlsafe(32)
        self._tokens[token] = {"user": user, "device": device[:64], "role": role,
                               "scopes": DEFAULT_SCOPES}
        self.save()
        return token

    def users(self) -> list[str]:
        self.refresh_if_changed()
        return sorted({i["user"] for i in self._tokens.values() if i.get("user")})

    def has_user(self, user: str) -> bool:
        return user in self.users()


def resolve(token: Optional[str], store: IdentityStore, project: Any) -> Optional[Identity]:
    """Server-level identity first; fall back to a project's own legacy token store.

    A legacy token is deliberately pinned to the project whose file holds it. Without that, moving
    to a project-neutral endpoint would silently widen every credential already deployed. Note this
    only ever consults the ONE project passed in — there is no cross-project search — so a token
    that lives in project A's tokens.json is refused (not mis-scoped) when resolved against B.
    """
    if not token:
        return None
    who = store.verify(token)
    if who is not None:
        return who
    access = project.tokens.verify(token) if project is not None else None
    if access is None:
        return None
    return Identity(user=f"legacy:{access.client_id}", device=access.client_id,
                    role="member", token_id=token[:12], legacy=True,
                    project_scope=project.name)


_IDENTITY: ContextVar = ContextVar("hivemind_identity", default=None)


def set_identity(who: Optional[Identity]) -> None:
    _IDENTITY.set(who)


def current_identity() -> Optional[Identity]:
    return _IDENTITY.get()
