"""Who is calling.

Identity used to be a per-project `client_id` naming a machine. Two things forced it up to the
server: a project-neutral MCP endpoint has to authenticate before it knows which project is meant,
and authorship has to name a person rather than a host. The token is the authority — no tool
argument can override it, which is the whole point (before this, `agent="anything"` was recorded
verbatim).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .db import Invalid

# No dots: project names are `<user>.<suffix>`, and a dotted username would make the prefix
# ambiguous between user `nik` owning `nik.x` and a user literally named `nik.x`.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_SCOPES = ["hivemind:rw"]
ROLES = ("member", "admin")


def validate_username(name: str) -> str:
    if not isinstance(name, str) or not USERNAME_RE.match(name):
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


class IdentityStore:
    """identities.json: token -> {user, device, role, scopes}.

    Same file discipline as auth.TokenStore — stamp, re-read on change, atomic write — so a token
    minted by `hivemind-admin` in another process works with no restart, and removing one revokes
    it immediately.
    """

    def __init__(self, path: Path):
        self.path = path
        self._tokens: dict[str, dict] = {}
        self._stamp: Optional[tuple] = None
        self.reload()

    def _file_stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload(self) -> None:
        stamp = self._file_stamp()
        if stamp is None:
            self._tokens, self._stamp = {}, None
            return
        try:
            self._tokens = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return                     # keep the last good copy rather than locking everyone out
        self._stamp = stamp

    def refresh_if_changed(self) -> bool:
        if self._file_stamp() != self._stamp:
            self.reload()
            return True
        return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(self._tokens, indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._stamp = self._file_stamp()

    def verify(self, token: str) -> Optional[Identity]:
        self.refresh_if_changed()
        info = self._tokens.get(token)
        if info is None:
            return None
        return Identity(user=info["user"], device=info.get("device", "?"),
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
        return sorted({i["user"] for i in self._tokens.values()})

    def has_user(self, user: str) -> bool:
        return user in self.users()


def resolve(token: Optional[str], store: IdentityStore, project: Any) -> Optional[Identity]:
    """Server-level identity first; fall back to a project's own legacy token store.

    A legacy token is deliberately pinned to the project whose file holds it. Without that, moving
    to a project-neutral endpoint would silently widen every credential already deployed.
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
