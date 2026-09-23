#!/usr/bin/env bash
# One-shot server setup on a Linux host (e.g. the lab box). Installs uv, builds the venv from the
# lockfile, creates the data dir and mints a first token.
#
# It does NOT install the systemd service and does NOT start the server: the "Next steps" block at
# the end only PRINTS those commands. Until you run deploy/install-service.sh (or that block by
# hand) there is no unit, and nothing brings the server back after a reboot. The check is
# `systemctl is-enabled hivemind` — `not-found` means no unit exists, however the process is
# running right now.
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

# A --user token, NOT the legacy --client-id one: a per-project token is pinned to the project whose
# tokens.json holds it, so it 401s on the server root — which is the address printed below and the
# plugin's own default since 1.2.0. Minting the legacy shape here would hand a fresh operator a
# credential that cannot be used with the URL this very script tells them to configure.
# Usernames are ^[a-z0-9][a-z0-9_-]{0,31}$ — lowercase, no dots — so $(id -un) is a guess, not a
# guarantee; set HIVEMIND_FIRST_USER when it is not one.
FIRST_USER="${HIVEMIND_FIRST_USER:-$(id -un)}"
echo "==> Minting a first token for user '$FIRST_USER'…"
uv run --package hivemind-server hivemind-admin mint-token \
    --user "$FIRST_USER" --device "$(hostname -s 2>/dev/null || echo box)" \
  || echo "!! mint failed — re-run: hivemind-admin mint-token --user <you> --device <machine>" >&2

cat <<EOF

==> Next steps — NONE of these has been run; the server is not installed or started yet:

  bash deploy/install-service.sh        # renders the template unit for this user + repo, enables it
  systemctl is-enabled hivemind         # must say 'enabled'; 'not-found' means no unit exists

  # apply the example domain pack (optional):
  uv run --package hivemind-server hivemind-admin --project default apply-pack packs/security-research/schema.json

  # point clients at the SERVER ROOT (the plugin's default since 1.2.0 — every call must then
  # pass project=, reads included, instead of defaulting into whatever the URL named):
  #     http://<lan-or-tailscale-ip>:${HIVEMIND_PORT:-8787}
  # The older project form still works and keeps that defaulting:
  #     http://<lan-or-tailscale-ip>:${HIVEMIND_PORT:-8787}/p/default
EOF
