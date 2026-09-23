"""Shared discipline for a JSON-dict file that a SEPARATE process can edit while the server runs.

Both auth.TokenStore and identity.IdentityStore are `token -> {...}` files minted by
`hivemind-admin` out of process and revoked by hand-editing the file — so this is the one place
that owns: re-reading only when the file actually changed (mtime_ns, size), never caching a stale
copy forever; writing atomically so a reader can never observe a half-written file; and refusing a
malformed file (bad JSON, or valid JSON that isn't an object) by keeping the last good copy rather
than locking every caller out. Splitting this out means a fix to any of that lands once for both
credential stores instead of twice, with the risk of the two drifting apart silently.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


class JsonFileStore:
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
            parsed = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return                     # keep the last good copy rather than locking everyone out
        if not isinstance(parsed, dict):
            return                     # e.g. a JSON array/string — malformed the same way a
                                        # decode error is; same "keep the last good copy" rule
        self._tokens = parsed
        self._stamp = stamp

    def refresh_if_changed(self) -> bool:
        """Re-read the file if another process changed it. Returns True if reloaded."""
        if self._file_stamp() != self._stamp:
            self.reload()
            return True
        return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        tmp.unlink(missing_ok=True)    # clear a leftover from a crashed run that reused this pid
        # Create the temp file AT mode 0600 (not chmod'd after the fact): a chmod-after-write
        # leaves the secrets file briefly world-readable under a permissive umask (e.g. 0644).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self._tokens, indent=2))
        os.replace(tmp, self.path)     # atomic: readers see old or new, never partial
        self._stamp = self._file_stamp()
