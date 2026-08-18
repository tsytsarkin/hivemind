# Canonical taxonomy — principals and builds

**Decided 2026-08-18.** Two importers seeded overlapping vocabularies concurrently (a b6
attack-surface map and an `atlas-migration` b4 import). The duplicates were reconciled; this
section is the result and is binding for new writes.

## The rule

**Every `principal` and every `build` is a subject-keyed node.** Look it up before creating one:

    graph_get(subject_key="principal:sandboxed-app", subject_version="-")
    graph_get(subject_key="build:24A5418b",          subject_version="-")

Always `subject_version="-"` (these are identities, not per-build cells). If the lookup misses,
create it with that exact `subject_key`. **Never create an unkeyed principal or build** — a node
with no `subject_key` cannot be found or superseded by another agent, which is precisely how the
duplication happened. That was the whole cause; do not repeat it.

## Canonical principals

    principal:sandboxed-app         third-party App-Sandboxed app (iOS or macOS; platform comes
                                    from the entry_point's build, not from a separate principal)
    principal:unsandboxed-app       local unprivileged unsandboxed process
    principal:compromised-renderer  post-RCE browser renderer
    principal:remote-web-content    pre-RCE: attacker-controlled web content
    principal:remote-message        0-click message delivery (attachment / indexing)
    principal:attacker-media        attacker controls BYTES a privileged parser consumes
    principal:nearby-wireless       RF / AWDL / BT proximity
    principal:unauth-network        unauthenticated network peer (LAN / reachable service)
    principal:usb-physical          USB, physical, attached accessory
    principal:ota-baseband          OTA baseband / RF
    principal:second-stage          already holds io-uc or an equivalent foothold — NOT first stage
    principal:root
    principal:kernel

`nearby-wireless` and `unauth-network` are DIFFERENT positions: proximity/RF versus a routable
network peer. Do not collapse them.

`second-stage` is not a first-stage position. A finding reachable only from there is worth
recording and is not submittable as a first-stage bug — keep that honest.

## Canonical builds

`build:<BuildID>` — e.g. `build:24A5418b`, `build:24A5390f`, `build:23G71`, `build:26A5388g`.
Carry `os`, `marketing`, `role` (`target`|`reference`|`shipping`|`prior`) and `device` where the
device matters. A build id can span devices: `24A5390f` was used on both iPhone17,5 and
iPhone18,5, so do not encode a single device as if it were part of the build's identity.

## Deprecated aliases

Twenty-one unkeyed duplicates survive with `deprecated: true`, a `canonical_subject_key`, and a
`[DEPRECATED ALIAS -> ...]` name prefix. All of their edges were re-pointed to the canonical
nodes. The engine exposes no delete, and its `redirect_to` merge column is not reachable through
any MCP tool — and would not have helped anyway, since `neighbors()` resolves redirects only on
the START node, so redirecting would have orphaned the edges rather than merging them.

If you find another duplicate: bind it with `same_as` (symmetric, non-assertive — a duplicate is
NOT a dispute; do not use `contradicts`), set `props.canonical` to the surviving node_id, re-point
the edges, then mark the loser deprecated. Never silently delete or overwrite another agent's node.

## Concurrency

This project has multiple simultaneous writers. Pass `expected_head` when superseding, and treat a
409 as "re-read and retry", not as an error. Endpoint types ARE enforced: a link whose src/dst type
is outside the edge's declared `src_types`/`dst_types` is refused with `{"ok": false}` in the
envelope — which is NOT a JSON-RPC error, so a client that only checks for `error` will see the
write silently vanish. Check `ok`.
