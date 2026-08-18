# Authoring the live guide + domain packs

The **guide** is how domain knowledge reaches agents at runtime (the on-disk skill stays tiny).

- Sections are markdown in `guide_section`, budget-capped (~5k tokens). `core` is seeded with the
  framework guide. Add domain sections via a pack's `guide/*.md` or `hivemind-admin set-guide`.
- Agents call `guide_get()` / `guide_get(section)`; the skill also dynamic-injects `core` via
  `guide.sh` (best-effort, never fails).
- **Firewall**: agents `guide_propose`; an operator `hivemind-admin merge-guide <id>` publishes it,
  bumping `guide_version`. Keep instructions out of the agent-writable graph.

A **domain pack** is `schema.json` (`node_types`, `edge_types` with traits) + optional `guide/*.md`:
```sh
hivemind-admin --project <p> apply-pack packs/<yourpack>/schema.json   # loads schema + guide/*.md
```
Swap the pack to change the domain; the engine is unchanged. See [packs.md](packs.md) for details on
**layering** (apply several packs to one project — they compose additively) and the **idempotent**
re-apply (unchanged types are skipped, so re-running a pack on every deploy is safe).
