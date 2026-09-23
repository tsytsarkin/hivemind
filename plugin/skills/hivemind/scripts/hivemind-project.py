#!/usr/bin/env python3
"""Read and write this session's Hivemind project pin. Stdlib only.

The pin exists because the project choice otherwise lives only in conversation context, where a
compaction drops it — and a dropped choice plus a defaulted write is how private work would end up
in the shared graph. Keyed by CLAUDE_CODE_SESSION_ID so a --resume lands back on the same project.

Purely local state: nothing here talks to the server. Listing projects needs the API token, and
`project_list` over MCP is already authenticated, so the session hook that reads this file cannot
fail in a way that delays or blocks a session.

    hivemind-project.py --pin <project> [--label "what this graph is for"]
    hivemind-project.py --show            # {"project": …, "label": …, "pinned_at": …}
    hivemind-project.py --session-id
"""
import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CTRL = re.compile(r"[\x00-\x1f\x7f]")
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
LABEL_MAX = 200


def clean(text):
    """Bound and de-control one stored string.

    Applied on the way in *and* on the way out, so the bound holds for a pin file written by
    another version or edited by hand. The session hook prints these values through json.dumps,
    which escapes anything — but they also reach the model as prose, so a 500-character label with
    an ANSI escape in it is trimmed here rather than injected.
    """
    if not isinstance(text, str):
        return ""
    return CTRL.sub(" ", ANSI.sub("", text))[:LABEL_MAX].strip()


def pin_path():
    """One file per session id.

    The id lands in a filename, so it is slugged rather than merely cleaned: a value holding a
    slash or a `..` would otherwise write outside ~/.hivemind.
    """
    slug = UNSAFE.sub("-", os.environ.get("CLAUDE_CODE_SESSION_ID", ""))[:100] or "no-session"
    return pathlib.Path(os.path.expanduser("~")) / ".hivemind" / ("session-%s.json" % slug)


def main(argv=None):
    ap = argparse.ArgumentParser(description="pin the Hivemind project for this session")
    ap.add_argument("--pin", metavar="PROJECT")
    ap.add_argument("--label", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--session-id", action="store_true")
    args = ap.parse_args(argv)

    if args.session_id:
        print(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        return 0
    path = pin_path()
    if args.pin:
        body = {"project": clean(args.pin), "label": clean(args.label),
                "pinned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2))
        except OSError as e:
            print(json.dumps({"error": str(e)}))
            return 1
        print(json.dumps(body))
        return 0
    stored = {}
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        pass                                  # no pin yet, or a file nobody can parse: not pinned
    if not isinstance(stored, dict):
        stored = {}
    print(json.dumps({"project": clean(stored.get("project")) or None,
                      "label": clean(stored.get("label")),
                      "pinned_at": clean(stored.get("pinned_at"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
