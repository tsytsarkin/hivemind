# Security-research domain

This deployment models iOS/macOS vulnerability research. Node/edge TYPES here are defined by the
`security-research` pack (see `schema_get`); the engine itself knows none of these words.

## Node types
`component` (a kext/daemon/framework), `function` (a symbol), `artifact` (a binary/log/PoC blob),
`finding` (a candidate/confirmed bug), `poc`, `report`, `host`.

## Subject-versioning (use it!)
A `component` or `function` behaves differently across OS builds. Model each build as its own
subject cell: `subject_key="IOSurfaceRootUserClient"`, `subject_version="26.6"`,
`subject_order="0266"`. "X on 26.5" and "X on 26.6" are DIFFERENT nodes — never a `contradicts`.

## Edges
- `contradicts` (assertive, symmetric): two claims that genuinely conflict on the SAME subject
  cell. Set `props.status="open"` until adjudicated; put the resolution in `props.resolution`.
  Nodes with an open `contradicts` show up flagged `disputed` — resolve before building on them.
- `refines` / `derived_from` / `evidence_for` / `confirms` / `refutes`: curated, versioned
  relationships (patchdiff → candidate → PoC → report; a crashlog as `evidence_for` a finding).
- `depends_on` (acyclic): exploit-primitive dependencies.
- `calls` / `reachable_from` (bulk, versioned=0): imported call/reachability graphs. Load with
  `graph_bulk_load(edge_type, source_tag="kernelcache@<build>", edges=[...])`; re-import replaces
  the whole tagged set.

## Workflow
1. `schema_get` + `guide_get` to orient. 2. Create/He supersede a `finding`; attach evidence
   artifacts (`hivemind artifact put`, then `artifact_attach(..., role="crashlog|binary|poc")`).
3. Link `evidence_for`, `derived_from`. 4. When another agent's claim conflicts on the same build,
   add `contradicts {status:"open"}` rather than overwriting. 5. Package a reusable RE tool and
   `hivemind tool publish` it so other agents/machines can reuse it.
