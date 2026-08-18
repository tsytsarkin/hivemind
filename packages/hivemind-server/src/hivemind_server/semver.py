"""Tiny semver: parse, compare, and match a constraint. Zero deps. Enough for a tool registry:
exact, ^ (caret), ~ (tilde), >= / > / <= / <, and '*'/'' (any). Prereleases sort before release.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_RANGE_HINT = re.compile(r"[\^~<>*]|\s-\s|\|\|| x$|\.x")


def is_range(s: str) -> bool:
    return bool(_RANGE_HINT.search(s.strip()))


def parse(v: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    m = _RE.match(v.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def _key(v: str):
    p = parse(v)
    if p is None:
        return (0, 0, 0, 1, ())          # unparseable sorts low
    major, minor, patch, pre = p
    # a release (no prerelease) sorts AFTER its prereleases -> pre_flag 1 for release
    return (major, minor, patch, 0 if pre else 1, tuple(pre.split(".")) if pre else ())


def compare(a: str, b: str) -> int:
    ka, kb = _key(a), _key(b)
    return (ka > kb) - (ka < kb)


def latest(versions: List[str], *, include_prerelease: bool = False) -> Optional[str]:
    cands = [v for v in versions if parse(v) is not None]
    if not include_prerelease:
        stable = [v for v in cands if parse(v)[3] is None]
        cands = stable or cands
    return max(cands, key=_key) if cands else None


def _norm(v: str) -> str:
    """Pad a partial version (1, 1.2) to full x.y.z so parse() accepts it. Leaves prereleases."""
    v = v.strip()
    if parse(v) is not None:
        return v
    if "-" in v or "+" in v:
        return v
    parts = v.split(".")
    if not all(p.isdigit() for p in parts):
        return v
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def satisfies(version: str, constraint: str) -> bool:
    c = (constraint or "").strip()
    if c in ("", "*", "latest"):
        return True
    p = parse(version)
    if p is None:
        return False
    maj, mn, pt, _ = p
    if c.startswith("^"):
        base = _norm(c[1:])
        b = parse(base)
        if not b:
            return False
        if maj != b[0]:
            return False
        return _key(version) >= _key(base)
    if c.startswith("~"):
        base = _norm(c[1:])
        b = parse(base)
        if not b:
            return False
        if maj != b[0] or mn != b[1]:
            return False
        return _key(version) >= _key(base)
    for op in (">=", "<=", ">", "<", "=="):
        if c.startswith(op):
            other = _norm(c[len(op):].strip())
            if parse(other) is None:
                return False
            cmp = compare(version, other)
            return {">=": cmp >= 0, "<=": cmp <= 0, ">": cmp > 0,
                    "<": cmp < 0, "==": cmp == 0}[op]
    return compare(version, _norm(c)) == 0     # bare version = exact
