# Edge traits and the two axes

The engine special-cases no relationship by name. `cites`, `depends_on` and `same_as` are just
strings to it; every behaviour you want comes from a **trait** declared on the edge type. Name the
relationship for the humans, then pick the traits for the engine.

| Trait | What the engine actually does | Use it when |
|---|---|---|
| `versioned: true` (default) | Per-edge history: one current head, a `prev_version` chain behind it, re-linking supersedes rather than duplicates | A person curates this claim by hand and how it changed matters |
| `versioned: false` | Sends the edge to the bulk table instead — no per-edge history, `source_tag` required on every write, and `graph_bulk_load` replaces that whole `(type, tag)` set atomically | It is imported wholesale from a tool: call graphs, dependency graphs, reachability maps. Millions of these are cheap; millions of versioned ones are not |
| `symmetric: true` | Canonicalises write orientation, so linking A→B and later B→A supersedes the one edge instead of creating a second. Traversal is **not** mirrored — query it with `direction="both"` | The two ends have equal standing: "related to", "duplicate of", "same as" |
| `transitive: true` | **Not enforced.** Stored and reported by `schema_get`, but no engine code acts on it; reaching A→C is done by asking `graph_neighbors` for `depth=2` | You want the intent on record for readers. Never rely on it to make a query return more |
| `acyclic: true` | A self-link, or any insert that would close a cycle over current edges of that type, is rejected by `graph_link` | It has to stay a DAG: dependencies, containment, refinement chains |
| `assertive: true` | An edge whose `props.status` is `open` — or absent, which counts as open — flags **both** endpoints `disputed` on every read; any other value clears the flag | Something should stop a reader before they build on a claim: disputes, open questions, "needs review" |
| `src_types` / `dst_types` | Domain and range, checked against the endpoints' node types on every `graph_link` (`["*"]` = any) | Cheaply reject a nonsense edge at write time instead of tripping over it in a traversal later |
| `cardinality` | **Not enforced.** `1:1` \| `1:N` \| `N:N` is stored and reported; nothing rejects a write that exceeds it | Documenting the shape you intend. If it must hold, hold it in your own write path |
| `directed: true` (default) | **Not enforced.** Recorded only; orientation is decided by how you write the edge and by the `direction=` you traverse with | Leave it alone. If the pair is genuinely unordered, reach for `symmetric` instead |

Defaults matter: an edge type you declare with no traits is `versioned`, `directed`, `N:N`,
`["*"]`→`["*"]`, and neither symmetric, acyclic nor assertive. Say the traits you want out loud.

## Gotchas worth knowing before you choose

- **Bulk loads skip validation.** `graph_bulk_load` does not validate edge props against the type's
  schema, does not check `src_types`/`dst_types`, and does not run the `acyclic` guard. Only
  `graph_link` does all three. A bulk type is a fast import path, not a checked one.
- **A bulk edge's identity includes its `source_tag`**, so the same pair imported under two tags is
  two rows — that is what makes re-importing one tool's output replace only its own edges.
- **Node types have a `parent` field that the engine records and never applies.** Props are
  validated against that type's own JSON Schema alone, so a child type must restate whatever it
  needs. Do not model inheritance you are relying on.
- **Additive-only is a floor, not a lock.** Adding an optional property, dropping a `required`
  entry or widening an enum is accepted; adding a required field, removing a property, narrowing an
  enum or tightening `additionalProperties` is refused as destructive. Nothing deletes a type at
  all — which is why an unnecessary type is forever.

## The two axes are not edges

Both are built into every node. Neither should be modelled as a relationship, and confusing them
is the most expensive modelling mistake available here.

- **Revision axis — the same claim, corrected.** Supersede the current head and the `prev_version`
  chain stays walkable, so `history=true` replays it and `as_of=<tx or timestamp>` reads the graph
  as it stood then. Pass `expected_head` to make a concurrent edit fail loudly instead of
  clobbering someone. This is automatic for every node; it is never a type decision.
- **Subject axis — the version of the *described thing*.** `subject_key` identifies the thing,
  `subject_version` is the coordinate of one version of it (a build, a release, a digest), and
  `subject_order` is the optional sortable key that makes "latest" and "as of version X" work when
  the coordinates do not sort naturally. There is exactly one node per `(subject_key,
  subject_version)` cell, the cells coexist, and each carries its own independent revision chain.

The consequence worth repeating to the user, because it is what the engine cannot know: **two
subject cells that disagree are not in conflict.** They describe different versions of the thing,
and both are correct. Only a disagreement *within* one cell is a real one — that is where an
`assertive` edge belongs. Linking two subject cells with one flags both `disputed` forever over
nothing.
