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
| `ios-macos-attack-surface` | Layers attacker-reachability on top: `principal`, `entry_point`, `gate`, `format`, `build` (+ widens `finding`); edges `attacker_reaches`, `attacker_blocked`, `exposes`, `gated_by`, `satisfies`, `parses`, `runs_as`, `affects`, `present_on`. Apply **after** `security-research`. |

## Writing your own
Copy a bundled pack, edit `schema.json` (keep changes additive if others depend on the types),
add `guide/*.md` explaining the workflow, and `apply-pack` it. To model a completely different
domain, start from an empty project and apply only your pack.
