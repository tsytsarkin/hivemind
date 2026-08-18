# ios-macos-attack-surface pack

Adds attacker-position / entry-point / gate **reachability** modelling for iOS and macOS research,
on top of the `security-research` pack. Purely additive: it widens `finding` (broader `status` enum
plus optional reporting fields) and adds new node and edge types.

**Node types:** `principal` (an attacker position), `entry_point` (where attacker-controlled input
crosses a trust boundary), `gate` (an entitlement, sandbox rule, TCC service or authorization
check), `format` (a parsed data format / UTI), `build` (an OS build), `lane` (a workstream).

**Edge types:** `attacker_reaches` and `attacker_blocked` (both carry mandatory provenance —
see below), `exposes`, `gated_by`, `satisfies`, `parses`, `runs_as`, `affects`, `present_on`,
`documented_by`, `same_as`.

**The design point:** a reachability claim cannot be recorded without saying how it was
established. `attacker_reaches` requires `verification`, and `attacker_blocked` additionally
requires a `control_test` — so a proven negative always names the test that proves it, and absence
of an edge means *unknown* rather than *unreachable*. That constraint is enforced by JSON Schema at
write time, not by convention.

Guide sections shipped with this pack: `reachability` (the modelling discipline and evidence
ladder) and `identity` (subject-keyed shared nodes, duplicate reconciliation, safe concurrent
writes). A deployment's own canonical `principal:*` / `build:*` values belong in a deployment guide
section, not here.

Apply it:

```sh
hivemind-admin --project <project> apply-pack packs/security-research/schema.json
hivemind-admin --project <project> apply-pack packs/ios-macos-attack-surface/schema.json
```
