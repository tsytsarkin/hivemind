"""Static bearer-token auth. Same token store gates BOTH the MCP transport (via TokenVerifier)
and the plain-HTTP REST routes (via require_token). Trusted-team tier: per-client tokens with
optional scopes; no OAuth infra. tokens.json = { "<token>": {"client_id": "...", "scopes": [...]} }.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier

DEFAULT_SCOPES = ["hivemind:rw"]


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self._tokens: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        if self.path.exists():
            self._tokens = json.loads(self.path.read_text())
        else:
            self._tokens = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._tokens, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def verify(self, token: str) -> Optional[AccessToken]:
        info = self._tokens.get(token)
        if info is None:
            return None
        return AccessToken(token=token, client_id=info.get("client_id", "unknown"),
                           scopes=info.get("scopes", DEFAULT_SCOPES), expires_at=None)

    def mint(self, client_id: str, scopes: Optional[list[str]] = None) -> str:
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
