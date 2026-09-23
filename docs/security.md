# Security notes (trusted-team tier)

## Who a caller is

A token names a **person**, not a machine. `hivemind-admin mint-token --user nik --device mac-studio
[--role member|admin]` writes a row into `identities.json` at the data-dir root
(`token -> {user, device, role, scopes}`), and that row is the authority: `db.write()` reads the
caller off a contextvar the auth middleware published, so **no tool argument can claim an author**.
The `agent` argument every tool accepts is a free-form job label and is recorded beside the user,
never instead of it.

Two credential kinds resolve, in this order (`identity.resolve`):

| | Server-level identity | Legacy project token |
|---|---|---|
| Lives in | `<data-dir>/identities.json` (`HIVEMIND_IDENTITIES` overrides) | `<project>/tokens.json` |
| Minted by | `mint-token --user <name>` | `mint-token --client-id <machine>` |
| Author recorded as | `nik` | `legacy:<client_id>` |
| Reaches | every project its user may access | **only** the project whose file holds it |
| On `POST /mcp` and `GET /projects` | works | `401` |
| May create or share a project | yes | no (`project_tools._require_minted`) |

A legacy token is pinned deliberately: `resolve()` is handed exactly one project and consults only
that one, so a token in project A's `tokens.json` does not resolve against B at all, and
`can_access` refuses it for any project other than its own **even a shared one**. Without that,
moving to a project-neutral endpoint would silently widen every credential already deployed.

Both stores are re-read when their file's `(mtime_ns, size)` changes, so minting from
`hivemind-admin` in another process needs no restart and **deleting a row revokes on the next
request** — within one heartbeat (30 s) for an already-open bus socket. Both are written atomically
at mode `0600`. A malformed row is dropped rather than raised, so one operator typo in
`identities.json` cannot lock out everybody else.

## Who may reach a project

`project.json` in the project's own directory is the ACL, and `projects_meta.can_access` is the one
predicate: **shared, or owner, or member** — plus the legacy pin above. Nothing else is consulted.

- **Sharing is owner-only.** `project_share` / `project_unshare` refuse a member and refuse an
  admin. A member cannot re-share, so a grant is non-transitive.
- **`can_access` is blind to `role`.** An admin who could open anyone's private project would make
  the private tier decorative. Measured: `Identity(user="ana", role="admin")` is refused
  `nik.private` exactly as a member is. In fact `Identity.is_admin` has **no call site anywhere in
  the server** — `--role admin` currently grants nothing at all beyond `member`.
- **A dotted project name belongs to the user it names, at *both* visibilities.** Without the
  shared-tier half of that rule anyone could pre-create a *shared* `nik.scratch`, and nik's own
  `project_create(..., visibility="private")` would then resolve onto a world-readable project
  somebody else owns.
- **A private project must be `<user>.<suffix>`**, so ownership is legible in the one string every
  surface reads. Undotted names (`team`, `default`) claim nobody's namespace and stay shared.
- **The bootstrap token cannot open the project it was minted into.** The server mints a legacy
  `<name>-bootstrap` token into every project's `tokens.json` at startup; its user is
  `legacy:<name>-bootstrap`, which is neither the owner nor a member, so `can_access` refuses it for
  a private project. Measured, not assumed.

## Where the ACL is enforced

`app.ProjectAuthMiddleware` resolves the caller and runs `can_access` for **every HTTP request**
under `/p/<name>/`. It is in the middleware rather than in the tool decorator because the REST
surface — blob `GET`/`PUT`, `/blobs/batch`, the guide, the skill and tool catalogs, the project
index — never reaches a tool at all: an ACL in the tool layer would leave
`GET /p/nik.private/blobs/<digest>` open to any authenticated user.
`test_the_blob_surface_is_not_a_bypass` walks every one of those routes, with controls proving each
request reached a handler rather than 404ing at the router.

Two surfaces the middleware cannot cover, each closed where it lives:

- **`POST /mcp`**, the project-neutral endpoint, has no project in the URL, so the ACL moves into
  the call: `envelope.resolve_project` runs `can_access` against the `project=` argument.
- **`WS /p/<name>/bus/ws`** is not an HTTP scope and carries a query-string credential rather than
  a bearer header (the listener is launched by Monitor, which cannot set one). `bus_ws` runs the
  same `can_access` at the handshake **and again on the open socket**, at most one `HEARTBEAT`
  (30 s) apart — a signed key otherwise outlives a revocation. A refused handshake writes nothing
  at all: peers are keyed by *label* while authorization is keyed by *user*, and the label is
  chosen by whoever mints the credential, so any write from the refusal path would land on a peer
  the caller merely named.

## Unknown and forbidden are the same answer

What must never differ is the answer to "this project does not exist" versus "this project is not
yours". Both layers give one answer for both cases, and it is the same sentence in each:

- **HTTP** (`app.PROJECT_DENIED`): `404 {"error":"unknown project or not accessible with this
  token"}`, for a missing name and a forbidden one alike.
  `test_a_private_project_is_indistinguishable_from_a_missing_one` compares **status, body and
  headers** across MCP, a blob `GET`, the index and `healthz`.
- **Tool layer** (`envelope._denied`, `project_tools.DENIED`): an `invalid` envelope opening with
  that same sentence. `resolve_project` raises it both when the registry has no such name and when
  `can_access` says no, so the two are one code path rather than two that happen to agree. It then
  appends *the projects the caller can use* — which is safe precisely because it names nothing the
  caller could not already list.

Two different answers would let a stranger confirm that `nik.private` exists by observing which came
back, and that confirmation is the whole of what the private tier withholds.

Consequences that are easy to get wrong:

- `GET /healthz` at the **server root** answers `{"ok": true}` and nothing else. It used to list
  every project, which was the same oracle.
- A **shared** project's `healthz` and endpoint index answer without a token — a client holds only
  its base URL and a healthy server must not look dead. A **private** project's do not.
- `GET /projects` and the "projects you can use" list appended to a refusal come from the same
  helper (`envelope.visible_projects`), so they cannot drift into two different answers to the same
  question. `project_list` is a third spelling but applies the same `can_access` predicate, which is
  what keeps all three agreeing.
- **Unreadable metadata fails closed.** A missing or unparseable `project.json` reads back
  `private` with no owner, i.e. reachable by nobody — including its real owner. An ill-typed
  `members` field is never coerced (`list("ab") == ["a", "b"]`, and single-character usernames are
  legal), so a hand-corrupted file cannot *grant* access. The reason is logged for the operator,
  once per distinct problem, and never appears in a response — "this project's metadata is corrupt"
  would itself confirm the project exists.

## Integrity and concurrency

- Blobs and tool artifacts are SHA-256 content-addressed and verified on download; tool and skill
  versions are immutable (yank, never delete).
- A digest **prefix** (8+ hex characters) is accepted where a listing shows one, and an unknown,
  ambiguous or truncated digest is an **error** — never an empty result. A `WHERE digest=?` on a
  17-character string is a well-formed query that matches nothing, and it once reported eight
  attached PoCs as orphans.
- Optimistic CAS (`expected_head`) plus a partial unique head index make lost updates impossible,
  not merely unlikely.
- Schema changes are additive-only for agents; destructive ones are operator-only (`apply-pack
  --force`).
- The guide (instructions) is human-gated, propose→merge. The graph (facts) is agent-writable.

## Shared content is untrusted input

Graph props, guide text, skill bodies, trap text and tool code may be written by any other agent
with access to that project. Review tool code before running it: the client verifies its checksum
but does **not** sandbox — run untrusted tools under `sandbox-exec`/`bwrap`. The session-pin hook
is built on the same assumption: it injects the pinned project **name** and deliberately not the
free-text label, because a label rendered before the name could choose the first `project=` value
in the agent's context, while a name that passed `^[a-z0-9][a-z0-9._-]{0,63}$` holds no space and is
simultaneously the payload and the destination.

## The honest limits

- **Private means private from other API users. It is not encrypted at rest.** Anyone with
  filesystem access to the data directory reads every project's SQLite file and blobs directly, and
  `hivemind-admin project-share` deliberately acts *as* the project's owner so an owner who lost
  their token can recover. Host access is total access, by design.
- **No transport security.** Tokens are static bearers over plain HTTP. Bind private interfaces
  only (LAN/Tailscale), never a public NIC; put TLS in front if the path is not already trusted.
  **Being on the LAN is not authorization** — every `/p/<project>` request still needs a token.
- `HIVEMIND_ALLOWED_HOSTS=*` (the default) disables the DNS-rebinding host check. Set explicit
  hostnames to enable it.
- `HIVEMIND_REQUIRE_AUTH=0` is a supported local mode that turns off authentication **and
  therefore the ACL** — by construction rather than by omission: with no credential there is nobody
  to authorize. It is passed explicitly to `envelope.set_registry` and to the bus rather than
  inferred from "is there an identity?", so the open branch can never be reached by an
  authenticated request that simply failed to resolve.
- **`legacy:*` is not a claim about a person.** `legacy:unknown` means "written before authorship
  existed" (a NULL column) or "written by no principal the server could resolve". `:` is illegal in
  a username, so no `legacy:x` value can ever be minted or matched as the user `x` — which matters
  because the labels `backfill-authors` reads are self-declared strings and some of them look like
  usernames.
- `--role admin` is inert today (see above). Do not rely on it as a privilege boundary in either
  direction.
