"""Runtime configuration, all env-overridable. Safe defaults bind localhost only."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Config:
    def __init__(self) -> None:
        self.data_dir = Path(_env("HIVEMIND_DATA_DIR", str(Path.home() / "hivemind-data")))
        self.host = _env("HIVEMIND_HOST", "127.0.0.1")            # deploy sets 0.0.0.0 (private NICs)
        self.port = int(_env("HIVEMIND_PORT", "8787"))
        self.public_url = _env("HIVEMIND_PUBLIC_URL", f"http://{self.host}:{self.port}")
        self.tokens_path = Path(_env("HIVEMIND_TOKENS", str(self.data_dir / "tokens.json")))
        self.max_blob_bytes = int(_env("HIVEMIND_MAX_BLOB", str(2 * 1024 * 1024 * 1024)))  # 2 GiB
        self.blob_grace_seconds = int(_env("HIVEMIND_BLOB_GRACE", "86400"))                # 24h GC grace
        # DNS-rebinding host allowlist for the MCP transport. '*' disables the check (LAN/mesh).
        hosts = _env("HIVEMIND_ALLOWED_HOSTS", "*")
        self.allowed_hosts = [h.strip() for h in hosts.split(",") if h.strip()]
        self.require_auth = _env("HIVEMIND_REQUIRE_AUTH", "1") not in ("0", "false", "no")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hivemind.db"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.blobs_dir / "tmp").mkdir(parents=True, exist_ok=True)


_cfg: Config | None = None


def config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
