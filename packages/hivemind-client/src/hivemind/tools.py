"""Client-side tool registry: publish a self-contained tool (PEP 723 single-file script or any
file + manifest), and fetch+verify a published tool with a generated RUN.md. Distribution-only:
we fetch and verify; the agent runs it (bootstrap uv first on a cold machine)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

# canonical PEP 723 block regex (packaging.python.org)
_PEP723 = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
_DEP_LINE = re.compile(r'"([^"]+)"')


def read_pep723(path: str) -> Dict:
    """Extract requires-python + dependencies from a PEP 723 `# /// script` block, if present."""
    text = Path(path).read_text(errors="ignore")
    for m in _PEP723.finditer(text):
        if m.group("type") != "script":
            continue
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in m.group("content").splitlines(keepends=True))
        req_py = None
        rp = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
        if rp:
            req_py = rp.group(1)
        deps: List[str] = []
        dm = re.search(r"dependencies\s*=\s*\[(.*?)\]", content, re.S)
        if dm:
            deps = _DEP_LINE.findall(dm.group(1))
        return {"requires_python": req_py, "dependencies": deps}
    return {}


def build_manifest(path: str, *, id: str, version: str, runtime: str = "python",
                   description: str = "", runtime_hint: Optional[str] = "uv",
                   examples: Optional[List[Dict]] = None, tags: Optional[List[str]] = None,
                   license: str = "", entrypoint: Optional[str] = None) -> Dict:
    entry = entrypoint or os.path.basename(path)
    manifest: Dict = {"id": id, "version": version, "runtime": runtime, "entrypoint": entry,
                      "runtime_hint": runtime_hint, "description": description,
                      "examples": examples or [], "tags": tags or [], "license": license}
    if runtime == "python":
        meta = read_pep723(path)
        if meta.get("requires_python"):
            manifest.setdefault("requires", {})["python"] = meta["requires_python"]
        if meta.get("dependencies"):
            manifest["dependencies"] = meta["dependencies"]
    return manifest


def publish(client, path: str, *, id: str, version: str, **manifest_kw) -> Dict:
    """Upload the tool bytes, then register an immutable manifest version."""
    up = client.artifacts.put(path, media_type="text/x-python")
    manifest = build_manifest(path, id=id, version=version, **manifest_kw)
    return client.call("tool_publish", {"manifest": manifest, "artifact_digest": up["digest"]})


def resolve(client, id: str, constraint: str = "", **kw) -> Dict:
    return client.call("tool_resolve", {"id": id, "constraint": constraint, **kw})


def search(client, query: str = "", **kw) -> Dict:
    return client.call("tool_search", {"query": query, **kw})


def get(client, id: str, *, constraint: str = "", dest_dir: str = ".",
        os_: Optional[str] = None, arch: Optional[str] = None) -> Dict:
    """Resolve, download+verify the artifact, and write a RUN.md + a pin lockfile entry."""
    res = client.call("tool_resolve", {"id": id, "constraint": constraint,
                                        "os": os_, "arch": arch})
    out = Path(dest_dir) / id.replace("/", "__")
    out.mkdir(parents=True, exist_ok=True)
    entry = res["entrypoint"]
    artifact_path = out / entry
    client.artifacts.get(res["artifact_digest"], str(artifact_path))
    _write_runmd(out, res)
    _append_lock(out, res)
    return {"id": id, "version": res["version"], "dir": str(out),
            "entrypoint": str(artifact_path), "run": res["run"],
            "artifact_digest": res["artifact_digest"]}


def _write_runmd(out: Path, res: Dict) -> None:
    m = res["manifest"]
    lines = [f"# {res['id']}@{res['version']}", ""]
    if m.get("description"):
        lines += [m["description"], ""]
    lines += ["## Run", "", "```sh", res["run"], "```", ""]
    if m.get("requires", {}).get("python"):
        lines += [f"Requires Python `{m['requires']['python']}` (uv fetches it automatically).", ""]
    if m.get("dependencies"):
        lines += ["Dependencies (resolved by uv from the PEP 723 block): "
                  + ", ".join(f"`{d}`" for d in m["dependencies"]), ""]
    if m.get("examples"):
        lines += ["## Examples", ""]
        for ex in m["examples"]:
            lines += [f"- `{ex.get('cmd','')}` — {ex.get('expect','')}"]
        lines += [""]
    if res.get("newer_incompatible"):
        n = res["newer_incompatible"]
        lines += [f"> Note: a newer version {n['version']} exists but {n['reason']}.", ""]
    lines += [f"Integrity: `{res['artifact_digest']}` (verified on download).", ""]
    (out / "RUN.md").write_text("\n".join(lines))


def _append_lock(out: Path, res: Dict) -> None:
    lock = out.parent / "hivemind-tools.lock"
    entry = f"{res['id']}@{res['version']} {res['artifact_digest']}\n"
    existing = lock.read_text() if lock.exists() else ""
    if entry not in existing:
        lock.write_text(existing + entry)
