"""Static bearer-token auth. Same token store gates BOTH the MCP transport (via TokenVerifier)
and the plain-HTTP REST routes (via require_token). Trusted-team tier: per-client tokens with
optional scopes; no OAuth infra. tokens.json = { "<token>": {"client_id": "...", "scopes": [...]} }.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier

DEFAULT_SCOPES = ["hivemind:rw"]


class TokenStore:
    """Token store backed by tokens.json.

    Tokens are minted by a SEPARATE process (`hivemind-admin mint-token`), so the running server
    must not cache the file forever — it re-reads whenever the file's (mtime, size) changes, which
    makes a freshly minted token usable with no restart. Writes go through a temp file + atomic
    rename so a reader can never observe a half-written file.
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
            self._tokens = {}
            self._stamp = None
            return
        try:
            self._tokens = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return  # keep the last good copy rather than locking everyone out
        self._stamp = stamp

    def refresh_if_changed(self) -> bool:
        """Re-read tokens.json if another process changed it. Returns True if reloaded."""
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
        os.replace(tmp, self.path)          # atomic: readers see old or new, never partial
        self._stamp = self._file_stamp()

    def verify(self, token: str) -> Optional[AccessToken]:
        # Always cheap-stat the file first: picks up tokens minted by another process AND makes
        # revocation (a token removed from the file) take effect, both without a restart.
        self.refresh_if_changed()
        info = self._tokens.get(token)
        if info is None:
            return None
        return AccessToken(token=token, client_id=info.get("client_id", "unknown"),
                           scopes=info.get("scopes", DEFAULT_SCOPES), expires_at=None)

    def mint(self, client_id: str, scopes: Optional[list[str]] = None) -> str:
        self.refresh_if_changed()   # don't clobber tokens another process added since we loaded
        token = "hm_" + secrets.token_urlsafe(32)
        self._tokens[token] = {"client_id": client_id, "scopes": scopes or DEFAULT_SCOPES}
        self.save()
        return token

    def ensure_first_token(self, client_id: str = "bootstrap") -> Optional[str]:
        """Create an initial token if the store is empty. Returns the new token, else None."""
        if self._tokens:
            return None
        return self.mint(client_id)


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, store: TokenStore):
        self.store = store

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        return self.store.verify(token)


def bearer_from_headers(headers) -> Optional[str]:
    auth = headers.get("authorization") or headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
