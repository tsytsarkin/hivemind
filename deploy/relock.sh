#!/usr/bin/env bash
# Regenerate uv.lock + the pinned requirements files after editing any pyproject.toml.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-uv}"
"$UV" lock
"$UV" export --package hivemind-server --no-hashes --no-dev --no-emit-workspace -o deploy/requirements-server.txt
"$UV" export --package hivemind        --no-hashes --no-dev --no-emit-workspace -o deploy/requirements-client.txt
echo "Relocked: uv.lock + deploy/requirements-{server,client}.txt"
