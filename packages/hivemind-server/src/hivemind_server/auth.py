"""Static bearer-token auth. Same token store gates BOTH the MCP transport (via TokenVerifier)
and the plain-HTTP REST routes (via require_token). Trusted-team tier: per-client tokens with
optional scopes; no OAuth infra. tokens.json = { "<token>": {"client_id": "...", "scopes": [...]} }.
"""
from __future__ import annotations

import secrets
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .jsonstore import JsonFileStore

DEFAULT_SCOPES = ["hivemind:rw"]


class TokenStore(JsonFileStore):
    """Token store backed by tokens.json. File discipline (re-read on change, atomic write) lives
    in JsonFileStore, shared with identity.IdentityStore — see jsonstore.py for why.
    """

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
