"""Schema-as-data: node/edge type resolution + props validation.

Types live as rows in node_type/edge_type (see schema.sql). The engine validates every write
against the current usable version of the relevant type. Additive-only proposal/promotion and
near-duplicate detection are layered on in this module (see propose_type / promote_type).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .db import Invalid, Tx

_TABLE = {"node": "node_type", "edge": "edge_type"}


def _table(kind: str) -> str:
    try:
        return _TABLE[kind]
    except KeyError:
        raise Invalid(f"unknown type kind {kind!r} (want 'node' or 'edge')")


def usable_type(cur, kind: str, name: str) -> Optional[dict]:
    """Highest-version type row that is usable to validate a write: prefer active, else proposed."""
    tbl = _table(kind)
    row = cur.execute(
        f"SELECT * FROM {tbl} WHERE name=? AND status IN ('active','proposed') "
        f"ORDER BY (status='active') DESC, version DESC LIMIT 1",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def validate_props(cur, kind: str, name: str, props: dict) -> int:
    """Validate props against the type's JSON Schema. Returns the schema_ver used. Raises Invalid."""
    t = usable_type(cur, kind, name)
    if t is None:
        raise Invalid(
            f"unknown {kind} type {name!r} — define it first via schema_propose/apply"
        )
    try:
        schema = json.loads(t["json_schema"])
    except json.JSONDecodeError as e:  # pragma: no cover - guarded by CHECK(json_valid)
        raise Invalid(f"type {name!r} has a corrupt schema: {e}")
    err = best_match(Draft202012Validator(schema).iter_errors(props))
    if err is not None:
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        raise Invalid(f"{kind} {name!r} props invalid at {loc}: {err.message}")
    return int(t["version"])


def edge_traits(cur, name: str) -> dict:
    """Resolve an edge type's generic behavioral traits (+ schema_ver). Raises Invalid if unknown."""
    t = usable_type(cur, "edge", name)
    if t is None:
        raise Invalid(f"unknown edge type {name!r} — define it first via schema_propose/apply")
    return {
        "schema_ver": int(t["version"]),
        "src_types": json.loads(t["src_types"]),
        "dst_types": json.loads(t["dst_types"]),
        "cardinality": t["cardinality"],
        "directed": bool(t["directed"]),
        "symmetric": bool(t["symmetric"]),
        "transitive": bool(t["transitive"]),
        "acyclic": bool(t["acyclic"]),
        "versioned": bool(t["versioned"]),
        "assertive": bool(t["assertive"]),
    }


# ── writing types (used by schema_apply + tests; additive-only guard in propose_type) ──────

_EDGE_TRAITS = ("src_types", "dst_types", "cardinality", "directed", "symmetric",
                "transitive", "acyclic", "versioned", "assertive")


def _bump_schema_version(cur, tx: Tx) -> None:
    cur.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
    )


def define_type(cur, tx: Tx, kind: str, name: str, json_schema: dict,
                *, status: str = "active", traits: Optional[dict] = None) -> int:
    """Insert a new *version* of a type (additive: never mutates an existing row). Returns version."""
    tbl = _table(kind)
    # sanity: the schema must itself be a valid JSON Schema
    try:
        Draft202012Validator.check_schema(json_schema)
    except Exception as e:
        raise Invalid(f"invalid JSON Schema for {kind} {name!r}: {e}")
    row = cur.execute(f"SELECT MAX(version) AS v FROM {tbl} WHERE name=?", (name,)).fetchone()
    version = (row["v"] or 0) + 1
    if kind == "node":
        cur.execute(
            "INSERT INTO node_type(name,version,json_schema,status,parent,created_tx) "
            "VALUES(?,?,?,?,?,?)",
            (name, version, json.dumps(json_schema), status,
             (traits or {}).get("parent"), tx.tx_id),
        )
    else:
        tr = {k: (traits or {}).get(k) for k in _EDGE_TRAITS}
        cur.execute(
            "INSERT INTO edge_type(name,version,json_schema,src_types,dst_types,cardinality,"
            "directed,symmetric,transitive,acyclic,versioned,assertive,status,created_tx) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, version, json.dumps(json_schema),
             json.dumps(tr["src_types"] if tr["src_types"] is not None else ["*"]),
             json.dumps(tr["dst_types"] if tr["dst_types"] is not None else ["*"]),
             tr["cardinality"] or "N:N",
             1 if tr["directed"] in (None, True, 1) else 0,
             1 if tr["symmetric"] in (True, 1) else 0,
             1 if tr["transitive"] in (True, 1) else 0,
             1 if tr["acyclic"] in (True, 1) else 0,
             1 if tr["versioned"] in (None, True, 1) else 0,
             1 if tr["assertive"] in (True, 1) else 0,
             status, tx.tx_id),
        )
    _bump_schema_version(cur, tx)
    return version


# ── additive-only guard + near-dup detection + proposal workflow ───────────────────
import difflib

from .db import Database

_DEPRECATED = "deprecated"


def _all_type_names(cur, kind: str) -> list[str]:
    tbl = _table(kind)
    return [r["name"] for r in cur.execute(f"SELECT DISTINCT name FROM {tbl}").fetchall()]


def _near_duplicates(cur, kind: str, name: str, threshold: float = 0.82) -> list[str]:
    """Return existing type names confusingly similar to `name` (type-sprawl guard)."""
    out = []
    for existing in _all_type_names(cur, kind):
        if existing == name:
            continue
        ratio = difflib.SequenceMatcher(None, name.lower(), existing.lower()).ratio()
        if ratio >= threshold:
            out.append(existing)
    return out


def _additive_ok(old: dict, new: dict) -> Optional[str]:
    """Best-effort backward-compat check. Returns a reason string if NON-additive, else None.

    Allowed (additive): add properties, drop a field from `required`, widen an enum.
    Rejected (destructive, human-only): add a new required field, tighten additionalProperties,
    shrink an enum, remove a previously-defined property.
    """
    old_req = set(old.get("required", []))
    new_req = set(new.get("required", []))
    added_required = new_req - old_req
    if added_required:
        return f"adds required field(s) {sorted(added_required)} (breaks existing data)"
    old_props = old.get("properties", {}) or {}
    new_props = new.get("properties", {}) or {}
    removed = set(old_props) - set(new_props)
    if removed:
        return f"removes previously-defined propert(y/ies) {sorted(removed)}"
    if old.get("additionalProperties", True) not in (False,) and \
            new.get("additionalProperties", True) is False:
        return "tightens additionalProperties from allowed to false"
    for key, oldp in old_props.items():
        newp = new_props.get(key, {})
        if isinstance(oldp, dict) and "enum" in oldp and isinstance(newp, dict) and "enum" in newp:
            dropped = set(oldp["enum"]) - set(newp["enum"])
            if dropped:
                return f"property {key!r} enum drops value(s) {sorted(map(str, dropped))}"
    return None


def propose_type(db: Database, agent_id: str, kind: str, name: str, json_schema: dict, *,
                 traits: Optional[dict] = None, why: str = "", force: bool = False) -> dict:
    """Agent-facing: propose an ADDITIVE type change. Creates a `proposed` version for review."""
    with db.write(agent_id, f"schema_propose {kind}:{name}") as tx:
        cur = tx.cur
        warnings = []
        existing = usable_type(cur, kind, name)
        if existing is None:
            dups = _near_duplicates(cur, kind, name)
            if dups and not force:
                raise Invalid(
                    f"proposed {kind} type {name!r} looks like existing type(s) {dups}. "
                    f"Reuse one, or re-propose with force=true and explain why it differs in `why`."
                )
            if dups:
                warnings.append(f"near-duplicate of {dups} (forced)")
        else:
            reason = _additive_ok(json.loads(existing["json_schema"]), json_schema)
            if reason is not None:
                raise Invalid(
                    f"non-additive schema change to {name!r}: {reason}. "
                    f"Destructive changes are human-only (apply a pack with an operator)."
                )
        version = define_type(cur, tx, kind, name, json_schema, status="proposed",
                              traits=traits)
        return {"kind": kind, "name": name, "version": version, "status": "proposed",
                "why": why, "warnings": warnings}


def promote_type(db: Database, agent_id: str, kind: str, name: str,
                 version: Optional[int] = None) -> dict:
    """Human/reviewer: promote a proposed type version to active (older active → deprecated)."""
    tbl = _table(kind)
    with db.write(agent_id, f"schema_promote {kind}:{name}") as tx:
        cur = tx.cur
        if version is None:
            row = cur.execute(
                f"SELECT MAX(version) v FROM {tbl} WHERE name=? AND status='proposed'", (name,)
            ).fetchone()
            if not row or row["v"] is None:
                raise Invalid(f"no proposed version of {kind} {name!r} to promote")
            version = row["v"]
        cur.execute(f"UPDATE {tbl} SET status='{_DEPRECATED}' WHERE name=? AND status='active'",
                    (name,))
        n = cur.execute(f"UPDATE {tbl} SET status='active' WHERE name=? AND version=?",
                        (name, version)).rowcount
        if n == 0:
            raise Invalid(f"{kind} {name!r} v{version} not found")
        return {"kind": kind, "name": name, "version": version, "status": "active"}


def _type_unchanged(cur, kind: str, name: str, json_schema: dict,
                    traits: Optional[dict]) -> bool:
    """True if the current usable type already matches (schema + edge traits) — makes re-apply
    idempotent instead of inflating versions."""
    cur_t = usable_type(cur, kind, name)
    if cur_t is None:
        return False
    if json.loads(cur_t["json_schema"]) != json_schema:
        return False
    if kind == "edge" and traits is not None:
        want = {
            "src_types": traits.get("src_types") if traits.get("src_types") is not None else ["*"],
            "dst_types": traits.get("dst_types") if traits.get("dst_types") is not None else ["*"],
            "cardinality": traits.get("cardinality") or "N:N",
            "directed": 1 if traits.get("directed") in (None, True, 1) else 0,
            "symmetric": 1 if traits.get("symmetric") in (True, 1) else 0,
            "transitive": 1 if traits.get("transitive") in (True, 1) else 0,
            "acyclic": 1 if traits.get("acyclic") in (True, 1) else 0,
            "versioned": 1 if traits.get("versioned") in (None, True, 1) else 0,
            "assertive": 1 if traits.get("assertive") in (True, 1) else 0,
        }
        if json.loads(cur_t["src_types"]) != want["src_types"]: return False
        if json.loads(cur_t["dst_types"]) != want["dst_types"]: return False
        for k in ("cardinality", "directed", "symmetric", "transitive", "acyclic",
                  "versioned", "assertive"):
            if cur_t[k] != want[k]:
                return False
    return True


def apply_pack(db: Database, agent_id: str, pack: dict, *, force: bool = False) -> dict:
    """Operator: load a domain pack's schema. Defines node/edge types as ACTIVE directly.

    A pack is COPIED into this project (type rows in the DB) — there is no live link back to
    the file, so later edits to the pack only matter when you re-apply it.

    force=False (default) refuses a NON-ADDITIVE change to an existing type (new required
    field, removed property, narrowed enum) because that would invalidate data already in the
    graph. Pass force=True only when you accept that.

    pack = {"node_types": {name: {"schema": {...}, "parent": ...}},
            "edge_types": {name: {"schema": {...}, ...traits}}}
    """
    created = {"node": [], "edge": []}
    with db.write(agent_id, f"apply_pack {pack.get('name','?')}") as tx:
        cur = tx.cur
        unchanged = {"node": [], "edge": []}
        for name, spec in (pack.get("node_types") or {}).items():
            spec = spec or {}
            schema = spec.get("schema", {"type": "object"})
            if _type_unchanged(cur, "node", name, schema, None):
                unchanged["node"].append(name); continue
            existing = usable_type(cur, "node", name)
            if existing is not None and not force:
                reason = _additive_ok(json.loads(existing["json_schema"]), schema)
                if reason:
                    raise Invalid(f"pack change to node {name!r} non-additive: {reason}")
            v = define_type(cur, tx, "node", name, schema, status="active",
                            traits={"parent": spec.get("parent")})
            created["node"].append(f"{name}@{v}")
        for name, spec in (pack.get("edge_types") or {}).items():
            spec = spec or {}
            traits = {k: spec.get(k) for k in _EDGE_TRAITS}
            schema = spec.get("schema", {"type": "object"})
            if _type_unchanged(cur, "edge", name, schema, traits):
                unchanged["edge"].append(name); continue
            existing_e = usable_type(cur, "edge", name)
            if existing_e is not None and not force:
                reason = _additive_ok(json.loads(existing_e["json_schema"]), schema)
                if reason:
                    raise Invalid(f"pack change to edge {name!r} non-additive: {reason}")
            v = define_type(cur, tx, "edge", name, schema, status="active", traits=traits)
            created["edge"].append(f"{name}@{v}")
        return {"pack": pack.get("name"), "created": created, "unchanged": unchanged}


def get_schema(db: Database, *, kind: Optional[str] = None,
               name: Optional[str] = None) -> dict:
    """Dump the usable node/edge types (+ traits + status) and the schema_version counter."""
    out = {"schema_version": int(db.meta_get("schema_version", "0")),
           "cursor": _schema_cursor(db), "node_types": [], "edge_types": []}
    with db.read() as cur:
        kinds = [kind] if kind else ["node", "edge"]
        for k in kinds:
            tbl = _table(k)
            names = [name] if name else _all_type_names(cur, k)
            for nm in names:
                t = usable_type(cur, k, nm)
                if t is None:
                    continue
                entry = {"name": nm, "version": t["version"], "status": t["status"],
                         "schema": json.loads(t["json_schema"])}
                if k == "edge":
                    entry.update({kk: t[kk] for kk in
                                  ("directed", "symmetric", "transitive", "acyclic",
                                   "versioned", "assertive", "cardinality")})
                    entry["src_types"] = json.loads(t["src_types"])
                    entry["dst_types"] = json.loads(t["dst_types"])
                out[f"{k}_types"].append(entry)
    return out


# ── change feed: "what changed since I last looked?" ────────────────────────────────
def _schema_cursor(db: Database) -> int:
    """Monotonic cursor over everything the change feed reports — type definitions AND guide
    edits. It must cover both, or `since_cursor=cursor` would keep reporting stale guide edits
    as new forever."""
    with db.read() as cur:
        r = cur.execute(
            "SELECT MAX(m) AS c FROM (SELECT MAX(created_tx) m FROM node_type "
            "UNION ALL SELECT MAX(created_tx) FROM edge_type "
            "UNION ALL SELECT MAX(updated_tx) FROM guide_section)").fetchone()
    return int(r["c"] or 0)


def changes_since(db: Database, since_cursor: int = 0, *, include_guide: bool = True,
                  limit: int = 200) -> dict:
    """Schema (and optionally guide) changes newer than `since_cursor`, with provenance.

    Agents: pass the `cursor` you got from a previous schema_get/schema_changes. The reply tells
    you exactly which types appeared or were re-versioned, by whom, when, and why — so you can
    re-read only what moved instead of re-fetching the whole schema.
    """
    changes = []
    with db.read() as cur:
        for kind, tbl in (("node", "node_type"), ("edge", "edge_type")):
            rows = cur.execute(
                f"SELECT x.name, x.version, x.status, t.tx_id, t.tx_time, t.agent_id, t.reason "
                f"FROM {tbl} x JOIN tx t ON t.tx_id = x.created_tx "
                f"WHERE x.created_tx > ? ORDER BY x.created_tx DESC LIMIT ?",
                (since_cursor, limit)).fetchall()
            for r in rows:
                changes.append({"kind": kind, "name": r["name"], "version": r["version"],
                                "status": r["status"], "tx": r["tx_id"], "at": r["tx_time"],
                                "by": r["agent_id"], "why": r["reason"]})
        guide_changes = []
        if include_guide:
            grows = cur.execute(
                "SELECT g.name, g.guide_version, t.tx_id, t.tx_time, t.agent_id "
                "FROM guide_section g JOIN tx t ON t.tx_id = g.updated_tx "
                "WHERE g.updated_tx > ? ORDER BY g.updated_tx DESC", (since_cursor,)).fetchall()
            guide_changes = [{"section": r["name"], "guide_version": r["guide_version"],
                              "at": r["tx_time"], "by": r["agent_id"]} for r in grows]
    changes.sort(key=lambda c: c["tx"], reverse=True)
    return {"schema_version": int(db.meta_get("schema_version", "0")),
            "cursor": _schema_cursor(db), "since_cursor": since_cursor,
            "schema_changes": changes, "guide_changes": guide_changes,
            "changed": bool(changes or guide_changes)}
