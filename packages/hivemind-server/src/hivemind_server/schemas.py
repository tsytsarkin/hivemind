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


def type_allows(cur, kind: str, name: str, node_type: str) -> bool:
    """domain/range check helper: is node_type allowed by an edge endpoint's type list?"""
    return name == "*" or node_type == name


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
