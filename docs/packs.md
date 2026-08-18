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

## Writing your own
Copy a bundled pack, edit `schema.json` (keep changes additive if others depend on the types),
add `guide/*.md` explaining the workflow, and `apply-pack` it. To model a completely different
domain, start from an empty project and apply only your pack.
