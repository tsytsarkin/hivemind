import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest
from hivemind_server.db import Database
from hivemind_server import schemas


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "hm.db")
    # register a couple of node/edge types (as a domain pack would)
    with d.write("test-setup", "seed types") as tx:
        cur = tx.cur
        obj = {"type": "object", "additionalProperties": True}
        schemas.define_type(cur, tx, "node", "component", obj, status="active")
        schemas.define_type(cur, tx, "node", "finding", obj, status="active")
        schemas.define_type(cur, tx, "node", "function", obj, status="active")
        schemas.define_type(cur, tx, "edge", "refines", obj, status="active",
                            traits={"versioned": True})
        schemas.define_type(cur, tx, "edge", "contradicts", obj, status="active",
                            traits={"versioned": True, "assertive": True, "symmetric": True})
        schemas.define_type(cur, tx, "edge", "calls", obj, status="active",
                            traits={"versioned": False})  # bulk
        schemas.define_type(cur, tx, "edge", "depends_on", obj, status="active",
                            traits={"versioned": True, "acyclic": True})
    return d
