# Domain packs

A **pack** turns the domain-agnostic engine into a domain-aware one, entirely as *data* — the
engine code never changes. A pack is a directory under `packs/`:

```
packs/<name>/
├── schema.json        # required: node_types + edge_types (with generic traits)
├── guide/*.md         # optional: guide sections uploaded to the live guide (by hivemind-admin)
└── README.md          # optional: how to apply it, what it models
```

## `schema.json` shape
```jsonc
{
  "name": "my-domain",
  "description": "…",
  "node_types": {
    "<type>": { "schema": { /* JSON Schema 2020-12 for props */ }, "parent": "<optional>" }
  },
  "edge_types": {
    "<type>": {
      "schema": { /* JSON Schema for edge props */ },
      "src_types": ["<node type>", "…", "*"],   // domain ('*' = any)
      "dst_types": ["…"],                          // range
      "cardinality": "1:1|1:N|N:N",
      "versioned": true,                            // false = bulk edge (edge_bulk), no history
      "symmetric": false, "transitive": false, "acyclic": false,
      "assertive": false                            // edges carry props.status; open ones flag endpoints
    }
  }
}
```
The engine enforces these **traits**, never the type *names* — a `contradicts` edge is just an
`assertive` type; a `calls` graph is just a `versioned:false` type. (See [data-model.md](data-model.md).)

## Applying a pack
```sh
# operator, on the server host (also loads the pack's guide/*.md as guide sections):
hivemind-admin --project default apply-pack packs/<name>/schema.json

# or remotely via the client (trusted-team):
hivemind schema apply packs/<name>/schema.json
```
`apply_pack` returns `{created, unchanged}`. It defines types as **active**.

## Schema mechanics (how it actually works)

**Types are data, not DDL.** Node and edge *types* live as rows in `node_type` / `edge_type`
inside each project's database. Hivemind never runs `ALTER TABLE` at runtime — adding a type is an
INSERT, so the engine itself stays domain-free and a project can grow new vocabulary while it runs.

**Every type is versioned; nothing is mutated in place.** Changing a type mints `name@N+1` and
leaves `name@N` intact. Writes validate against the newest *usable* version (active preferred, else
proposed).

**Old data is never retro-validated.** Each `node_version` / `edge_version` records the
`schema_ver` that validated it. A node written under `finding@1` stays valid forever, even after
`finding@4` exists. This is what makes schema evolution safe on a live graph.

**Two ways a type enters a project:**

| | `schema_propose` (agents) | `apply_pack` (operators) |
|---|---|---|
| Status created | `proposed` (usable, flagged) | `active` |
| Additive-only guard | **always enforced** — cannot be bypassed | enforced; `--force` to override |
| Near-duplicate check | yes (blocks `Kext` vs `KernelExtension` sprawl) | no |
| Promotion | a human runs `schema_promote` | already active |

**Additive means:** add a type, add an *optional* property, widen an enum, add an edge type.
**Non-additive** (new required field, removed property, narrowed enum) is refused, because it would
invalidate data already in the graph. Agents can never force it; an operator must pass `--force`.

**Changes take effect immediately** — the server resolves types from the database per write, so no
restart is needed after a propose/promote/apply.

**Knowing something changed.** `schema_get` returns a monotonic `schema_version` plus a `cursor`.
Pass that cursor to **`schema_changes(since_cursor=…)`** to get exactly which types appeared or were
re-versioned, *by whom, when, and why* (it reads the same `tx` provenance every write records), plus
any guide edits. Write results (`graph_upsert`, `graph_link`) echo the current `schema_version`, so
an agent notices drift mid-session without polling. There is no server push: Claude Code agents are
turn-based, so a staleness signal on the next call is the mechanism that actually works.

## Applying packs: order matters

Apply a base pack before packs that extend it. A later pack may **widen** a type the base defined
(the iOS pack widens `finding` with campaign fields) — that is the intended layering.

The consequence: **re-applying the *base* pack afterwards is refused**, because relative to the
widened type it would *remove* properties. That is the guard protecting your data, not a bug:

```
apply security-research → ios-macos-attack-surface → research-workflow   # ok, in order
re-apply ios-macos-attack-surface   → no-op (idempotent)
re-apply security-research          → REFUSED: non-additive (would drop finding's added fields)
```
Each pack is idempotent **on its own**. If you re-apply packs on every deploy, apply the whole stack
in order, or re-apply only the outermost pack. Never `--force` a base pack over a widened type
unless you intend to drop those fields.

## Packs are COPIED, not linked

`apply_pack` **copies** the pack's types into that project's database as rows. There is no live
link back to the file afterwards — editing a pack does **not** change any project until you
re-apply it, and a project's schema can legitimately drift ahead of the pack (agents may add types
at runtime via `schema_propose`). To pull that drift back under version control, export the live
types into a pack file and commit it (`packs/research-workflow/` was captured exactly this way).

## Will re-applying a pack break running code?

**Additive changes are safe:**
- Every `node_version` records the `schema_ver` that validated it, so **existing data stays valid
  under the version that wrote it** — old rows are never retro-validated.
- Types are versioned, never mutated in place; adding a type or an optional field mints a new
  version and leaves the old one intact.
- Node/edge props default to `additionalProperties: true`, so a client that doesn't know a new
  field simply ignores it.
- The server reads types from the DB per write, so changes take effect **without a restart**.

**Non-additive changes would break writers** (a newly-required field, a removed property, a
narrowed enum): existing writers start failing validation. `apply_pack` therefore **refuses** such
a change by default and tells you which rule it violates; pass `--force` / `force=true` only when
you accept it. Agent-facing `schema_propose` can *never* force.

## Layering (compose multiple packs)
Packs are **additive** and **layerable**: apply several to one project and they compose. A later
pack may (a) add new node/edge types, and (b) **widen** an existing type — add optional properties
or extend an enum (never remove/require, which would break existing data). Each such change mints a
new **version** of that type; old data stays valid under the version that wrote it.

## Idempotent re-apply
Re-applying a pack whose types are byte-identical to what's live is a **no-op** — unchanged types
are skipped (reported under `unchanged`), so `schema_version` and type versions don't churn. This
makes "apply the pack on every deploy" safe.

## Bundled example packs
| Pack | Models |
|---|---|
| `security-research` | iOS/macOS vuln-research vocabulary: `component`, `function`, `artifact`, `finding`, `poc`, `report`, `host`; edges `contradicts` (assertive), `refines`, `derived_from`, `evidence_for`, `confirms`/`refutes`, `depends_on` (acyclic), `calls`/`reachable_from` (bulk). |
| `research-workflow` | Workstream vocabulary captured from a live project: `claim`, `lead`, `measurement`, `verdict`, `note`, `lane`, `instrument`; edge `documented_by`. |
| `ios-macos-attack-surface` | Layers attacker-reachability on top: `principal`, `entry_point`, `gate`, `format`, `build` (+ widens `finding`); edges `attacker_reaches`, `attacker_blocked`, `exposes`, `gated_by`, `satisfies`, `parses`, `runs_as`, `affects`, `present_on`. Apply **after** `security-research`. |

## Contributing a pack — please do!

Packs are the intended way to make Hivemind fit a domain, and **new packs are very welcome**. A pack
is just a `schema.json` (plus optional `guide/*.md` and a `README.md`) — no engine code, no Python,
nothing to compile. If you have modelled a domain that others might reuse — literature review,
incident response, codebase mapping, lab/experiment tracking, procurement, anything — please open a
PR adding it under `packs/<your-pack>/`, or fork and publish your own.

Good packs tend to:
- **model nouns as node types and verbs as edge types**, leaning on the generic traits
  (`versioned`, `symmetric`, `acyclic`, `assertive`, `src_types`/`dst_types`) instead of inventing
  engine features;
- keep `required` minimal — agents record partial knowledge, and a strict schema pushes them to
  invent placeholder values;
- ship a short `guide/` section explaining the workflow, since that is what agents actually read;
- say in the README which pack (if any) it layers on top of.

You can also **capture a pack from a running project** — if your agents grew useful types at
runtime, export them into a `schema.json` and commit it so the vocabulary is version-controlled and
shareable (`packs/research-workflow/` was produced exactly this way).

## Writing your own
Copy a bundled pack, edit `schema.json` (keep changes additive if others depend on the types),
add `guide/*.md` explaining the workflow, and `apply-pack` it. To model a completely different
domain, start from an empty project and apply only your pack.
