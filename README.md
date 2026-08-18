# Hivemind

A **domain-agnostic** shared service that lets a fleet of AI agents (local and remote, across a
LAN/Tailscale mesh) read and write a common, versioned knowledge graph, store large artifacts, and
publish/consume standalone tools — concurrently and safely.

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
| `packs/security-research/` | Example domain pack (iOS/macOS RE vocabulary) — optional, swappable. |
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

## Reproducible dependencies

`uv.lock` is the source of truth; `deploy/requirements-{server,client}.txt` are generated from it
for pure-`pip`/venv installs. Regenerate with `deploy/relock.sh` after editing any `pyproject.toml`.

MIT licensed. Authorized-research / internal-team tooling.
