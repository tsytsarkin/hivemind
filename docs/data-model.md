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
same user directly — `node.created_by`, `node_version.author_user`, `edge_version.author_user`,
and the same column on `skill_version`, `trap`, `tool_version` and `guide_proposal` — while a bulk
edge (`edge_bulk`) has no version row and so is attributed through its `created_tx` and
`source_tag` alone. NULL means "written before authorship existed" and reads as `legacy:unknown`;
`hivemind-admin backfill-authors` fills those from each row's `tx.agent_id` as `legacy:<label>`,
never as a real username (`:` is illegal in one).

Three different questions, three different fields, and a read answers all three:
`created_by` is the node's **first** author; `author` is the author of the version being returned
(the head, or the as-of one); and `contributors` is **everyone who ever revised it**. The last is
`GROUP BY author_user` over that node's `node_version` rows, **computed on read and never stored**
— a denormalized `authors` array would be a second copy of what the version rows already say and
would drift from them the first time a row was corrected. `agent_label` sits beside `author` as the
free-form `agent` string the caller passed: kept so "which job was this" survives, never mistaken
for identity. `author=` on the four searches filters on the same column (see [api.md](api.md)).

`edge` holds the identity of each edge (type + endpoints); `edge_version` and `edge_bulk` hold
what it says.

## Search index
`node_fts` (BM25 over prose) and `sym_fts` (trigram over symbols) are the two halves `graph_search`
fuses with RRF; both are FTS5 virtual tables rebuilt by `hivemind-admin reindex`. `meta` is a
single key/value table for per-database counters such as `guide_version`.

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

## Agent bus (no tables at all)

The bus stores **nothing**. Presence is the open WebSocket, messages live in the hub's memory, and
a restart is a clean slate — so nothing it carries writes a `tx` row, none of it is searchable, and
there is no reaper to keep honest. The v1 polling bus did have six tables
(`bus_session`/`bus_capability`/`bus_membership`/`bus_message`/`bus_request`/`bus_ref`);
`Database._DROPPED` drops them at startup so a database that predates the rewrite converges on the
shape of a fresh one, and the data was ephemeral by design so there was nothing to migrate. The
only durable trace of a conversation is the local JSONL inbox each listener appends on the
*receiving machine* (4 MiB, one rotation) — outside the database entirely. Details:
[bus.md](bus.md).

## Projects

A project is a directory — `<projects_root>/<name>/{hivemind.db, blobs/, tokens.json,
project.json, bus_secret}` — so "which project" is a filesystem boundary, not a column. Three of
those five are credentials or ACL state and are written `0600`: `tokens.json`, `project.json` and
`bus_secret` (the HMAC key that signs this project's bus listen keys; it is the only one *not* in
`deploy/backup.sh`, on purpose — see `deploy/restore.md`). `project.json` holds
`{name, visibility: "shared"|"private", owner, members[], label, created, session, last_touched}`
and **is the ACL**: `projects_meta.can_access` is the one predicate every surface consults. It is
read through an mtime+size-stamped cache, so a share made in another process takes effect on the
next request with nothing to invalidate, and a file that is missing or will not parse reads back
`private` with no owner — reachable by nobody. See [security.md](security.md).

## Schema, tools, guide
`node_type`/`edge_type` (versioned, additive-only, proposed→active; operator `apply_pack` is
idempotent — byte-identical types are skipped, so re-applying a pack causes no version churn);
`tool`/`tool_version` (immutable, yankable); `guide_section`/`guide_proposal` (human-gated).
