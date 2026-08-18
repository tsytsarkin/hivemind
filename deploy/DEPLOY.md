# Deploying Hivemind

Every dependency is tracked three ways so any machine can reproduce the environment:

- **`uv.lock`** (repo root) — the source of truth, a universal lock across Python 3.9–3.14.
- **`deploy/requirements-server.txt`** — fully pinned server deps (needs Python ≥3.11).
- **`deploy/requirements-client.txt`** — fully pinned client deps (runs on Python ≥3.9).

Both requirements files are generated from `uv.lock` — never edit by hand. Regenerate with
`deploy/relock.sh` after changing any `pyproject.toml`.

## Server (lab box, Python ≥3.11)

### Option A — uv (recommended, uses uv.lock exactly)
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh      # one-time; or deploy/bootstrap-uv.sh
git clone <repo> hivemind && cd hivemind
uv sync --package hivemind-server                     # creates .venv from the lock
uv run hivemind-server                                # or: .venv/bin/hivemind-server
```

### Option B — plain venv + pip (no uv on the box)
```sh
git clone <repo> hivemind && cd hivemind
python3 -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements-server.txt         # pinned transitive deps
pip install --no-deps ./packages/hivemind-server      # the server package itself
hivemind-server
```

## Client (any machine, Python ≥3.9 — incl. the Mac Studio's system 3.9.6)

### Option A — uv
```sh
uv tool install --from ./packages/hivemind-client hivemind   # puts `hivemind` on PATH
```

### Option B — plain venv + pip
```sh
python3 -m venv .venv-hm && . .venv-hm/bin/activate
pip install -r deploy/requirements-client.txt
pip install --no-deps ./packages/hivemind-client
hivemind --help
```

The client's only third-party runtime dependency is `httpx` (plus its small transitive set),
so it installs cleanly on a stock Python 3.9 with nothing preinstalled.
