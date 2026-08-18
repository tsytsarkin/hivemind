#!/usr/bin/env bash
# Install uv (a single self-contained binary — no python/node/docker needed) if missing.
# Used to bootstrap a cold machine so it can `uv run` published tools or the server.
set -euo pipefail
UV_VERSION="${UV_VERSION:-0.12.5}"
if command -v uv >/dev/null 2>&1; then
  echo "uv already installed: $(uv --version)"; exit 0
fi
echo "Installing uv ${UV_VERSION}…"
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
echo "Done. Ensure ~/.local/bin is on PATH (or restart your shell)."
