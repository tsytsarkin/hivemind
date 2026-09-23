"""`deploy/restart.sh` — stop the server, wait for the port, start it again, prove it is serving.

The script exists because every one of its failure modes is silent: a launcher that was never
installed, a port that has not freed yet, a server that died three lines into startup. So what is
worth pinning is not the happy path but that each of those fails *loudly* — non-zero, with the
reason on stdout — and that it does so identically on Linux and on macOS, which share almost none
of the relevant tooling. `ss`, `setsid` and `fuser -k` are Linux-only; `lsof` is the macOS answer;
the script is supposed to ask for whichever one the host actually has.

Two safety rules shape every test here:

  * `pkill` is stubbed to a no-op on PATH. The real script kills by process name, and a suite that
    did that for real would take down a live server the moment someone ran pytest on the deploy
    host. The kill that *is* platform-specific — finding and signalling whoever holds the port — is
    exercised for real, against an ephemeral port nothing else is using.
  * HOME and PATH point away from the developer's machine, because the script prefers `uv` when it
    is installed. Left alone, these tests would exercise a different branch on your laptop than in
    CI, which is the kind of coverage that is worse than none.
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RESTART = REPO / "deploy" / "restart.sh"

# The script's whole verification step is a curl to /healthz; without curl there is nothing to test.
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="restart.sh verifies via curl")

# Binds the configured port, answers anything with 200, and drops a pidfile so a test can tell one
# instance from the next without depending on lsof being installed.
FAKE_SERVER = """#!@PYTHON@
import os, http.server
port = int(os.environ["HIVEMIND_PORT"])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", port), H)
open(os.path.join(os.environ["HIVEMIND_DATA_DIR"], "server.pid"), "w").write(str(os.getpid()))
print("fake server on %d" % port, flush=True)
srv.serve_forever()
"""

DIES_ON_STARTUP = "#!/bin/sh\necho \"ImportError: no module named 'whatever'\" >&2\nexit 1\n"


def _free_port():
    # Never hand back something like 58787: one test asserts the default port never appears in the
    # output, and a substring match would fail it for the wrong reason roughly one run in a hundred.
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if "8787" not in str(port):
            return port
    raise RuntimeError("could not find a free port unlike the default")


def _gone_within(pid, seconds):
    """True once `pid` no longer exists. A dead-but-unreaped process still answers signal 0, so
    this is a poll for the reap, not a check of whether the kill landed."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _path_without_uv():
    """Drop every PATH entry that provides `uv`, so the launcher takes its venv branch here whether
    or not the machine running the suite happens to have uv installed."""
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "uv").exists()]
    return os.pathsep.join(kept)


class Checkout:
    """A throwaway ~/hivemind: the real script, a config, and a stubbed pkill."""

    def __init__(self, root):
        self.root = root
        self.port = _free_port()
        self.data = root / "data"

        (root / "deploy").mkdir(parents=True)
        shutil.copy(RESTART, root / "deploy" / "restart.sh")
        (root / "deploy" / "hivemind.env").write_text(
            f"HIVEMIND_DATA_DIR={self.data}\nHIVEMIND_HOST=127.0.0.1\nHIVEMIND_PORT={self.port}\n"
        )

        self.stub_bin = root / "stub-bin"
        self.stub_bin.mkdir()
        self._stub("pkill", "#!/bin/sh\nexit 0\n")  # see the module docstring

    def _stub(self, name, body):
        p = self.stub_bin / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    def install_launcher(self, body=FAKE_SERVER):
        exe = self.root / ".venv" / "bin" / "hivemind-server"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(body.replace("@PYTHON@", sys.executable))
        exe.chmod(0o755)
        return exe

    def run(self, start_timeout="20"):
        # 20s is a ceiling, not a cost: a healthy start breaks out of the script's poll loop on the
        # first or second second. Only the deliberately-broken fixtures below wait it out, and they
        # pass a short timeout of their own. A tighter bound here was intermittently flaky — each
        # `lsof` the script runs costs ~0.2s, and that adds up on a loaded machine.
        env = dict(os.environ)
        env.pop("HIVEMIND_PORT", None)
        env.update(
            HOME=str(self.root),          # so $HOME/.local/bin cannot smuggle a uv onto PATH
            HIVEMIND_HOME=str(self.root),
            PATH=f"{self.stub_bin}{os.pathsep}{_path_without_uv()}",
            HIVEMIND_START_TIMEOUT=start_timeout,
        )
        return subprocess.run(["bash", str(self.root / "deploy" / "restart.sh")],
                              capture_output=True, text=True, env=env, timeout=180)

    def pid(self):
        return int((self.data / "server.pid").read_text().strip())

    def stop(self):
        try:
            os.kill(self.pid(), signal.SIGKILL)
        except (OSError, ValueError, FileNotFoundError):
            pass
        # The pidfile alone is not enough to clean up after a *failing* test: a server that came up
        # after the script stopped waiting writes its pidfile too late to be read here, and then
        # outlives the suite holding a port. The checkout path is unique per test, so matching on it
        # cannot touch anything but this fixture's own process.
        subprocess.run(["pkill", "-f", str(self.root)], capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    c = Checkout(tmp_path)
    yield c
    c.stop()


def test_it_parses_under_the_system_shell():
    """macOS ships bash 3.2, so a bashism from a newer release is a real portability failure."""
    r = subprocess.run(["bash", "-n", str(RESTART)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_missing_config_stops_before_touching_anything(checkout):
    """Without hivemind.env the script cannot know the port, so proceeding would kill and probe
    whatever happens to be on the default 8787 — someone else's server, on a shared box."""
    (checkout.root / "deploy" / "hivemind.env").unlink()
    r = checkout.run()
    assert r.returncode == 1, r.stdout
    assert "hivemind.env.example" in r.stderr, r.stderr


def test_no_launcher_is_refused_instead_of_starting_nothing(checkout):
    """DEPLOY.md documents a uv install and a plain-venv install. When neither is present the old
    script ran `uv` anyway, and the "command not found" went to the log while it printed healthz."""
    r = checkout.run()  # no install_launcher() call: neither uv nor .venv/bin exists
    assert r.returncode == 1, r.stdout
    assert "no launcher" in r.stderr and "DEPLOY.md" in r.stderr, r.stderr


def test_it_starts_the_server_and_confirms_it_is_serving(checkout):
    checkout.install_launcher()
    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert '"status":"ok"' in r.stdout, r.stdout
    assert str(checkout.port) in r.stdout, r.stdout


def test_restarting_over_a_live_instance_replaces_it(checkout):
    """The bind race this script was written for: the old instance still holds the port when the
    new one tries to bind, so the new one exits and a wrapper lingers looking healthy."""
    checkout.install_launcher()
    assert checkout.run().returncode == 0
    first = checkout.pid()

    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    second = checkout.pid()
    assert second != first, f"pid {second} unchanged — it never actually restarted"
    # And the one that was holding the port is gone, rather than lingering as a wrapper process
    # that makes pgrep report "running" while nothing is listening. Poll rather than sleep a fixed
    # interval: the old process is reaped by init once its shell exits, and that timing is not ours.
    assert _gone_within(first, 10), f"pid {first} still around; it was supposed to be killed"


def test_a_server_that_dies_on_startup_exits_nonzero_with_the_reason(checkout):
    """The failure this script is supposed to make loud: it used to print `healthz: FAILED` and
    then exit 0, so `ssh box restart.sh && echo deployed` reported a successful deploy."""
    checkout.install_launcher(DIES_ON_STARTUP)
    r = checkout.run(start_timeout="3")
    assert r.returncode != 0, r.stdout
    assert "FAILED" in r.stdout, r.stdout
    # The reason has to reach the operator, not just the log file.
    assert "ImportError" in r.stdout, r.stdout


def test_it_uses_the_port_from_the_config_not_a_hardcoded_8787(checkout):
    """With 8787 baked in, changing HIVEMIND_PORT left the script killing and probing a port the
    server was never going to bind — reporting a dead server as healthy, or vice versa."""
    checkout.install_launcher()
    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert f"port {checkout.port}" in r.stdout, r.stdout
    assert "8787" not in r.stdout, r.stdout


# Emulates the two Linux tools this suite would otherwise never reach. `ss` reports the real state
# of the port in Linux's output format; the decoy rows are the ones the old `grep ':8787'` counted
# as a listener — a peer address that happens to use the port, and a longer port ending in it.
SS_STUB = """#!/bin/bash
echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
if (exec 3<>/dev/tcp/127.0.0.1/@PORT@) 2>/dev/null; then
    echo "LISTEN 0      128            0.0.0.0:@PORT@        0.0.0.0:*"
fi
echo "LISTEN 0      128            0.0.0.0:22    203.0.113.5:@PORT@"
echo "LISTEN 0      128          127.0.0.1:1@PORT@       0.0.0.0:*"
"""

SETSID_STUB = '#!/bin/sh\nexec "$@"\n'


def test_the_linux_only_branches_are_driven_correctly(checkout):
    """macOS never reaches these, and the deploy host runs nothing else. With `ss` and `setsid`
    stubbed, the script has to detach via setsid and read Linux-format output — picking our
    listener out of rows that the old substring match would have miscounted."""
    checkout.install_launcher()
    checkout._stub("ss", SS_STUB.replace("@PORT@", str(checkout.port)))
    checkout._stub("setsid", SETSID_STUB)

    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "(via setsid)" in r.stdout, r.stdout
    assert f"listening: 0.0.0.0:{checkout.port}" in r.stdout, r.stdout
    # Exactly one listener: the peer-address row and the 1<port> row are not ours.
    assert "port %d in use before start: 0" % checkout.port in r.stdout, r.stdout


def test_uv_is_preferred_over_the_venv_when_both_are_available(checkout):
    """Option A resolves from uv.lock, so it wins when it is installed. The stub uv refuses to run,
    which is enough: what is pinned here is the choice, not the outcome."""
    checkout.install_launcher()
    checkout._stub("uv", "#!/bin/sh\necho 'stub uv declines' >&2\nexit 1\n")
    r = checkout.run(start_timeout="2")
    assert "launching with: uv run" in r.stdout, r.stdout
    assert r.returncode != 0, r.stdout
