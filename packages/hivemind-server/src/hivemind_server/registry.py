"""Tool registry: publish/resolve/yank standalone tools. Versions are IMMUTABLE once published;
range strings are rejected as versions; yank (never delete) hides a version from resolution unless
it is the only exact match. Tool bytes live in the blob store (artifact_digest)."""
from __future__ import annotations

import json
import re
from typing import Optional

from . import semver
from .db import Conflict, Database, Invalid, NotFound

_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)?$")
_REQUIRED = ("id", "version", "runtime", "entrypoint")
_RUNTIMES = {"python", "node", "binary", "shell", "container"}


def _validate_manifest(manifest: dict) -> None:
    for k in _REQUIRED:
        if not manifest.get(k):
            raise Invalid(f"manifest missing required field {k!r}")
    if not _ID_RE.match(manifest["id"]):
        raise Invalid(f"invalid tool id {manifest['id']!r} (want reverse-dns-ish [a-z0-9._-/])")
    if manifest["runtime"] not in _RUNTIMES:
        raise Invalid(f"runtime must be one of {sorted(_RUNTIMES)}")
    ver = str(manifest["version"])
    if semver.is_range(ver) or semver.parse(ver) is None:
        raise Invalid(f"version {ver!r} must be an exact semver (no ranges)")


def _run_command(manifest: dict) -> str:
    rt, hint, entry = manifest["runtime"], manifest.get("runtime_hint"), manifest["entrypoint"]
    if rt == "python":
        return f"uv run --script {entry}" if hint in (None, "uv") else f"python {entry}"
    if rt == "node":
        return f"npx {entry}" if hint == "npx" else f"node {entry}"
    if rt == "shell":
        return f"sh {entry}"
    return f"./{entry}"


def publish(db: Database, agent_id: str, manifest: dict, artifact_digest: str) -> dict:
    _validate_manifest(manifest)
    tid, version = manifest["id"], str(manifest["version"])
    with db.write(agent_id, f"tool_publish {tid}@{version}") as tx:
        cur = tx.cur
        b = cur.execute("SELECT 1 FROM blob WHERE digest=?", (artifact_digest,)).fetchone()
        if b is None:
            raise Invalid(f"artifact {artifact_digest} not uploaded; PUT the tool blob first")
        exists = cur.execute("SELECT 1 FROM tool_version WHERE id=? AND version=?",
                             (tid, version)).fetchone()
        if exists:
            raise Conflict(f"{tid}@{version} already published (immutable). Bump the version.")
        cur.execute("INSERT INTO tool(id,latest_version,created_tx) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO NOTHING", (tid, version, tx.tx_id))
        cur.execute("INSERT INTO tool_version(id,version,manifest,artifact_digest,created_tx) "
                    "VALUES(?,?,?,?,?)",
                    (tid, version, json.dumps(manifest), artifact_digest, tx.tx_id))
        # recompute latest across non-yanked stable versions
        rows = cur.execute("SELECT version FROM tool_version WHERE id=? AND yanked=0",
                           (tid,)).fetchall()
        latest = semver.latest([r["version"] for r in rows]) or version
        cur.execute("UPDATE tool SET latest_version=? WHERE id=?", (latest, tid))
    return {"id": tid, "version": version, "latest": latest, "artifact_digest": artifact_digest}


def yank(db: Database, agent_id: str, tid: str, version: str, reason: str = "") -> dict:
    with db.write(agent_id, f"tool_yank {tid}@{version}") as tx:
        cur = tx.cur
        n = cur.execute("UPDATE tool_version SET yanked=1, yanked_reason=? WHERE id=? AND version=?",
                        (reason, tid, version)).rowcount
        if n == 0:
            raise NotFound(f"{tid}@{version} not found")
        rows = cur.execute("SELECT version FROM tool_version WHERE id=? AND yanked=0",
                           (tid,)).fetchall()
        latest = semver.latest([r["version"] for r in rows])
        cur.execute("UPDATE tool SET latest_version=? WHERE id=?", (latest, tid))
    return {"id": tid, "version": version, "yanked": True, "reason": reason}


def resolve(db: Database, tid: str, *, constraint: str = "", os: Optional[str] = None,
            arch: Optional[str] = None, include_prerelease: bool = False) -> dict:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT version, manifest, artifact_digest, yanked, yanked_reason "
            "FROM tool_version WHERE id=?", (tid,)).fetchall()
    if not rows:
        raise NotFound(f"tool {tid!r} not found")
    all_versions = [r["version"] for r in rows]
    live = [r for r in rows if not r["yanked"]]
    matching = [r for r in live if semver.satisfies(r["version"], constraint)]
    # yanked-but-exact-pin still resolvable (PEP 592 semantics)
    if not matching and constraint and not semver.is_range(constraint):
        matching = [r for r in rows if r["version"] == constraint.strip()]
    if not matching:
        raise NotFound(f"no version of {tid} satisfies {constraint!r}")
    best = max(matching, key=lambda r: semver._key(r["version"])) if include_prerelease else \
        _pick_stable(matching)
    manifest = json.loads(best["manifest"])
    newer = _newer_incompatible(all_versions, best["version"], constraint)
    href = f"/blobs/{best['artifact_digest'].replace(':', '/', 1)}"
    return {"id": tid, "version": best["version"], "manifest": manifest,
            "artifact_digest": best["artifact_digest"], "artifact_url": href,
            "run": _run_command(manifest), "entrypoint": manifest["entrypoint"],
            "yanked": bool(best["yanked"]), "yanked_reason": best["yanked_reason"],
            "newer_incompatible": newer}


def _pick_stable(rows):
    stable = [r for r in rows if semver.parse(r["version"])[3] is None]
    pool = stable or rows
    return max(pool, key=lambda r: semver._key(r["version"]))


def _newer_incompatible(all_versions, chosen: str, constraint: str):
    newer = [v for v in all_versions if semver.compare(v, chosen) > 0
             and not semver.satisfies(v, constraint)]
    if not newer:
        return None
    top = semver.latest(newer, include_prerelease=True)
    return {"version": top, "reason": f"exists but does not satisfy {constraint!r}"}


def search(db: Database, query: str = "", *, os: Optional[str] = None,
           arch: Optional[str] = None, limit: int = 25) -> dict:
    with db.read() as cur:
        rows = cur.execute(
            "SELECT t.id, t.latest_version, tv.manifest FROM tool t "
            "JOIN tool_version tv ON tv.id=t.id AND tv.version=t.latest_version").fetchall()
    out = []
    q = query.lower()
    for r in rows:
        m = json.loads(r["manifest"])
        hay = " ".join([r["id"], m.get("description", ""), " ".join(m.get("tags", []) or [])]).lower()
        if q and q not in hay:
            continue
        platforms = [f"{a.get('os')}/{a.get('arch')}" for a in (m.get("artifacts") or [])]
        out.append({"id": r["id"], "latest": r["latest_version"],
                    "description": m.get("description", ""),
                    "runtime": m.get("runtime"), "platforms": platforms or ["any"]})
        if len(out) >= limit:
            break
    return {"tools": out, "count": len(out)}


# ── MCP tool attachment (called from registry_tools.attach) ────────────────────────
def attach_tools(mcp, project, envelope, RO, WRITE, base) -> None:
    db = project.db

    @mcp.tool(annotations=WRITE,
              description="Publish an immutable tool version. `manifest` must include id "
                          "(reverse-dns), version (exact semver — no ranges), runtime, entrypoint. "
                          "Upload the tool blob first (PUT /blobs/...) and pass its artifact_digest.")
    @envelope
    def tool_publish(manifest: dict, artifact_digest: str, agent: str = "agent") -> dict:
        return publish(db, agent, manifest, artifact_digest)

    @mcp.tool(annotations=RO,
              description="Resolve a tool to a runnable version. Returns the exact version, a "
                          "ready-to-run command, the artifact URL, and a newer_incompatible hint. "
                          "constraint accepts ^, ~, >=, or an exact version.")
    @envelope
    def tool_resolve(id: str, constraint: str = "", os: Optional[str] = None,
                     arch: Optional[str] = None, include_prerelease: bool = False) -> dict:
        return resolve(db, id, constraint=constraint, os=os, arch=arch,
                       include_prerelease=include_prerelease)

    @mcp.tool(annotations=RO,
              description="List/search published tools (id, latest version, description, "
                          "platforms). Use this to discover what tools other agents have shared.")
    @envelope
    def tool_search(query: str = "", os: Optional[str] = None, arch: Optional[str] = None,
                    limit: int = 25) -> dict:
        return search(db, query, os=os, arch=arch, limit=limit)

    @mcp.tool(annotations=WRITE,
              description="Yank a tool version (hide from resolution; still fetchable by exact pin). "
                          "Immutable registries never delete — this just stops new adopters.")
    @envelope
    def tool_yank(id: str, version: str, reason: str = "", agent: str = "agent") -> dict:
        return yank(db, agent, id, version, reason)
