# Data model

Domain-agnostic engine; all *types* are data (rows in `node_type`/`edge_type`).

## Nodes
`node(node_id, node_type, subject_key?, subject_version?, subject_order?, redirect_to?)`.
Two versioning axes:
- **Revision** — `node_version(version_id, node_id, seq, prev_version, props, schema_ver,
  content_hash, tx_from, tx_to)`. Exactly one open head per node (`tx_to = SENTINEL`), enforced by
  a partial unique index. Supersede by writing a new version; optimistic CAS via `expected_head`.
- **Subject-version** — nodes sharing `subject_key` are cells of one thing at different
  `subject_version`s; each cell has its own revision chain. `subject_order` sorts them for as-of.

## Edges
Fully schema-defined types with generic traits (`directed/symmetric/transitive/acyclic/versioned/
assertive`, `src_types/dst_types`, `cardinality`). `versioned=1` → `edge_version` (history);
`versioned=0` → `edge_bulk` (high-volume, replace-by-`source_tag`). `assertive=1` → edges carry
`props.status`; a node with an open assertive edge is flagged `disputed`.

## Provenance
Every write inserts a `tx(tx_id, tx_time, agent_id, reason)` row; `tx_id` is the as-of coordinate.

## Blobs
Content-addressed files (`blob`, `blob_ref`, `blob_pin`); attach to any node/edge **version**.

## Schema, tools, guide
`node_type`/`edge_type` (versioned, additive-only, proposed→active); `tool`/`tool_version`
(immutable, yankable); `guide_section`/`guide_proposal` (human-gated).
