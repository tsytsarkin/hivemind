# Hivemind

*Shared, versioned memory for a fleet of AI agents.*

Multi-agent workflows lose knowledge between sessions and machines. Hivemind is a shared, versioned
knowledge graph + artifact store + tool registry that agents read and write concurrently over MCP —
domain-agnostic, self-hosted, and schema-flexible so it fits any research or ops domain. It runs as
one service for a fleet of agents (local and remote, across a LAN/Tailscale mesh).

> ⚠️ **Work in progress — proof of concept.** This is early, has had light real-world use, and
> almost certainly contains bugs, rough edges, and missing hardening. Treat it as a foundation to
> build on, not a finished product. **Contributions are welcome** — please open issues/PRs upstream,
> or **fork it** and make it your own. No stability or backwards-compatibility guarantees yet.

Nothing about any particular subject is baked into the engine: **every node and edge type is
defined at runtime in the schema.** The engine provides only *mechanics* (typed nodes/edges,
two-axis versioning, content-addressed blobs, provenance, traversal, search, a tool registry, a
live guide). Meaning is data — shipped as a swappable **domain pack** (`packs/`).

## What's here

| Path | What |
|---|---|
| `packages/hivemind-server/` | The server: MCP (streamable HTTP) + REST, SQLite-backed. Python ≥3.11. |
| `packages/hivemind-client/` | The client library + `hivemind` CLI. Python ≥3.9, only dep is `httpx`. |
| `plugin/` | The Claude Code plugin (MCP config + self-updating bootstrap skill). |
| `packs/` | Optional, swappable, **layerable** domain packs (schema + guide). Ships `security-research` and `ios-macos-attack-surface`. See [docs/packs.md](docs/packs.md). |
| `deploy/` | Deploy docs, systemd unit, Litestream backup, bootstrap + relock scripts. |
| `docs/` | Data model, API, guide authoring, security notes. |

## Two versioning axes (core concept)

1. **Revision (supersession):** the same research about the same subject gets updated → a
   `prev_version` chain with a single current head, protected by optimistic concurrency.
2. **Subject-version:** the version of the *described thing* (e.g. an OS build). "X at 26.5" and
   "X at 26.6" are coexisting nodes grouped by a `subject_key`, each with its own revision chain.

## Quickstart

See **`deploy/DEPLOY.md`**. In short — server on the lab box:
```sh
uv sync --package hivemind-server && uv run hivemind-server      # or the pip/venv path in DEPLOY.md
```
Client anywhere (incl. stock Python 3.9):
```sh
pip install -r deploy/requirements-client.txt && pip install --no-deps ./packages/hivemind-client
```

## Domain packs

A *pack* = `schema.json` (node/edge types with generic traits) + optional `guide/*.md`. Packs are
**additive and layerable** — apply several to one project and they compose (a later pack may widen
an earlier type's enum or add fields, and add new types/edges). Re-applying a pack is **idempotent**
(byte-identical types are skipped, no version churn). Apply one:
```sh
hivemind-admin --project default apply-pack packs/security-research/schema.json   # operator, on host
hivemind schema apply packs/ios-macos-attack-surface/schema.json                  # or remote (client)
```
See [docs/packs.md](docs/packs.md) and the [full docs](docs/) (data model, API, security, guides).

## Adding machines

**One server, many clients** — don't run a second server per machine (each has its own database,
so it would be a separate graph). The server listens on `127.0.0.1` by default; set
`HIVEMIND_HOST=0.0.0.0` to serve your LAN/Tailscale. To put a machine on it, mint a token on the
server and install the plugin there — no server or checkout needed on the client:

```sh
hivemind-admin --project default mint-token --client-id laptop   # on the server
claude plugin marketplace add tsytsarkin/hivemind                # on the new machine
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-ip>:8787/p/default --config api_token=hm_…
```
Full walkthrough incl. secure token transfer: **[docs/clients.md](docs/clients.md)**.

## Reproducible dependencies

`uv.lock` is the source of truth; `deploy/requirements-{server,client}.txt` are generated from it
for pure-`pip`/venv installs. Regenerate with `deploy/relock.sh` after editing any `pyproject.toml`.

Licensed under the **Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Fork it freely; please keep the NOTICE attribution pointing back to the original project.
