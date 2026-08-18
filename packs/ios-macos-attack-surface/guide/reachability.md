# Attacker reachability

Models WHO the attacker is, WHERE input crosses in, and WHAT PROVES the crossing.
Shared identities (`principal:*`, `build:*`) follow the `identity` section — look them up, never
invent an unkeyed one.

## Shape

    principal --attacker_reaches--> entry_point <--exposes-- component --runs_as--> principal
                                         |                       |
                                     gated_by                  parses
                                         v                       v
                                       gate <--satisfies--   format

`finding --affects--> component|function|entry_point|format` ties a defect to its surface.
`finding|function|component --present_on--> build` carries per-build presence evidence.
`finding --documented_by--> lane` points at the workstream that produced it.

## The one rule that matters

**No reachability claim without provenance.** `attacker_reaches` REQUIRES `verification`; the write
is refused without it. Strongest first:

    device-measured   ran on real hardware and observed it
    vm-measured       ran in a VM or instrumented guest
    profile-read      read from a sandbox profile, service manifest or ACL
    static-callgraph  derived from disassembly
    doc-inferred      from naming, documentation or a prior summary
    inferred          weakest; a guess with a reason

Do not launder a weak level into a strong one. A summary table asserting "app-reachable" is
`doc-inferred`, however confident it sounds.

## Absence of an edge means UNKNOWN, never blocked

To claim something is NOT reachable, write `attacker_blocked` — it requires BOTH `verification` and
`control_test`. Name the artifact or test that proves it.

Name-matching censuses reliably produce false negatives: grepping for entitlement strings, for
`has*Entitlements`-style helper names, or for policy entry names will report "ungated" and
"unreachable" for surfaces that are neither. Match on CALL SHAPE, follow ungated dispatchers down
into what they call, and settle a negative with a runtime fact rather than a decompile.

## Reachability usually has more than one gate

A policy allow plus a service registration proves only that the lookup succeeds. Whether the
service ACCEPTS the peer is a second, independent gate — an audit-token or entitlement check, or a
peer code-signing requirement, evaluated inside the daemon. A map that measures only the first gate
will overstate the surface, often by an order of magnitude. State which gate you measured, and mark
the other `unknown` rather than assuming it open or closed.

## present_on, and pre-release regressions

A claim spanning builds is not a subject cell — it is a `present_on` edge whose `evidence` says how
presence was carried: `device-measured` / `vm-measured` / `translation-table` / `byte-identical` /
`normalised-diff` / `inferred` / `absent`, plus `accepted` for table results ("50/50",
"28 of 28, 0 rejected").

Use `absent` to record a build where the code is NOT present. That is how a pre-release-only
regression is expressed, and the distinction is load-bearing: **"present in a beta" never means
"unfixed in the shipping release"** — diff the real shipping build before claiming a fix or a
regression.

## Findings

`status`: candidate | confirmed | reported | submitted | shelved | duplicate | fixed | dead.
Record `dead` lanes WITH their verdict — a closed dead end is expensive knowledge and stops the next
agent re-walking it. Keep any bounty/flag claim literal: what was captured, how much of it, on which
venue, or the worked reason none was claimable.
