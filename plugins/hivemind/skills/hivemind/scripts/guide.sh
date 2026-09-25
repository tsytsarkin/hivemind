#!/usr/bin/env bash
# Fetch a live Hivemind guide section with an ETag cache. NEVER fails the skill:
# on any error it prints the cached copy (or the bundled offline snapshot) and exits 0 — and says
# WHICH error, because "server unreachable" was wrong for most of them: a missing token, a 401 and
# a project nobody pinned all reach this path and all used to blame the network.
#
# HIVEMIND_SERVER_URL is the SERVER (plugin 1.2.0 made the root the configured shape) and /guide is
# mounted only under /p/<project>/, so this script composes the two halves itself: the server from
# the environment, the project from HIVEMIND_PROJECT or — read at call time, so a mid-session
# $hivemind-project switch is picked up — the session pin.
set -u
SECTION="core"
INSTALL_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install-only) INSTALL_ONLY=1; shift ;;
    --section) SECTION="${2:-core}"; shift 2 ;;
    --section=*) SECTION="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
OFFLINE="$HERE/../references/OFFLINE.md"

# Install the agent-runnable scripts at fixed, shell-expandable paths.
#
# bus_connect hands the agent a listener command that runs in a plain shell without PLUGIN_ROOT.
# The command cannot name the plugin directory, and the server cannot know it either.
# Copying the listener to a path built
# only from $HOME makes one fixed string work on every machine. The project-pin helper is here for
# the same reason: the hivemind-project skill and agent invoke it from a plain shell. The Codex
# SessionStart hook reads the bundled helper directly if the skill has not loaded yet.
# Refreshed on each skill load, so both track the installed plugin version. Silent and
# best-effort: this must never fail the skill.
install_script() {           # $1 = file in this directory, $2 = destination path
  [ -f "$HERE/$1" ] || return 0
  cmp -s "$HERE/$1" "$2" 2>/dev/null && return 0
  mkdir -p "$(dirname "$2")" 2>/dev/null &&
    cp "$HERE/$1" "$2" 2>/dev/null &&
    chmod +x "$2" 2>/dev/null
  return 0
}
install_script bus-listen.py "${HIVEMIND_LISTENER:-$HOME/.hivemind/bus-listen.py}"
install_script bus-autojoin.py "$HOME/.hivemind/bus-autojoin.py"
PIN_HELPER="${HIVEMIND_PIN_HELPER:-$HOME/.hivemind/hivemind-project.py}"
install_script hivemind-project.py "$PIN_HELPER"
if [ "$INSTALL_ONLY" -eq 1 ]; then
  exit 0
fi
CACHE_DIR="${HIVEMIND_CACHE_DIR:-$HOME/.cache/hivemind}"
CACHE="$CACHE_DIR/guide-$SECTION.md"
ETAG="$CACHE_DIR/guide-$SECTION.etag"
mkdir -p "$CACHE_DIR" 2>/dev/null

# $1 = why the live fetch produced nothing, in the reader's words. Every caller passes one: the
# reason is the difference between "check the network" and "check your URL", and the guide_get MCP
# tool is unaffected by any of them, so knowing which happened is what tells you to use it.
print_fallback() {
  WHY="${1:-live fetch failed}"
  if [ -f "$CACHE" ]; then
    echo "> (offline: $WHY; showing last cached copy of '$SECTION')"; echo
    cat "$CACHE"
  elif [ -f "$OFFLINE" ]; then
    echo "> (offline: $WHY; showing bundled framework guide)"; echo
    cat "$OFFLINE"
  else
    echo "> (offline: $WHY; no cached guide available — call the guide_get MCP tool instead)"
  fi
  exit 0
}

# Both come from the shell. The plugin's SessionStart hook exports them from the plugin's own
# config when the shell has not, so this is normally set even on a plugin-only machine; it is empty
# when those variables are not set in the Codex process environment.
if [ -z "${HIVEMIND_SERVER_URL:-}" ] || [ -z "${HIVEMIND_TOKEN:-}" ]; then
  print_fallback "no HIVEMIND_SERVER_URL / HIVEMIND_TOKEN in this shell"
fi
command -v curl >/dev/null 2>&1 || print_fallback "curl is not installed"

BASE="${HIVEMIND_SERVER_URL%/}"
case "$BASE" in
  */p/*)
    # A URL that already names a project — the pre-1.2.0 shape, or somebody's deliberate export.
    # Used VERBATIM: HIVEMIND_PROJECT must not silently redirect a URL whose own path named one.
    URL="$BASE/guide/$SECTION" ;;
  *)
    # The server root. HIVEMIND_PROJECT first (the SessionStart hook exports it from the pin), then
    # the pin file itself — read HERE rather than at session start, because that is what follows a
# hivemind-project switch made mid-session; an exported variable is frozen at the event that
    # wrote it.
    PROJECT="${HIVEMIND_PROJECT:-}"
    if [ -z "$PROJECT" ] && [ -f "$PIN_HELPER" ] && command -v python3 >/dev/null 2>&1; then
      PROJECT="$(python3 "$PIN_HELPER" --show 2>/dev/null |
                 sed -n 's/.*"project"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    fi
    # Validated before it becomes a path segment: `..`, a slash or a scheme would build a URL
    # pointing somewhere nobody pinned, and this value arrives from a hand-editable file or from the
    # environment. The server's own rule, verbatim (projects_meta.NAME_RE).
    #
    # LC_ALL=C and grep rather than a `case` glob: measured, a shell bracket range is COLLATED, so
    # `case Default in *[!a-z0-9._-]*)` does not match under a UTF-8 locale — `D` sorts inside a-z —
    # and an uppercase name sailed through to the URL. A C-locale regex is the range it looks like.
    printf '%s' "$PROJECT" | LC_ALL=C grep -q '^[a-z0-9][a-z0-9._-]\{0,63\}$' || PROJECT=""
    # Named as its own cause: "unreachable" would send the reader after a network fault that is not
    # there, and the fix is one command rather than anything to do with the server.
    [ -n "$PROJECT" ] || print_fallback "no project for $BASE — HIVEMIND_PROJECT is unset and no session pin was readable; use the hivemind-project skill"
    URL="$BASE/p/$PROJECT/guide/$SECTION" ;;
esac
INM=""
[ -f "$ETAG" ] && INM="$(cat "$ETAG" 2>/dev/null)"

TMP="$(mktemp 2>/dev/null)" || print_fallback "no writable temporary file"
CODE="$(curl -s -m 8 -o "$TMP" -w '%{http_code}' \
  -H "Authorization: Bearer $HIVEMIND_TOKEN" \
  ${INM:+-H "If-None-Match: $INM"} \
  -D "$CACHE_DIR/.hdr-$SECTION" "$URL" 2>/dev/null)" \
  || { rm -f "$TMP"; print_fallback "no answer from $URL"; }

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
    rm -f "$TMP"
    # The server answered, so this is not unreachability. Every URL built above carries a
    # /p/<project> prefix, so the old "that is the server root" reason is now unreachable and is
    # gone: a 404 here is about the PROJECT, and the server answers the same 404 for one that does
    # not exist, one this token may not see, and one whose mount has not been built yet.
    WHY="$URL answered HTTP $CODE"
    case "$CODE" in
      404) WHY="$WHY — no project answered there: it does not exist, this token cannot see it, or it was created since the server last started (the /p/<name>/ mounts are built at startup)" ;;
    esac
    print_fallback "$WHY" ;;
esac
exit 0
