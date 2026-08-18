# Node identity, duplicates, and concurrent writers

Generic discipline for any project where more than one agent writes to the graph.

## Shared vocabulary nodes MUST be subject-keyed

Nodes that many other nodes point at — attacker positions, OS builds, and anything else that
behaves as a shared identity rather than a per-item record — must be created with a stable
`subject_key` and `subject_version="-"`. Look one up before creating it:

    graph_get(subject_key="principal:<slug>", subject_version="-")
    graph_get(subject_key="build:<BuildID>",  subject_version="-")

If it misses, create it with exactly that key.

**Never create an unkeyed shared node.** A node with no `subject_key` is reachable only by its
`node_id`, so no other agent can find it, reuse it, or supersede it — they will create their own
instead and the graph silently forks. This is the single most common way two importers end up with
parallel vocabularies and split edges.

The project's own canonical list of these keys belongs in a deployment guide section, not in this
pack — the pack defines the *rule*, the deployment defines the *values*. Read the section index
(`guide_get()`) and check for one before inventing keys.

## Reconciling duplicates

When two nodes denote the same real thing:

1. Bind them with `same_as` — symmetric, non-assertive, carrying `confidence`
   (`exact`|`semantic`|`partial`), a `reason`, and `canonical` set to the surviving `node_id`.
   **Do not use `contradicts`.** A duplicate is not a dispute; `contradicts` is assertive and would
   wrongly flag both nodes `disputed`.
2. Prefer the subject-keyed node as canonical. If neither is keyed, create a keyed one and make
   both aliases of it.
3. **Re-point the edges.** A `redirect_to` column exists on the node table and reads honour it, but
   traversal resolves redirects only on the START node — edge endpoints are matched raw. Redirecting
   therefore *orphans* the loser's edges from canonical queries instead of merging them. Re-creating
   each edge against the canonical node is the only correct merge.
4. Mark the loser: `deprecated: true`, a `canonical_subject_key`, and a name prefixed
   `[DEPRECATED ALIAS -> …]`. There is no delete; marking is the strongest available signal.

Never silently delete or overwrite another agent's node.

## Writing safely alongside other agents

- Pass `expected_head` when superseding. A 409 means "re-read and retry", not failure.
- **A refused write is not a transport error.** Validation and endpoint-type failures come back as
  `{"ok": false, "error_kind": "invalid", ...}` inside a normal 200 response. A client that only
  checks for a JSON-RPC `error` will report success while every write silently vanishes — check
  `ok` on every call.
- Endpoint types are enforced against each edge type's declared `src_types`/`dst_types`.
- Widening an edge's `dst_types` or adding an optional property is additive and safe; re-applying a
  pack inserts a new type *version* and leaves existing data valid.
