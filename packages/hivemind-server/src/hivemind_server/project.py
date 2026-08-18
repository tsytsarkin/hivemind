"""A *project* is a self-contained data deployment: its own SQLite DB, blob store, token store,
schema, and guide — all under one directory, fully separated from the code. One server process
can host many projects (mounted at /p/<name>/...), each isolated; or you can run one server per
project for physical isolation. Either way the data lives outside the repo.

Layout:  <projects_root>/<name>/{hivemind.db, blobs/, tokens.json}
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .auth import TokenStore
from .db import Database

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


class Project:
    """Bundles the per-project data handles. Blob store is attached lazily (see blobs.py)."""

    def __init__(self, name: str, root: Path, *, max_blob_bytes: int, blob_grace_seconds: int):
        if not valid_name(name):
            raise ValueError(f"invalid project name {name!r} (want [a-z0-9._-], <=64 chars)")
        self.name = name
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "blobs" / "tmp").mkdir(parents=True, exist_ok=True)
        self.db = Database(self.dir / "hivemind.db")
        self.tokens = TokenStore(self.dir / "tokens.json")
        self._max_blob_bytes = max_blob_bytes
        self._blob_grace_seconds = blob_grace_seconds
        self._blobs = None

    @property
    def blobs(self):
        if self._blobs is None:
            from .blobs import BlobStore  # deferred: blobs.py may not enforce at import time
            self._blobs = BlobStore(self.dir / "blobs", self.db,
                                    max_bytes=self._max_blob_bytes,
                                    grace_seconds=self._blob_grace_seconds)
        return self._blobs


class ProjectRegistry:
    """Discovers/creates projects under a root directory. Discovery happens at startup; new
    projects are created by making a directory (or via `create`) and (re)starting the server."""

    def __init__(self, root: Path, *, max_blob_bytes: int, blob_grace_seconds: int,
                 default_name: str = "default"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._max_blob_bytes = max_blob_bytes
        self._blob_grace_seconds = blob_grace_seconds
        self.default_name = default_name
        self._projects: dict[str, Project] = {}

    def _make(self, name: str) -> Project:
        return Project(name, self.root / name, max_blob_bytes=self._max_blob_bytes,
                       blob_grace_seconds=self._blob_grace_seconds)

    def discover(self) -> list[str]:
        """Load every project subdir; guarantee at least the default project exists."""
        names = sorted(p.name for p in self.root.iterdir()
                       if p.is_dir() and valid_name(p.name)) if self.root.exists() else []
        if not names:
            names = [self.default_name]
        for n in names:
            if n not in self._projects:
                self._projects[n] = self._make(n)
        return list(self._projects)

    def get(self, name: str) -> Optional[Project]:
        return self._projects.get(name)

    def create(self, name: str) -> Project:
        if name in self._projects:
            return self._projects[name]
        p = self._make(name)
        self._projects[name] = p
        return p

    def all(self) -> list[Project]:
        return list(self._projects.values())


def projects_root_from_env(data_dir: Path) -> Path:
    override = os.environ.get("HIVEMIND_PROJECTS_DIR")
    return Path(override) if override else (data_dir / "projects")
