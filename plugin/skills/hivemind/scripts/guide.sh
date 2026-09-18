#!/usr/bin/env bash
# Fetch a live Hivemind guide section with an ETag cache. NEVER fails the skill:
# on any error it prints the cached copy (or the bundled offline snapshot) and exits 0.
set -u
SECTION="core"
while [ $# -gt 0 ]; do
  case "$1" in
    --section) SECTION="${2:-core}"; shift 2 ;;
    --section=*) SECTION="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
OFFLINE="$HERE/../references/OFFLINE.md"

# Install the bus listener at a fixed, shell-expandable path.
#
# bus_connect hands the agent a Monitor command, and a Monitor command runs in a plain shell where
# — measured — neither CLAUDE_PLUGIN_ROOT nor CLAUDE_SKILL_DIR is set. So the command cannot name
# the plugin directory, and the server cannot know it either. Copying the listener to a path built
# only from $HOME makes one fixed string work on every machine. Refreshed on each skill load, so
# it tracks the installed plugin version. Silent and best-effort: this must never fail the skill.
LISTENER_SRC="$HERE/bus-listen.py"
LISTENER_DST="${HIVEMIND_LISTENER:-$HOME/.hivemind/bus-listen.py}"
if [ -f "$LISTENER_SRC" ]; then
  if ! cmp -s "$LISTENER_SRC" "$LISTENER_DST" 2>/dev/null; then
    mkdir -p "$(dirname "$LISTENER_DST")" 2>/dev/null &&
      cp "$LISTENER_SRC" "$LISTENER_DST" 2>/dev/null &&
      chmod +x "$LISTENER_DST" 2>/dev/null
  fi
fi
CACHE_DIR="${HIVEMIND_CACHE_DIR:-$HOME/.cache/hivemind}"
CACHE="$CACHE_DIR/guide-$SECTION.md"
ETAG="$CACHE_DIR/guide-$SECTION.etag"
mkdir -p "$CACHE_DIR" 2>/dev/null

print_fallback() {
  if [ -f "$CACHE" ]; then
    echo "> (offline: showing last cached copy of '$SECTION')"; echo
    cat "$CACHE"
  elif [ -f "$OFFLINE" ]; then
    echo "> (offline: server unreachable; showing bundled framework guide)"; echo
    cat "$OFFLINE"
  else
    echo "> (offline: no cached guide available; call the guide_get MCP tool instead)"
  fi
  exit 0
}

if [ -z "${HIVEMIND_SERVER_URL:-}" ] || [ -z "${HIVEMIND_TOKEN:-}" ]; then
  print_fallback
fi
command -v curl >/dev/null 2>&1 || print_fallback

URL="${HIVEMIND_SERVER_URL%/}/guide/$SECTION"
INM=""
[ -f "$ETAG" ] && INM="$(cat "$ETAG" 2>/dev/null)"

TMP="$(mktemp 2>/dev/null)" || print_fallback
CODE="$(curl -s -m 8 -o "$TMP" -w '%{http_code}' \
  -H "Authorization: Bearer $HIVEMIND_TOKEN" \
  ${INM:+-H "If-None-Match: $INM"} \
  -D "$CACHE_DIR/.hdr-$SECTION" "$URL" 2>/dev/null)" || { rm -f "$TMP"; print_fallback; }

case "$CODE" in
  200)
    mv "$TMP" "$CACHE"
    NEW_ETAG="$(awk 'tolower($1)=="etag:"{print $2}' "$CACHE_DIR/.hdr-$SECTION" | tr -d "\r")"
    [ -n "$NEW_ETAG" ] && printf '%s' "$NEW_ETAG" > "$ETAG"
    GV="$(awk 'tolower($1)=="x-guide-version:"{print $2}' "$CACHE_DIR/.hdr-$SECTION" | tr -d "\r")"
    echo "> (live: guide '$SECTION' v${GV:-?}, fetched just now)"; echo
    cat "$CACHE" ;;
  304)
    rm -f "$TMP"
    echo "> (live: guide '$SECTION' unchanged; cached copy is current)"; echo
    cat "$CACHE" ;;
  *)
    rm -f "$TMP"; print_fallback ;;
esac
exit 0
