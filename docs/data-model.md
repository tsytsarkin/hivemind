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
Every write inserts a `tx(tx_id, tx_time, agent_id, user_id, device, reason)` row; `tx_id` is the
as-of coordinate. `user_id` is the **author**, taken from the caller's token and not settable from
a tool argument; `agent_id` is a free-form label ("which job was this"). Versioned rows carry the
same user directly — `node.created_by`, `node_version.author_user`, `edge_version.author_user` —
while a bulk edge (`edge_bulk`) has no version row and so is attributed through its `created_tx`
and `source_tag` alone. NULL means "written before authorship existed" and reads as
`legacy:unknown`.

## Blobs
Content-addressed files (`blob`, `blob_ref`, `blob_pin`); attach to any node/edge **version**.

## Mini-skills and traps
`skill` / `skill_version` — documented procedures, immutable per version exactly like the tool
registry (yank, never delete; only the newest non-yanked version is indexed in `skill_fts`).
`trap` — recorded dead-ends: `what_failed` + `symptom` are NOT NULL by design, optional
`node_id` and `subject_key`/`subject_version` scope it, and `status`
(`active`/`disputed`/`retired`) makes it falsifiable. Indexed in `trap_fts`; every record and
status change writes a `tx` row for provenance.
`skill_link` / `tool_link` attach either to a node (`relation`, plus `source`=auto|confirmed and the retrieval `score`) and are what `graph_get` surfaces; they are written automatically on publish and a confirmed link is never downgraded. `embedding` holds one L2-normalised float32 vector per skill/tool per backend (`model`), used for semantic search; `skill_fts` / `tool_fts` hold the lexical side. Details: [skills-and-traps.md](skills-and-traps.md).

## Agent bus (ephemeral — no provenance)

Six tables for live coordination, written through `Database.write_light()` and reaped on a TTL.
They are the one part of the store that inserts **no `tx` row**: presence and chatter are worthless
five minutes later, so paying revision-chain, `graph_search` and backup costs for them would be a
mistake. `bus_session(session_id, label, harness, interruptible, cursor, expires_at, ttl, ended_at)`
— identity is per **session**, never per token, since one token runs many agents with different
capabilities. `ttl` is stored per session so that both `bus_ping` *and* `bus_poll` extend
`expires_at` by what that session actually asked for: a session stays alive by working, not only by
pinging. `bus_capability(session_id, name, attrs)` holds dotted, self-asserted names for exact or
`prefix.*` queries; `bus_membership` is room joins. `bus_message(seq, room, to_session, kind,
reply_to, request_id, expires_at)` — one global `AUTOINCREMENT` `seq` orders everything, so a
session needs only a single integer cursor, and `AUTOINCREMENT` (not a bare rowid) is what stops a
reap of the tail from reusing numbers and silently rewinding every cursor past them.
`bus_request(request_id, requester, needs, state, claimed_by, lease_expires_at, attempts)` is
single-winner dispatch, decided by one conditional `UPDATE … WHERE claimed_by IS NULL`. `bus_ref`
holds typed pointers (`node`/`version`/`subject`/`traversal`/`search`) from a message, request or
response into the graph, validated at write time. The link is deliberately **one-way** — the bus
points at the graph and the graph never points back — so reaping a message cannot leave the graph
holding a dead reference. Details: [bus.md](bus.md).

## Schema, tools, guide
`node_type`/`edge_type` (versioned, additive-only, proposed→active; operator `apply_pack` is
idempotent — byte-identical types are skipped, so re-applying a pack causes no version churn);
`tool`/`tool_version` (immutable, yankable); `guide_section`/`guide_proposal` (human-gated).
