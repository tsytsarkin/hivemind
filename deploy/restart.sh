#!/usr/bin/env bash
# Restart the server without racing itself.
#
# Two failure modes seen in practice, both silent:
#   * `pkill -f hivemind_server` (underscore) never matches the real process name (hyphen);
#   * killing and immediately re-launching loses the bind race — the new instance gets
#     "address already in use", exits, and a wrapper process lingers so `pgrep` still says
#     "running" while nothing is listening.
# So: kill by pattern AND by port, wait for the port to actually free, then verify it is serving.
set -uo pipefail
cd "$HOME/hivemind"
export PATH="$HOME/.local/bin:$PATH"
set -a; . deploy/hivemind.env; set +a
pkill -f "uv run --package hivemind-server" 2>/dev/null || true
pkill -f hivemind-server 2>/dev/null || true
fuser -k 8787/tcp 2>/dev/null || true
sleep 4
echo "port in use before start: $(ss -ltn | grep -c ':8787')"
setsid bash -c 'cd "$HOME/hivemind"; set -a; . deploy/hivemind.env; set +a; export PATH="$HOME/.local/bin:$PATH"; exec uv run --package hivemind-server hivemind-server' >"$HOME/hivemind-data/server.log" 2>&1 &
sleep 10
echo "listening: $(ss -ltn | grep ':8787' | awk '{print $4}')"
echo -n "healthz: "; curl -s -m 5 http://127.0.0.1:8787/healthz || echo FAILED
echo
tail -3 "$HOME/hivemind-data/server.log"
