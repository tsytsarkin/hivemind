# Hivemind: user identity, authorship, and project selection

**Date:** 2026-09-22
**Status:** design approved section-by-section; adversarially reviewed; not yet planned
**Target version:** server/client 1.1.0, plugin 1.1.0

## Intent

Four capabilities, requested together because they share one foundation:

1. **Every token is tied to a user identity (username).** For every node or other object a user
   touches, that identity is persisted as an author, so the graph records who did what.
2. **Projects can be created from a Claude session**, over MCP, rather than only by an admin on the
   server.
3. **A project picker in the plugin**, so a user can switch the active project from their session.
4. **Private projects**, reachable only by their owner (and users the owner shares with), for work
   the user does not want shared. On plugin launch the user is asked whether to connect to an
   existing project, create a new one, or create a private one; a private project keyed to the
   session resumes into the same project.

Success looks like: the graph can answer "what did nik write", a private graph exists that other
API users cannot read or even confirm the existence of, and switching projects is one action inside
a running session rather than a config edit and a restart.

The shared foundation is that **identity becomes server-level**. A project-neutral MCP endpoint must
authenticate before it knows which project is meant; private projects need an owner; authorship
needs a trustworthy identity. All three are the same change.

## Decisions

| Decision | Choice | Consequence |
|---|---|---|
| Identity source | Token carries `user` (+ `device` label), server-side | Identity cannot be spoofed by an argument; `agent` demotes to a label |
| Credential scope | **Server-level** `identities.json` | One token works across projects, gated per project by ACL |
| Project routing | **Project-neutral `/mcp`; project resolved per call** | Live switching, and safe when many agents share one token |
| Write safety | **`project` is REQUIRED on write tools**, defaulted on reads | A forgotten argument is a loud error, not a silent misroute |
| Visibility model | `shared` \| `private`, plus a `members` list | One access rule; no ambiguous third enum value |
| Private naming | `<user>.<suffix>`, prefix enforced | No squatting; ownership legible in the name |
| Scratch projects | Private + `session` tag, created lazily, **never deleted** | Always re-openable, as required; they only accumulate |
| Contributor chain | **Computed** from `node_version`, not stored | No duplicated truth, no drift |
| Backfill | Pre-identity writes become `legacy:<agent_id>` | Never invents provenance for a real person |
| Launch prompt | `SessionStart` hook supplies state; the agent asks | A hook cannot render a menu; this is the closest honest thing |

## 1. Identity

Today each project owns `tokens.json` mapping token -> `{client_id, scopes}`, where `client_id` is a
machine label.

**New server-level store** `<data_dir>/identities.json`:

```
token -> { user: "nik", device: "mac-studio", role: "admin"|"member", scopes: [...] }
```

- `user` is the attributed identity. Many tokens map to one user, so "nik from any of his three
  machines" is a single author.
- `device` is retained beside it, so a machine is still distinguishable without becoming the author.
- **Usernames validate as `^[a-z0-9][a-z0-9_-]{0,31}$` — dots are excluded** so that the
  `<user>.` prefix rule in section 3 can never be ambiguous between users `nik` and `nik.x`.
- Same `TokenStore` discipline as today: mtime/size stamp, re-read on change, atomic writes, so a
  token minted by `hivemind-admin` works with no restart and revocation takes effect immediately.

**Identity is derived from the token and cannot be overridden by any argument.** Currently `agent`
is an ordinary tool parameter, so a caller can claim to be anything (`agent="verify-1.0.0"` was
recorded verbatim during 1.0.1 testing). After this it is a free-form *label* — useful for "which
job was this" — and never identity.

**Plumbing** reuses the contextvar pattern proven in 1.0.1 for the bus: the ASGI middleware
resolves the bearer token once per request and publishes `{user, device, role, token_id}`, which
propagates into MCP tool worker threads.

**Legacy compatibility.** A token found only in a project's `tokens.json` still authenticates, with
`user` inferred from its `client_id` and flagged legacy. **A legacy token is scoped to its own
project and grants no access to any other project** — otherwise the move to a project-neutral
endpoint would silently widen every existing credential.

**`tx` gains `user_id` and `device`** alongside `agent_id`, recording trustworthy identity and
self-declared label separately.

`hivemind-admin mint-token --user nik --device mac-studio [--role admin]`.

## 2. Project routing

**Mounts.** One project-neutral MCP app at `/mcp` on the server root. Every existing
`/p/<name>/mcp` mount stays, serving the same app with that project as its default, so no machine
in the fleet needs touching.

**How a tool learns its project.** All 47 tools are annotated `RO` or `WRITE` and wrapped by an
envelope decorator — but there are **four** of them, not one (`_envelope` in `mcp_tools.py`,
`registry_tools.py` and `bus_ws_tools.py`, plus a differently-named `envelope` used 9 times). A
**prerequisite step consolidates them into one shared decorator**, after which the injection lives
in a single place. The decorator sets an explicit `__signature__` appending
`project: Optional[str]`, which is what the SDK reads when generating each tool's schema —
**verified empirically against the installed SDK**: the generated `input_schema` gains a `project`
property, stays out of `required`, and a call carrying `project=` is accepted and routed. At call
time it pops the argument, resolves and ACL-checks the project, and publishes it on a contextvar;
tools read `current_project()` where they currently close over a bound `project`. This is the bulk
of the mechanical work and it is uniform.

**Resolution order:** explicit `project` argument -> the mount's default -> the configured default.
No server-side "current project" exists anywhere.

**Write tools require `project` explicitly (adversarial-review fix).** Resolution-by-default is
safe for reads and *unsafe for writes*: an agent working in `nik.private` that forgets the argument
would have its write silently land in the default shared project — leaking private work, which is
the exact inverse of the feature's purpose, with no error to notice. Because every tool is already
annotated, the decorator requires `project` for `WRITE`-annotated tools and rejects an omission
with an error naming the projects the caller can see. Omission becomes loud; the failure mode
becomes a retry instead of a leak.

**Why not server-side session state.** MCP `2026-07-28` is stateless streamable HTTP and
`.mcp.json` headers are static strings, so no session identifier reaches the server. The only
available key is the token, and tokens are per-machine while many agents run per machine — one
agent switching would move every sibling session's writes. That is a data-integrity bug, and there
is a test for its absence (section 6).

**Drift control.** Every response echoes the resolved `project`, so an agent that drifts sees the
wrong name come back and self-corrects.

**Attach-time bindings that must move to call time.** A single project-neutral MCP app cannot keep
these, each of which is bound once per project today:

- `MCPServer(name=f"hivemind:{project.name}")` -> a single `hivemind` server name.
- `registry_tools.base = f"/p/{project.name}"`, baked into returned URLs -> computed per call.
- `bus_ws_tools`: `hub_for(project.name)` and `register_secret(...)` -> resolved per call; the
  secret is created at project creation or first bus use.

**Blob and index paths are NOT unchanged (adversarial-review fix A11).** See section 3: the ASGI
middleware must apply the project ACL to *every* `/p/<name>/...` path, not only MCP calls.

**Privacy rule:** an unknown project and a forbidden project return the *identical* error.

## 3. Projects: kinds, ownership, visibility

A project is currently a bare directory. Each gains `project.json`:

```
{ name, visibility: "shared"|"private", owner: "nik"|null, members: [],
  label, created, session, last_touched }
```

- **Two-valued visibility plus `members`**, not a three-way enum: `visibility="restricted"` with an
  empty member list would be an inconsistent state that has to be interpreted somewhere.
- **Access is one rule:** `visibility == "shared" or user == owner or user in members`.
- A private project with members displays as "private, shared with N".
- `project.json` is **cached with an mtime/size stamp** like `TokenStore`, because the ACL is
  checked on every call and a per-request file read is not acceptable.
- Existing projects get a `project.json` on first startup; `default` becomes `shared` with
  `owner: null` (a shared project needs no owner).

**Naming.** A private project must be named `<user>.<suffix>`, enforced against the caller. This
prevents squatting, makes ownership legible, and guarantees `nik.private` can only be nik's.

- Per-user private graph: `nik.private`.
- Scratch: `nik.s-<short-session-id>`, **created lazily on first write** so an unused one never
  clutters the picker.

**Lifecycle: no deletion and no reaper.** Scratch projects persist and stay openable indefinitely.
An empty, never-written scratch project is hidden from the picker but remains reachable by name.
Disk cost is one small SQLite database per project; `deploy/backup.sh` already iterates
`projects/*/`, so private and scratch projects are backed up with no change (verified).

**A per-user project cap** (configurable, default 50) bounds `project_create`, so an agent loop
cannot create unbounded databases.

**MCP surface:**

- `project_list()` -> three groups: shared / mine / shared-with-me. Never another user's private
  projects.
- `project_create(name, visibility, label?, schema?, pack?)` -> directory, DB, guide, schema.
  See **Schema bootstrap** below: `schema` is one of `inherit` (default), `interview`, or `bare`.
- `project_share(project, user)` / `project_unshare(project, user)` -> **owner only** (A16).
  Validates that the named user exists (that error may be explicit — it reveals nothing about
  projects). The owner cannot be unshared. Revocation is effective on the next call.
  - **Members cannot re-share.** Sharing is not transitive: a member who could re-share would
    spread the owner's private graph to people the owner never approved, without their knowledge.
  - **`project_share` on a `shared` project is an explicit error**, not a silent no-op — it is
    already readable by everyone, so the call can only mean the caller misunderstood the state.
- `project_info(project)` -> metadata the caller is allowed to see.

#### Schema bootstrap: three modes

A project's node and edge types *are* its meaning, and a project with none cannot be written to at
all. So creation asks how to get them rather than silently picking:

| `schema=` | What happens |
|---|---|
| `inherit` (default) | Copy the active node/edge types of the project the call came from. Right for a private offshoot of work already underway — it arrives speaking your vocabulary. |
| `interview` | The project is created with no types, and the response instructs the agent to load the **schema-authoring skill**, which interviews the user about their work and applies a schema built from the answers. |
| `bare` | No types and no interview. The agent defines types as it goes with `schema_propose`. For a scratch project, or a user who would rather not be asked. |

`pack=<name>` remains available to apply a shipped pack from `packs/` instead.

**The schema-authoring skill** is a new plugin skill, loaded when a project has no schema. Its job
is to stop an agent inventing a vocabulary before it understands the work: near-duplicate type
sprawl is the dominant long-term failure mode of a graph like this, and it is far cheaper to prevent
at creation than to clean up afterwards. It teaches:

- **Interview before proposing.** What does this work track? Which of those things have versions *of
  the thing itself* (the subject axis — an OS build, a firmware release, a package version) as
  opposed to being corrected over time (the revision axis)? Which relationships matter, and which
  are curated by hand versus bulk-imported from a tool? What counts as a disagreement worth
  surfacing?
- **How the answers map onto the engine's generics** — node types; edge types carrying the traits
  `versioned`/bulk, `symmetric`, `transitive`, `acyclic`, `assertive`; and `subject_key` /
  `subject_version` for the subject axis.
- **Keep it small.** Five to eight node types to start. Additive-only means a missing type is cheap
  to add later, while a redundant one is permanent and quietly splits the graph in two.
- **Propose, show the user, then apply** — and record the rationale as a node in the new project, so
  a future agent can read why the vocabulary looks the way it does.
- **Offer the skip plainly**, so a user who does not want an interview is not trapped in one.

**The root-index leak is closed here.** `GET /` currently lists every project name with no token.
It drops to service identity and health only; project listing moves behind auth. `/healthz` stays
open — it names nothing.

**The ACL is enforced in the ASGI middleware, not only in the tool decorator (A11).** Today the
middleware verifies the bearer token against *that project's* `tokens.json`, which is what
incidentally keeps projects apart. Once identity is server-level that incidental barrier is gone, so
without an explicit check **any authenticated user could fetch `/p/nik.private/blobs/<digest>`** —
the blob REST surface never reaches the tool decorator. The middleware therefore resolves the
identity and applies the same `can_access` rule to every `/p/<name>/...` request, MCP and REST
alike.

Two related oracles close with it:

- The middleware currently answers `404 {"error": "unknown project 'x'"}` for a missing project and
  `401` for a bad token. **Divergent responses are an existence oracle**: a stranger learns
  `nik.private` exists by observing which error comes back. Both become the identical response.
- `/p/<name>/` (the per-project index) is deliberately unauthenticated so a client holding only a
  project URL can probe health. For a **private** project that confirms existence, so the index
  returns the same generic error unless the caller is authorised. `/healthz` stays open because it
  names nothing.

**Private project creation requires a server-level identity (A14).** A legacy token's `user` is
inferred from a `client_id` that may contain dots or uppercase (`mac-studio`, `default-bootstrap`),
which cannot satisfy the `<user>.<suffix>` prefix rule. Legacy tokens may read and write projects
they already reach; creating a private project needs a real minted identity.

**Admin scope (A16).** A server-level `admin` manages identities and tokens, shared projects, and
maintenance commands. It carries **no reach into another user's private project over the API** —
specifically, an admin cannot call `project_share` on a project they do not own. An admin who could
share any project could trivially grant themselves read access, which would make the private tier
decorative; since a second human will hold an identity here, that has to be a real boundary rather
than a stated intention.

**Recovery of an orphaned private project** — owner loses their token, or leaves — is done by
editing `project.json` on the server. That deliberately requires shell access, which is already the
real trust boundary (below), so recovery is possible without giving admins a standing API back door.
Ownership transfer over the API is out of scope.

**Honest limit on "private".** Private means private *from other API users*. It is not encrypted at
rest: anyone with shell access to the server, or to a backup, can read every project. The API
boundary is real and enforced; the storage boundary is not.

## 4. Authorship

**Source of truth stays `tx`**, which already writes one row per write and now carries `user_id`
and `device` beside the `agent_id` label.

**Denormalized for reads and filters:** `author_user` on `node_version` and `edge_version` (the
identity that wrote that revision) and `created_by` on the `node` identity row. Both indexed.

**The contributor chain needs no new storage.** Every revision persists as its own `node_version`
row, so the author set is a `GROUP BY author_user` over an indexed column. Storing an `authors`
array on the head would duplicate truth and could drift from it. A node five agents refined reports
all five, with creator and last editor distinguished.

**Bulk edges are the one exception (A15).** A `versioned=0` edge type writes into `edge_bulk`, which
has no version row and therefore no `author_user`; those are attributed through `tx` and their
`source_tag` alone. Stated here so "every edge carries an author" is not read as a guarantee.

**Uniform across object types.** Skills, tools, traps, guide proposals and blob uploads already
carry an agent string or a `tx` reference; each records the token-derived user, preserves the label,
and surfaces the author on read. `graph_get` returns `author`, `created_by`, `contributors`, and
per-entry authors in `history`.

**Filtering:** an `author` parameter on `graph_search`, `skill_search`, `trap_search`,
`tool_search`, composable with existing `types` / `props_filter`. The `author_user` index is what
makes "everything nik wrote" cheap rather than a scan.

**Backfill is honest.** Measured on the live server: 124,353 nodes, 380,729 node-version rows and
1.5M transactions, all of which need attributing. Existing writes carry self-declared strings (`agent`, `cli`, `laptop`,
`verify-1.0.0`); they become `legacy:<agent_id>`, never `nik`. A write whose token cannot resolve an
identity records `legacy:unknown` and **proceeds**, so no machine stops working mid-migration.

## 5. Session flow

**A hook cannot prompt.** `SessionStart` runs a script and returns
`hookSpecificOutput.additionalContext` — text, not a menu. So state comes from a hook and the
question comes from the agent.

**`SessionStart` hook** (matcher `startup|clear|compact`, the pattern shipped by other plugins):
reads `~/.hivemind/session-<CLAUDE_CODE_SESSION_ID>.json` and injects either

- **pinned:** "Hivemind project for this session: `nik.private`. Pass `project=nik.private`."
- **unpinned:** the rule plus "before the first Hivemind call, ask which project to use and pin it."

It deliberately **does not contact the server**: listing projects needs the token, and whether
userConfig reaches a hook's environment is unverified. `project_list()` is already authenticated.
This also removes every offline and cache-staleness path, so the hook cannot block a session.

**Why the pin file exists.** The project choice otherwise lives only in conversation context, where
a compaction can drop it — and a dropped choice plus a defaulted write is the leak that section 2
now fails closed on. The hook re-fires on `compact`, so the pin is re-injected whenever context is
rebuilt, and because it is keyed by session id a `--resume` lands back on the same project.

**The launch question**, asked once by the agent when nothing is pinned: existing project / new
shared project / your private graph (`nik.private`) / new scratch (`nik.s-<short-session-id>`).
`CLAUDE_CODE_SESSION_ID` is present in the environment (confirmed).

**Switching:** `/hivemind:project` lists shared / yours / shared-with-you, switches, rewrites the
pin. The pinning helper is a stdlib-only script installed to `$HOME/.hivemind/` by the skill,
exactly like `bus-listen.py`, because a plugin-only machine has no client package and
`${CLAUDE_PLUGIN_ROOT}` is not set in an arbitrary shell (measured in 1.0.1). It *is* expanded in
`hooks.json` commands, so the hook itself can use it.

**Skill changes:** the per-call `project=` rule, the "echoed project is authoritative" habit, and
the fact that writes require it explicitly.

## 6. Migration, failure modes, tests

**Rollout order** (the fleet is live):

1. `deploy/backup.sh` first — it has already recovered lost data once.
2. Consolidate the four envelope decorators into one (prerequisite for the `project` injection).
3. Additive `ADD COLUMN` migrations: `tx.user_id`, `tx.device`, `node_version.author_user`,
   `edge_version.author_user`, `node.created_by`, plus indexes.
4. `project.json` written for existing projects at startup; `identities.json` created with the
   operator as `admin`.
5. Server-level `/mcp` added; all `/p/<name>/mcp` mounts retained -> no client change required.
6. Plugin 1.1.0: hook, `/hivemind:project`, skill rule. `server_url` accepts either the server root
   or a project base.
7. `hivemind-admin backfill-authors --dry-run` then for real: an **explicit command**, never a
   startup side effect, because it rewrites provenance on every version row of a 4.6 GB live
   database.
8. Remove the vestigial server-level `blobs/` directory from `ensure_dirs` (verified unused: 8 KB,
   while the real per-project store is 33 GB).

**Failure modes:**

- Unknown project and forbidden project -> identical error, suggesting only visible projects.
- Write tool without `project` -> explicit error listing visible projects.
- `project_create` on a name you own -> returns it; on another user's -> the unknown/forbidden error.
- Private name without your prefix -> explicit error (`must be named nik.<suffix>`).
- `project_share` with an unknown user -> explicit error.
- Unresolvable identity -> `legacy:unknown`, write proceeds.
- Revocation -> next call fails, error tells the agent to re-pick.
- Per-user project cap reached -> explicit error.

**Tests:**

- **Spoofing:** a call passing `agent="root"` still records the token's user as author.
- **Privacy:** user B gets byte-identical errors for `nik.private` and `does.not.exist`;
  `project_list` omits it; the root index no longer names it (regression test for the leak found
  during design).
- **Concurrency (the reason approach 1 was chosen):** two concurrent calls on the *same token* with
  different `project=` land in different databases.
- **Fail-closed writes:** a `WRITE` tool with no `project` errors and writes nothing anywhere.
- **Legacy scoping:** an old per-project token authenticates for its own project, is attributed
  `legacy:*`, and is refused on every other project.
- **Membership:** granted works; revoked fails on the next call; owner cannot be unshared.
- **Sharing authority (A16):** an admin token cannot share or read another user's private project; a
  *member* of a private project cannot re-share it; `project_share` on a `shared` project errors.
- **Authorship:** the author filter returns exactly the right set; a node five agents revised
  reports all five, with creator and last editor distinguished.
- **Scratch:** created lazily; the same session id resolves to the same project; it stays listed and
  re-openable.
- **Hook robustness:** valid JSON when `~/.hivemind` is missing or unwritable; never blocks.
- **Username validation:** a dotted username is rejected, so prefix matching stays unambiguous.
- **REST ACL (A11):** user B with a valid server-level token gets the generic error for
  `GET /p/nik.private/blobs/<digest>`, for `/p/nik.private/`, and for a nonexistent project —
  **all three byte-identical**. This is the test that would have caught the hole.
- **Attach-time bindings (A13):** a call resolved to project X returns X's REST base URLs and
  reaches X's bus hub, never the default project's.
- **Legacy identity (A14):** a legacy token cannot create a private project, with an actionable
  error.
- **All 106 existing tests still pass** — per-call resolution must not change single-project
  behaviour.

## Adversarial review

Findings from reviewing this design against itself. Those marked **fixed** are already folded into
the sections above.

| # | Finding | Status |
|---|---|---|
| A1 | A forgotten `project` on a write silently lands private work in the shared default project — a leak with no error. The whole safety story rested on LLM diligence. | **fixed**: writes require `project` (section 2) |
| A2 | A legacy per-project token, presented to a project-neutral endpoint, would gain access to every project. | **fixed**: legacy tokens scoped to their own project (section 1) |
| A3 | A bus listen key is a 7-day HMAC and the WS handshake only checks its signature, so a revoked member's listener keeps receiving from a private project's bus until expiry. | **fixed**: embed `user` in the key and re-check the project ACL at handshake; a per-project revocation epoch invalidates outstanding keys |
| A4 | Usernames containing dots make the `<user>.` prefix rule ambiguous (`nik` vs `nik.x`). | **fixed**: username charset excludes dots (section 1) |
| A5 | The ACL is checked per call; reading `project.json` per request is a per-call file read. | **fixed**: mtime/size-stamped cache (section 3) |
| A6 | `project_create` over MCP is unbounded — an agent loop could create databases without limit. | **fixed**: per-user cap (section 3) |
| A7 | "Private" could be read as a confidentiality guarantee it does not provide. | **fixed**: stated explicitly — API-level only, not encrypted at rest (section 3) |
| **A16** | The spec said sharing was "owner **or admin** only" while also claiming admins get no API access to others' private projects. Both cannot hold: an admin could `project_share` a private project to themselves. The private tier was decorative as written. | **fixed**: sharing is owner-only; admin scope bounded to identities/tokens/shared projects/maintenance; orphan recovery via shell only (section 3) |
| A8 | A vestigial server-level `blobs/` dir invites a future code path to write cross-project blobs into it. | **fixed**: removed from `ensure_dirs` (section 6); verified nothing writes there |
| A9 | Would backups cover private and scratch projects? | **verified safe**: `backup.sh` already loops `projects/*/`; no change needed. Restore of a private project should be exercised once. |
| A10 | Sharing a *scratch* project is permitted, so "scratch" must not be read as implying privacy. | accepted: scratch is private-plus-a-session-tag, nothing more; naming in the picker says "private" |
| **A11** | **The blob REST surface never reaches the tool decorator.** Per-project token stores are what currently keep projects apart; server-level identity removes that barrier, so any authenticated user could read a private project's blobs. Divergent 404/401 responses and the unauthenticated `/p/<name>/` index are also existence oracles for private projects. | **fixed**: ACL enforced in the ASGI middleware for every `/p/<name>/...` path; identical responses; private index gated (section 3) |
| A12 | The spec claimed one shared envelope decorator. There are **four**. "Injection in one place" was false as written. | **fixed**: a consolidation step is now an explicit prerequisite (section 2) |
| A13 | Four project bindings happen at attach time (`MCPServer` name, `registry_tools.base`, `hub_for`, `register_secret`) and would silently serve the wrong project under a shared app. | **fixed**: enumerated and moved to call time (section 2) |
| A14 | A legacy `client_id` (`mac-studio`, `default-bootstrap`) cannot satisfy the `<user>.` prefix rule, so private creation on a legacy token would fail confusingly or be coerced. | **fixed**: private creation requires a minted server-level identity (section 3) |
| A15 | `edge_bulk` rows have no version row, so "every edge gets `author_user`" was unachievable as stated. | **fixed**: bulk edges attributed via `tx` + `source_tag` only (section 4) |

**Residual risks, accepted:**

- Fail-closed catches an *omitted* project, not a *wrong* one. An agent that passes
  `project=default` while working privately still leaks. Mitigations are the echoed project and the
  hook-injected pin, both of which make drift visible, but this is not eliminated.
- Session-id stability across `--resume` is assumed from strong evidence (this session's
  `CLAUDE_CODE_SESSION_ID` matches its pre-compaction transcript filename) and must be verified
  explicitly during implementation.
- Access is binary; there is no read-only member tier.
- No at-rest encryption, per A7.

## Out of scope

- Project deletion or archival (scratch projects are explicitly permanent).
- Automatic schema *migration* between projects, or reconciling two projects whose vocabularies
  diverged. The schema-authoring skill bootstraps a project; it does not merge.
- A read-only membership tier.
- Cross-project search or cross-project authorship queries.
- OAuth, SSO, or any identity provider beyond minted tokens.
- Changing the revision/subject-version axes, the blob store, or the bus protocol beyond A3.
