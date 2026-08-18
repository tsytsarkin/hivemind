#!/usr/bin/env bash
# One-shot server setup on a Linux host (e.g. the lab box). Installs uv, builds the venv from the
# lockfile, creates the data dir, mints a first token, and installs the systemd service.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

command -v uv >/dev/null 2>&1 || bash deploy/bootstrap-uv.sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> Building venv from uv.lock (server)…"
uv sync --package hivemind-server            # creates ./.venv exactly per the lock

# env file
[ -f deploy/hivemind.env ] || cp deploy/hivemind.env.example deploy/hivemind.env
set -a; . deploy/hivemind.env; set +a
mkdir -p "${HIVEMIND_DATA_DIR:-$HOME/hivemind-data}"

echo "==> Minting a first token for project 'default'…"
uv run --package hivemind-server hivemind-admin --project default mint-token --client-id labbox || true

cat <<EOF

==> Next steps:
  sudo cp deploy/hivemind.service /etc/systemd/system/hivemind.service
  sudo systemctl daemon-reload && sudo systemctl enable --now hivemind
  systemctl status hivemind

  # apply the example domain pack (optional):
  uv run --package hivemind-server hivemind-admin --project default apply-pack packs/security-research/schema.json

  # point clients at:  http://<lan-or-tailscale-ip>:${HIVEMIND_PORT:-8787}/p/default
EOF
