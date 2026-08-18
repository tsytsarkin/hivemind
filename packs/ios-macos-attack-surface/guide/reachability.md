# Attacker reachability

Models WHO the attacker is, WHERE input crosses in, and WHAT PROVES the crossing.
See the `taxonomy` section for the canonical `principal:*` / `build:*` keys — look them up, never
invent an unkeyed one.

## Shape

    principal --attacker_reaches--> entry_point <--exposes-- component --runs_as--> principal
                                         |                       |
                                     gated_by                  parses
                                         v                       v
                                       gate <--satisfies--   format

`finding --affects--> component|function|entry_point|format` ties a bug to its surface.
`finding|function|component --present_on--> build` carries per-build presence evidence.
`finding --documented_by--> lane` points at the working notes in `hunt/`.

## The one rule that matters

**No reachability claim without provenance.** `attacker_reaches` REQUIRES `verification`; the write
is refused without it. Strongest first:

    device-measured   ran on real hardware and observed it
    vm-measured       ran in a VM / SIP-enabled guest
    profile-read      read from a sandbox profile, launchd plist or ACL
    static-callgraph  derived from disassembly
    doc-inferred      from naming, docs or a prior ledger line
    inferred          weakest; a guess with a reason

Do not launder a weak level into a strong one. A ledger row asserting "app-reachable" is
`doc-inferred`, however confident it sounds.

## Absence of an edge means UNKNOWN, never blocked

To claim something is NOT reachable, write `attacker_blocked` — it requires BOTH `verification` and
`control_test`. Name the artifact or test that proves it. Name-matching censuses (grepping for
entitlement strings, `has*Entitlements` helpers, profile entry names) reliably produce false
negatives: match on CALL SHAPE, follow ungated dispatchers down, settle negatives with a runtime
fact. Two candidates died to that trap and one was nearly reported on a decompile the device
contradicted.

## Two gates, not one

A sandbox `mach-lookup` allow plus a launchd `MachServices` registration proves only that
`bootstrap_look_up` succeeds. Whether the DAEMON accepts the peer is a second, independent gate —
`xpc_connection_get_audit_token` -> entitlement check, or
`xpc_connection_set_peer_code_signing_requirement`. On the b6 map that second gate is unmeasured for
all 374 reachable names, and it is the most likely reason a "reachable" name is useless in practice.
Say which gate you measured.

## present_on, and pre-release regressions

A claim spanning builds is not a subject cell — it is a `present_on` edge whose `evidence` says how
presence was carried: `device-measured` / `vm-measured` / `translation-table` / `byte-identical` /
`normalised-diff` / `inferred` / `absent`, plus `accepted` for table results ("50/50", "28 of 28").
Use `absent` to record a build where the code is NOT present: that is how a beta-only regression is
expressed, and it is load-bearing. **"Present in a beta" never means "unfixed in shipping"** —
always diff the real shipping build.

## Findings

`status`: candidate | confirmed | reported | submitted | shelved | duplicate | fixed | dead.
Record `dead` lanes WITH their verdict — a closed dead end is expensive knowledge and stops the next
agent re-walking it. Keep `target_flag` literal: what was captured, how many bits, on which venue,
or the worked reason none was claimable.
