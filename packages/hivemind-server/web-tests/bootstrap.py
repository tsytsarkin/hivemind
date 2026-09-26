"""Isolated browser smoke: create tokens, run both sockets, shut down owned subprocess."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hivemind_server.identity import IdentityStore
from hivemind_server.projects_meta import ProjectMeta, save


def free_port():
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="hivemind-console-smoke-") as tmp:
        data = Path(tmp)
        token = IdentityStore(data / "identities.json").mint("nikt", "macbook")
        second = IdentityStore(data / "identities.json").mint("ana", "laptop")
        save(data / "projects" / "nikt.private",
             ProjectMeta(name="nikt.private", visibility="private", owner="nikt"))
        save(data / "projects" / "ana.private",
             ProjectMeta(name="ana.private", visibility="private", owner="ana"))
        mcp_port, ui_port = free_port(), free_port()
        env = os.environ.copy()
        env.update({"HIVEMIND_DATA_DIR": tmp, "HIVEMIND_PORT": str(mcp_port),
                    "HIVEMIND_UI_PORT": str(ui_port), "HIVEMIND_ALLOWED_HOSTS": "*",
                    "HIVEMIND_TEST_TOKEN": token,
                    "HIVEMIND_TEST_SECOND_TOKEN": second,
                    "HIVEMIND_TEST_UI_URL": f"http://127.0.0.1:{ui_port}",
                    "PYTHONPATH": str(ROOT / "src")})
        interpreter = os.environ.get("HIVEMIND_TEST_PYTHON", sys.executable)
        server = subprocess.Popen([interpreter, "-m", "hivemind_server.app"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("server exited before UI startup: " +
                                       server.stderr.read().decode(errors="replace")[-2000:])
                try:
                    with urllib.request.urlopen(env["HIVEMIND_TEST_UI_URL"], timeout=1):
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(.1)
            else:
                raise RuntimeError("UI did not become ready within 10 seconds")
            return subprocess.call(["npx", "playwright", "test", "console.spec.mjs"],
                                   cwd=Path(__file__).parent, env=env)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
