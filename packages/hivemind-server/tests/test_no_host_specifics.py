"""The repository must carry no host specifics.

This is a forkable project and the repo is pushed to a remote, so an operator's username, home
path, hostname or private mount must never be committed — not in a script, not in a doc, not in a
systemd unit. Deploy files are templates filled in per machine (see deploy/install-service.sh).

Author attribution in NOTICE / pyproject is deliberate and exempt.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXEMPT = {"NOTICE", "packages/hivemind-server/tests/test_no_host_specifics.py"}

# Patterns that would identify a particular machine or account.
PATTERNS = {
    "absolute home path": re.compile(r"/(?:home|Users)/[a-z][a-z0-9_.-]*/", re.I),
    "private IPv4": re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "tailscale CGNAT IP": re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    "ssh user@host": re.compile(r"\b[a-z][a-z0-9_.-]*@(?:\d{1,3}\.){3}\d{1,3}\b"),
    "systemd hardcoded user": re.compile(r"^(?:User|Group)=(?!__)", re.M),
}


# `git ls-files` returns EMPTY outside a git checkout, and this test would then pass having read
# nothing — the same blind-pass that let a phantom command through in test_invoked_commands.py. The
# floor sits far below the current file count (132 tracked at the time of writing) and far above
# zero, so "no host specifics" can only mean the files were actually read.
MIN_TRACKED = 20


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f and f not in EXEMPT]


def test_no_host_specifics_in_tracked_files():
    tracked = _tracked()
    assert len(tracked) >= MIN_TRACKED, (
        f"only {len(tracked)} tracked files to scan, under the floor of {MIN_TRACKED} — this test "
        f"read nothing and its pass means nothing. `git ls-files` in {REPO} returns empty outside "
        f"a git checkout; that is the usual cause.")
    offenders = []
    for rel in tracked:
        p = REPO / rel
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for label, pat in PATTERNS.items():
            for m in pat.finditer(text):
                # placeholders and env expansion are the approved way to say "a path here"
                frag = m.group(0)
                if frag.startswith("${") or "__" in frag or "<" in frag:
                    continue
                offenders.append(f"{rel}: {label}: {frag}")
    assert not offenders, "host specifics committed:\n  " + "\n  ".join(sorted(set(offenders)))


def test_the_patterns_can_actually_see_a_host_specific():
    """The positive control: patterns that match nothing would also report "no host specifics".

    One synthetic line per pattern — invented addresses, never a real machine's — each in the
    shape that has actually been found committed. This file is in EXEMPT, which is what lets
    the samples sit here at all.
    """
    samples = {
        "absolute home path": "cd /Users/someone/hivemind && ./run.sh",
        "private IPv4": "ssh 192.168.42.42 systemctl restart hivemind",
        "tailscale CGNAT IP": "HIVEMIND_URL=http://100.64.42.42:8848",
        "ssh user@host": "scp dump.db someone@10.42.42.42:/srv/hivemind/",
        "systemd hardcoded user": "[Service]\nUser=someone\nGroup=someone",
    }
    assert set(samples) == set(PATTERNS), "a pattern was added without a control sample"
    for label, pat in PATTERNS.items():
        assert pat.search(samples[label]), f"{label} no longer matches its own example"
