#!/usr/bin/env bash
# Render and install the systemd unit for THIS machine.
#
# The committed unit is a template so the repository carries no hostnames, usernames or absolute
# paths. This fills them in from where it is run.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$REPO/deploy/hivemind.service"
UNIT_OUT="${HIVEMIND_UNIT_OUT:-/etc/systemd/system/hivemind.service}"

[ -x "$REPO/.venv/bin/hivemind-server" ] || {
  echo "no venv yet — run deploy/bootstrap-labbox.sh first" >&2; exit 1; }
[ -f "$REPO/deploy/hivemind.env" ] || cp "$REPO/deploy/hivemind.env.example" "$REPO/deploy/hivemind.env"

rendered="$(sed -e "s|__USER__|$(id -un)|g" -e "s|__REPO__|$REPO|g" "$UNIT_SRC")"
if [ -w "$(dirname "$UNIT_OUT")" ]; then
  printf '%s\n' "$rendered" > "$UNIT_OUT"
else
  printf '%s\n' "$rendered" | sudo tee "$UNIT_OUT" >/dev/null
fi
echo "installed $UNIT_OUT for user $(id -un) at $REPO"
sudo systemctl daemon-reload
sudo systemctl enable --now hivemind
systemctl --no-pager --lines=5 status hivemind || true
