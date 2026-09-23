#!/usr/bin/env bash
# Restart the server without racing itself. Runs on Linux (the deploy host) and macOS (a dev box).
#
# Failure modes seen in practice, all of them silent:
#   * `pkill -f hivemind_server` (underscore) never matches the real process name (hyphen);
#   * killing and immediately re-launching loses the bind race — the new instance gets
#     "address already in use", exits, and a wrapper process lingers so `pgrep` still says
#     "running" while nothing is listening;
#   * on macOS none of the tooling was there: no `setsid` (so the server never launched at all),
#     no `ss` (so every port check reported 0 and the script cheerfully carried on), and BSD
#     `fuser` rejects both `-k` and the `8787/tcp` syntax.
# So: kill by pattern AND by port, wait for the port to actually free, then verify it is serving —
# at each step using whichever tool this host actually has.
set -uo pipefail

# ── where the checkout is ───────────────────────────────────────────────────────────────────────
# The deploy host keeps it at ~/hivemind; a dev box has it wherever it was cloned. Derive the root
# from this script's own location — on the server that still resolves to ~/hivemind, so nothing
# changes there — and let HIVEMIND_HOME override. The symlink walk is so a ~/bin symlink to this
# script still finds the repo; `readlink -f` would be shorter but does not exist on macOS.
if [ -n "${HIVEMIND_HOME:-}" ]; then
    ROOT=$HIVEMIND_HOME
else
    src=$0
    while [ -L "$src" ]; do
        link=$(readlink "$src")
        case $link in /*) src=$link ;; *) src=$(dirname "$src")/$link ;; esac
    done
    ROOT=$(cd "$(dirname "$src")/.." && pwd)
fi
cd "$ROOT" || { echo "!!! cannot cd to $ROOT" >&2; exit 1; }

if [ ! -f deploy/hivemind.env ]; then
    echo "!!! no deploy/hivemind.env in $ROOT — copy deploy/hivemind.env.example and edit it" >&2
    exit 1
fi
export PATH="$HOME/.local/bin:$PATH"
set -a; . deploy/hivemind.env; set +a

# Take the port and the log path from the env instead of repeating 8787 here: hardcoded, this
# script killed and probed port 8787 no matter what hivemind.env actually told the server to bind.
PORT=${HIVEMIND_PORT:-8787}
LOG=${HIVEMIND_DATA_DIR:-$HOME/hivemind-data}/server.log
mkdir -p "$(dirname "$LOG")"
# How long to wait for the new instance to answer /healthz. A warm start answers in about a second;
# a cold `uv run` that has to resolve the environment first can take considerably longer than 30s,
# hence the knob.
START_TIMEOUT=${HIVEMIND_START_TIMEOUT:-30}

# ── port tools that exist on both platforms ─────────────────────────────────────────────────────
# `ss` is iproute2, absent on macOS. `lsof` ships with macOS but is frequently missing from a
# minimal Linux server. `netstat` is deprecated on Linux but still the last resort. Ask for each in
# turn so neither platform depends on a tool the other one owns.
listeners() {   # -> the listening addr:port, one per line; empty output means nothing holds it
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk -v p="[.:]$PORT\$" 'NR > 1 && $4 ~ p { print $4 }'
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 { print $9 }' | sort -u
    else
        netstat -an 2>/dev/null | awk -v p="[.:]$PORT\$" '$NF == "LISTEN" && $4 ~ p { print $4 }'
    fi
}
# Anchoring on the port ($4 ~ ":8787$") also fixes a quieter bug in the old `grep ':8787'`: it
# matched a peer address, or port 18787, and called that "listening".

port_pids() {   # -> PIDs holding the port. BSD fuser cannot answer this at all, so lsof goes first.
    if command -v lsof >/dev/null 2>&1; then
        lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null
    elif command -v fuser >/dev/null 2>&1; then
        fuser -n tcp "$PORT" 2>/dev/null
    fi
}

# ── stop whatever is running ────────────────────────────────────────────────────────────────────
pkill -f "uv run --package hivemind-server" 2>/dev/null || true
pkill -f hivemind-server                    2>/dev/null || true

# Then by port. `fuser -k` did this with an immediate SIGKILL and only on Linux; signalling the PIDs
# ourselves works on both and gives the server the chance to close its database cleanly first.
pids=$(port_pids)
[ -n "$pids" ] && kill $pids 2>/dev/null

# Wait for the port to actually free rather than sleeping a flat 4s and hoping — that was too long
# on an idle box and, on a loaded one, sometimes not long enough, which is the bind race above.
i=0
while [ "$i" -lt 20 ] && [ -n "$(listeners)" ]; do
    sleep 0.5
    i=$((i + 1))
done
if [ -n "$(listeners)" ]; then
    echo "port $PORT still held after 10s; escalating to SIGKILL"
    pids=$(port_pids)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null
    sleep 1
fi
echo "port $PORT in use before start: $(listeners | wc -l | tr -d ' ')"

# ── pick a launcher that exists ─────────────────────────────────────────────────────────────────
# DEPLOY.md documents two installs — uv (option A) and a plain venv (option B) — but this script
# only ever knew the uv one, so on a venv-only host every restart died with "uv: command not found"
# and left nothing listening, the reason buried in the log while the script still printed its
# healthz line. Prefer uv (it resolves from uv.lock).
if command -v uv >/dev/null 2>&1; then
    LAUNCH='exec uv run --package hivemind-server hivemind-server'
elif [ -x "$ROOT/.venv/bin/hivemind-server" ]; then
    LAUNCH='exec ./.venv/bin/hivemind-server'
else
    echo "!!! no launcher: neither uv nor ./.venv/bin/hivemind-server — see deploy/DEPLOY.md" >&2
    exit 1
fi

# setsid gives the server its own session on Linux. macOS has no setsid; nohup gets us the part
# that actually matters once this script exits — immunity to the SIGHUP of a closing ssh session.
if command -v setsid >/dev/null 2>&1; then
    DETACH=setsid
else
    DETACH=nohup
fi

echo "launching with: ${LAUNCH#exec } (via $DETACH)"
# ROOT travels in the environment rather than interpolated into the -c string, so a checkout path
# containing a space or a quote cannot break the child's `cd`.
HM_ROOT=$ROOT $DETACH bash -c '
    cd "$HM_ROOT" || exit 1
    set -a; . deploy/hivemind.env; set +a
    export PATH="$HOME/.local/bin:$PATH"
    '"$LAUNCH" >"$LOG" 2>&1 &

# ── verify it is actually serving ───────────────────────────────────────────────────────────────
# Poll instead of `sleep 10` then one curl: a healthy start answers in about a second, and a broken
# one should not cost ten seconds to find out about.
health=""
i=0
while [ "$i" -lt "$START_TIMEOUT" ]; do
    health=$(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null) && [ -n "$health" ] && break
    health=""
    sleep 1
    i=$((i + 1))
done

echo "listening: $(listeners | tr '\n' ' ')"
if [ -n "$health" ]; then
    echo "healthz: $health"
else
    echo "healthz: FAILED (no answer in ${i}s)"
fi
echo "--- tail $LOG ---"
tail -5 "$LOG"

# Exit non-zero when the server did not come up, so a caller — or a human reading $? after an ssh
# one-liner — is not told a failed restart succeeded. This is the whole point of the file.
[ -n "$health" ]
