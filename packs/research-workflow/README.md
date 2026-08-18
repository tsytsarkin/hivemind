# research-workflow pack

Research-workflow vocabulary **captured from the live `default` project** — these types were added
by agents at runtime (via `schema_propose`/`schema_apply`) and existed in no pack file, so they were
not version-controlled. This pack pins them.

**Node types:** `claim` (a statement with method/denominator/confidence), `lead` (an open question
with priority/state/next_action), `measurement` (value + denominator + unit + scorer),
`verdict` (subject + grade + venue + rationale), `note`, `lane` (a workstream), `instrument`
(a scorer/tool with recall/defect stats).

**Edge type:** `documented_by` (finding/component/entry_point/function → lane).

Layers on top of `security-research` (its `documented_by` range references `finding` etc.), so
apply that first:
```sh
hivemind-admin --project default apply-pack packs/security-research/schema.json
hivemind-admin --project default apply-pack packs/ios-macos-attack-surface/schema.json
hivemind-admin --project default apply-pack packs/research-workflow/schema.json
```
Re-applying is idempotent. Verified to match the live project exactly (a re-apply reports every
type `unchanged`). See [../../docs/packs.md](../../docs/packs.md).
