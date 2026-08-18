# ios-macos-attack-surface pack

Adds attacker-position / entry-point / gate **reachability** modelling on top of the
`security-research` pack. Additive: it widens `finding` (broader `status` enum + campaign fields
like `case_id`, `package`, `ledger_row`, `target_flag`) and adds new types.

**Node types:** `principal` (attacker position — renderer, sandboxed app, daemon, kernel, …),
`entry_point` (mach/xpc/iokit/mig/url/file-format/…), `gate` (entitlement/sandbox/tcc/…),
`format`, `build`.

**Edge types:** `attacker_reaches` / `attacker_blocked` (principal → entry_point/component/…,
with a `verification` provenance enum), `exposes`, `gated_by`, `satisfies`, `parses`, `runs_as`,
`affects`, `present_on` (finding/… → build, with an `evidence` enum).

Apply it **after** `security-research` (it references `component`/`function` from that pack):
```sh
hivemind-admin --project default apply-pack packs/security-research/schema.json
hivemind-admin --project default apply-pack packs/ios-macos-attack-surface/schema.json
```
Re-applying is idempotent. See [../../docs/packs.md](../../docs/packs.md).
