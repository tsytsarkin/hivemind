import pytest
from hivemind_server.db import Database, Invalid
from hivemind_server import schemas, graph


@pytest.fixture()
def fresh(tmp_path):
    return Database(tmp_path / "s.db")


def test_propose_new_type_then_promote(fresh):
    r = schemas.propose_type(fresh, "agent", "node", "paper",
                             {"type": "object", "properties": {"title": {"type": "string"}}})
    assert r["status"] == "proposed"
    # proposed types are usable for writes (flagged), so this succeeds
    graph.upsert_node(fresh, "agent", "paper", {"title": "hi"})
    sch = schemas.get_schema(fresh, kind="node", name="paper")
    assert sch["node_types"][0]["status"] == "proposed"
    schemas.promote_type(fresh, "human", "node", "paper")
    assert schemas.get_schema(fresh, kind="node", name="paper")["node_types"][0]["status"] == "active"


def test_near_duplicate_blocked(fresh):
    schemas.propose_type(fresh, "a", "node", "KernelExtension", {"type": "object"})
    with pytest.raises(Invalid) as e:
        schemas.propose_type(fresh, "a", "node", "KernelExtensions", {"type": "object"})
    assert "looks like existing" in str(e.value)
    # force overrides with a warning
    r = schemas.propose_type(fresh, "a", "node", "KernelExtensions", {"type": "object"}, force=True)
    assert r["warnings"]


def test_additive_change_ok_destructive_blocked(fresh):
    schemas.apply_pack(fresh, "op", {"name": "p", "node_types": {
        "component": {"schema": {"type": "object",
                                 "properties": {"name": {"type": "string"}},
                                 "required": ["name"]}}}})
    # additive: add an optional property → OK
    ok = schemas.propose_type(fresh, "a", "node", "component",
                              {"type": "object",
                               "properties": {"name": {"type": "string"},
                                              "note": {"type": "string"}},
                               "required": ["name"]})
    assert ok["version"] == 2
    # destructive: add a new required field → rejected
    with pytest.raises(Invalid) as e:
        schemas.propose_type(fresh, "a", "node", "component",
                             {"type": "object",
                              "properties": {"name": {"type": "string"}},
                              "required": ["name", "note"]})
    assert "non-additive" in str(e.value)


def test_props_validation_enforced(fresh):
    schemas.apply_pack(fresh, "op", {"name": "p", "node_types": {
        "finding": {"schema": {"type": "object",
                               "properties": {"severity": {"enum": ["low", "high"]}},
                               "required": ["severity"]}}}})
    graph.upsert_node(fresh, "a", "finding", {"severity": "high"})       # ok
    with pytest.raises(Invalid):
        graph.upsert_node(fresh, "a", "finding", {"severity": "nope"})    # bad enum
    with pytest.raises(Invalid):
        graph.upsert_node(fresh, "a", "finding", {})                      # missing required


def test_apply_pack_defines_edges_with_traits(fresh):
    schemas.apply_pack(fresh, "op", {"name": "sec", "edge_types": {
        "contradicts": {"schema": {"type": "object"}, "assertive": True, "symmetric": True},
        "calls": {"schema": {"type": "object"}, "versioned": False}}})
    sch = schemas.get_schema(fresh, kind="edge")
    by = {e["name"]: e for e in sch["edge_types"]}
    assert by["contradicts"]["assertive"] == 1 and by["contradicts"]["symmetric"] == 1
    assert by["calls"]["versioned"] == 0
    assert sch["schema_version"] >= 2


def test_apply_pack_idempotent(fresh):
    pack = {"name": "p",
            "node_types": {"thing": {"schema": {"type": "object",
                           "properties": {"x": {"type": "string"}}}}},
            "edge_types": {"rel": {"schema": {"type": "object"}, "versioned": True,
                                   "symmetric": True}}}
    r1 = schemas.apply_pack(fresh, "op", pack)
    assert r1["created"]["node"] == ["thing@1"] and r1["created"]["edge"] == ["rel@1"]
    # re-apply the identical pack -> no new versions
    r2 = schemas.apply_pack(fresh, "op", pack)
    assert r2["created"] == {"node": [], "edge": []}
    assert r2["unchanged"] == {"node": ["thing"], "edge": ["rel"]}
    assert schemas.get_schema(fresh, kind="node", name="thing")["node_types"][0]["version"] == 1
    # a real change still bumps
    pack["node_types"]["thing"]["schema"]["properties"]["y"] = {"type": "string"}
    r3 = schemas.apply_pack(fresh, "op", pack)
    assert r3["created"]["node"] == ["thing@2"]
