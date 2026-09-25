"""Codex port smoke tests, including two independent clients on a live loopback bus."""
import asyncio
import json
import importlib.util
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from hivemind.client import Client, HivemindError
from hivemind_server.identity import IdentityStore


PLUGIN = Path(__file__).resolve().parents[1]
HELPER = PLUGIN / "skills/hivemind/scripts/hivemind-project.py"
HOOK = PLUGIN / "hooks/session_start.py"
LISTENER = PLUGIN / "skills/hivemind/scripts/bus-listen.py"
GUIDE = PLUGIN / "skills/hivemind/scripts/guide.sh"
AUTOJOIN = PLUGIN / "skills/hivemind/scripts/bus-autojoin.py"
CLAUDE_PLUGIN = PLUGIN.parents[1] / "plugin"
CLAUDE_AUTOJOIN = CLAUDE_PLUGIN / "skills/hivemind/scripts/bus-autojoin.py"
LAUNCHER = PLUGIN.parents[1] / "scripts/hivemind-codex"
PROTO = "2026-07-28"


def _mcp_call(base, token, name, arguments):
    """Speak the plugin transport directly, not the separate REST client package."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments, "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTO,
            "io.modelcontextprotocol/clientInfo": {"name": "agent-plugin-test", "version": "1.3.0"},
            "io.modelcontextprotocol/clientCapabilities": {}}}}
    result = httpx.post(base + "/mcp", json=request, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTO,
        "Mcp-Method": "tools/call", "Mcp-Name": name}, timeout=5)
    assert result.status_code == 200, (name, result.status_code, result.text[:300])
    if result.headers.get("content-type", "").startswith("text/event-stream"):
        rows = [line[5:].strip() for line in result.text.splitlines() if line.startswith("data:")]
        payload = json.loads(rows[-1])
    else:
        payload = result.json()
    assert "result" in payload, payload
    value = payload["result"].get("structuredContent")
    if value is None:
        value = json.loads(payload["result"]["content"][0]["text"])
    assert value.get("ok") is not False, value
    return value


def _pin(home, session, *args):
    env = dict(os.environ, HOME=str(home), CODEX_THREAD_ID=session)
    return subprocess.run([sys.executable, str(HELPER), *args], env=env,
                          capture_output=True, text=True, check=True)


def _hook(home, session, source="compact"):
    env = dict(os.environ, HOME=str(home))
    return json.loads(subprocess.check_output(
        [sys.executable, str(HOOK)],
        input=json.dumps({"session_id": session, "source": source}).encode(),
        env=env))["hookSpecificOutput"]["additionalContext"]


def test_plugin_metadata_and_session_isolation(tmp_path):
    repo = PLUGIN.parents[1]
    manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["hivemind"]
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    marketplace = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert manifest["name"] == "hivemind"
    assert marketplace["name"] == "personal"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/hivemind"
    assert '[plugins."hivemind@personal"]' in (repo / ".codex/config.toml").read_text()
    assert mcp["url"] == "http://127.0.0.1:8787/mcp"
    assert mcp["bearerTokenEnvVar"] == "HIVEMIND_TOKEN"
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    _pin(tmp_path, "session-a", "--pin", "alice.private", "--label", "project=wrong")
    assert json.loads(_pin(tmp_path, "session-b", "--show").stdout)["project"] is None
    assert "project=alice.private" in _hook(tmp_path, "session-a")
    assert "project=wrong" not in _hook(tmp_path, "session-a")
    assert "no project pinned" in _hook(tmp_path, "session-b")
    no_id = subprocess.run([sys.executable, str(HELPER), "--pin", "default"],
                           env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True)
    assert no_id.returncode != 0 and "no Codex session id" in no_id.stdout


def test_setup_configures_server_and_stores_token_privately(tmp_path, monkeypatch, capsys):
    # A hyphenated executable has no import suffix; load its source without touching real config.
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_loader("hivemind_codex_setup",
                                        SourceFileLoader("hivemind_codex_setup", str(LAUNCHER)))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    private = tmp_path / "private"
    manifest = tmp_path / ".mcp.json"
    manifest.write_text((PLUGIN / ".mcp.json").read_text())
    monkeypatch.setattr(setup, "STATE", private)
    monkeypatch.setattr(setup, "ADDRESS", private / "codex-server.json")
    monkeypatch.setattr(setup, "TOKEN", private / "codex-token")
    monkeypatch.setattr(setup, "MCP", manifest)
    monkeypatch.setattr("builtins.input", lambda prompt: "http://127.0.0.1:18789")
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "private-test-token")
    assert setup.configure() == 0
    assert manifest.exists()
    assert json.loads(manifest.read_text())["mcpServers"]["hivemind"]["url"] == \
        "http://127.0.0.1:18789/mcp"
    assert setup.TOKEN.stat().st_mode & 0o077 == 0
    assert setup.TOKEN.read_text().strip() == "private-test-token"
    assert "private-test-token" not in capsys.readouterr().out
    called = {}
    monkeypatch.setattr(setup.os, "execvpe", lambda exe, argv, env: called.update(
        {"exe": exe, "args": argv, "env": env}))
    setup.launch(["exec", "hello"])
    assert called["exe"] == "codex" and called["args"] == ["codex", "exec", "hello"]
    assert called["env"]["HIVEMIND_TOKEN"] == "private-test-token"
    assert called["env"]["HIVEMIND_SERVER_URL"] == "http://127.0.0.1:18789"


def test_hook_inbox_reports_each_message_once_across_rotation(tmp_path):
    spec = importlib.util.spec_from_file_location("bus_autojoin_notice", AUTOJOIN)
    auto = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auto)
    inbox = tmp_path / "inbox-default.jsonl"
    state_path = tmp_path / "listener.json"
    state = {}
    inbox.write_text(json.dumps({"type": "message", "id": "first", "body": "malicious-peer-payload"}) + "\n")
    assert "1 new message(s)" in auto._notice(inbox, state_path, state)
    assert auto._notice(inbox, state_path, state) == ""
    with inbox.open("a") as dest:
        dest.write(json.dumps({"type": "broadcast", "id": "second"}) + "\n")
    assert "1 new message(s)" in auto._notice(inbox, state_path, state)
    inbox.rename(inbox.with_name(inbox.name + ".1"))
    inbox.write_text(json.dumps({"type": "message", "id": "third"}) + "\n")
    assert "1 new message(s)" in auto._notice(inbox, state_path, state)
    assert auto._notice(inbox, state_path, state) == ""
    assert "malicious-peer-payload" not in auto._notice(inbox, state_path, {})


def test_install_only_bootstraps_helpers_without_fetching_guide(tmp_path):
    run = subprocess.run(["bash", str(GUIDE), "--install-only"],
                         env=dict(os.environ, HOME=str(tmp_path),
                                  HIVEMIND_SERVER_URL="http://127.0.0.1:1",
                                  HIVEMIND_TOKEN="should-not-be-used"),
                         capture_output=True, text=True, timeout=5, check=True)
    assert run.stdout == ""
    assert (tmp_path / ".hivemind/bus-listen.py").is_file()
    assert (tmp_path / ".hivemind/hivemind-project.py").is_file()


def test_claude_plugin_registers_autolaunch_and_has_credentials():
    manifest = json.loads((CLAUDE_PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["userConfig"]["api_token"]["sensitive"] is True
    assert "server_url" in manifest["userConfig"]
    hooks = json.loads((CLAUDE_PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    startup = [item["command"] for item in hooks["SessionStart"][0]["hooks"]]
    assert any("session-start" in command for command in startup)
    assert any("bus-autojoin.py" in command and "--mode ensure" in command for command in startup)
    assert "--mode after-pin" in hooks["PostToolUse"][0]["hooks"][0]["command"]
    assert "--mode ensure" in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--mode stop" in hooks["SessionEnd"][0]["hooks"][0]["command"]


def test_autojoin_uses_the_installed_platform_endpoint(monkeypatch):
    for module_name, script in (("codex_endpoint", AUTOJOIN),
                                ("claude_endpoint", CLAUDE_AUTOJOIN)):
        spec = importlib.util.spec_from_file_location(module_name, script)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        if module_name == "codex_endpoint":
            monkeypatch.setenv("HIVEMIND_SERVER_URL", "http://127.0.0.1:8787")
            assert helper._endpoint("codex") == "http://127.0.0.1:8787/mcp"
            monkeypatch.setenv("HIVEMIND_SERVER_URL", "http://127.0.0.1:18888")
            assert helper._endpoint("codex") == ""
        else:
            monkeypatch.delenv("HIVEMIND_SERVER_URL", raising=False)
            monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_SERVER_URL", "http://127.0.0.1:18888")
            assert helper._endpoint("claude") == "http://127.0.0.1:18888/mcp"


def _receive(lines, substring, timeout=7):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            line = lines.get(timeout=max(0.01, until - time.monotonic()))
        except queue.Empty:
            break
        if substring in line:
            return line
    raise AssertionError("listener did not receive expected message: %r" % substring)


def test_standard_mcp_client_initializes_and_lists_tools_on_localhost(tmp_path):
    """Exercise the real MCP client handshake, not only hand-built JSON-RPC requests."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    data = tmp_path / "server"
    data.mkdir()
    token = IdentityStore(data / "identities.json").mint("alice", "mcp-client-test")
    base = "http://127.0.0.1:%d" % port
    server = subprocess.Popen([sys.executable, "-m", "hivemind_server.app"],
                              env=dict(os.environ, HIVEMIND_DATA_DIR=str(data),
                                       HIVEMIND_PORT=str(port), HIVEMIND_PUBLIC_URL=base),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise AssertionError("local Hivemind server exited early")
            try:
                if httpx.get(base + "/healthz", timeout=0.25).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("local server did not start")

        async def verify():
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
                async with streamable_http_client(base + "/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {item.name for item in tools.tools}
                        assert {"project_list", "bus_connect", "bus_send", "graph_search"} <= names
                        listing = await session.call_tool("project_list", {})
                        assert not listing.is_error
                        assert json.loads(listing.content[0].text)["ok"]
        asyncio.run(verify())
    finally:
        server.terminate()
        server.wait(timeout=3)


def test_two_sessions_chat_over_localhost_without_client_install(tmp_path):
    # Bind a free local port and isolate every server file under pytest's temp directory.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    data = tmp_path / "server"
    data.mkdir()
    token = IdentityStore(data / "identities.json").mint("alice", "codex-port-test")
    env = dict(os.environ, HIVEMIND_DATA_DIR=str(data), HIVEMIND_HOST="127.0.0.1",
               HIVEMIND_PORT=str(port), HIVEMIND_PUBLIC_URL="http://127.0.0.1:%d" % port)
    server = subprocess.Popen([sys.executable, "-m", "hivemind_server.app"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    listeners = []
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise AssertionError("local Hivemind server exited early")
            try:
                if httpx.get(base + "/healthz", timeout=0.25).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("local server did not start")

        # Both clients speak to the SAME server via the root /mcp endpoint.
        first = Client(base, token, project="default", agent="codex-session-a", max_retries=0)
        second = Client(base, token, project="default", agent="codex-session-b", max_retries=0)
        try:
            assert first.call("project_list", {"project": "default"})
            _pin(tmp_path, "session-a", "--pin", "default")
            guide_env = dict(env, HOME=str(tmp_path), CODEX_THREAD_ID="session-a",
                             HIVEMIND_SERVER_URL=base, HIVEMIND_TOKEN=token)
            live = subprocess.run(["bash", str(GUIDE), "--section", "core"], env=guide_env,
                                  capture_output=True, text=True, check=True, timeout=12)
            assert "(live: guide" in live.stdout
            assert (tmp_path / ".hivemind/bus-listen.py").exists()
            assert (tmp_path / ".hivemind/hivemind-project.py").exists()
            try:
                Client(base, "invalid-test-token", project="default", max_retries=0).call(
                    "project_list", {"project": "default"})
            except HivemindError as error:
                assert error.kind == "auth"
            else:
                raise AssertionError("unauthorized localhost MCP call succeeded")

            for client, label in ((first, "codex-a"), (second, "codex-b")):
                info = client.call("bus_connect", {"label": label})
                listener = subprocess.Popen([sys.executable, str(LISTENER),
                                             "--url", info["ws_url"],
                                             "--key", info["listen_key"],
                                             "--inbox", str(tmp_path / (label + ".jsonl"))],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, bufsize=1)
                lines = queue.Queue()
                threading.Thread(target=lambda p=listener, q=lines: [q.put(line) for line in p.stdout],
                                 daemon=True).start()
                listeners.append((listener, lines))
                _receive(lines, "connected as " + label)
            assert second.call("bus_peers")["count"] >= 2
            assert first.call("bus_send", {"to": "codex-b", "body": "hello from a"})["delivered"]
            _receive(listeners[1][1], "hello from a")
            assert second.call("bus_send", {"to": "codex-a", "body": "reply from b"})["delivered"]
            _receive(listeners[0][1], "reply from b")
            for label in ("codex-a", "codex-b"):
                inbox = (tmp_path / (label + ".jsonl")).read_text()
                assert "body" in inbox and ("hello from a" in inbox or "reply from b" in inbox)
        finally:
            first._http.close()
            second._http.close()
    finally:
        for process in [item[0] for item in listeners] + [server]:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def test_autojoin_codex_and_claude_fallback_on_localhost(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    data = tmp_path / "server"
    data.mkdir()
    token = IdentityStore(data / "identities.json").mint("alice", "auto-test")
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ, HIVEMIND_DATA_DIR=str(data), HIVEMIND_PORT=str(port),
               HIVEMIND_PUBLIC_URL=base)
    server = subprocess.Popen([sys.executable, "-m", "hivemind_server.app"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Simulate Codex's installed plugin cache, including its actual MCP manifest. No endpoint
    # helper is mocked: the copied script must find the manifest relative to its install root.
    installed = tmp_path / "installed-codex"
    script_dir = installed / "skills/hivemind/scripts"
    script_dir.mkdir(parents=True)
    installed_autojoin = script_dir / "bus-autojoin.py"
    shutil.copy2(AUTOJOIN, installed_autojoin)
    shutil.copy2(LISTENER, script_dir / "bus-listen.py")
    (installed / ".mcp.json").write_text(json.dumps({"mcpServers": {"hivemind": {
        "type": "http", "url": base + "/mcp", "bearerTokenEnvVar": "HIVEMIND_TOKEN"}}}))
    hook_env = dict(env, HOME=str(tmp_path), HIVEMIND_TOKEN=token,
                    HIVEMIND_SERVER_URL=base)
    codex = {"session_id": "codex-thread-1"}
    claude = {"session_id": "claude-thread-2"}

    def hook(platform, event, mode="ensure"):
        script = installed_autojoin if platform == "codex" else CLAUDE_AUTOJOIN
        result = subprocess.run([sys.executable, str(script), "--platform", platform,
                                 "--mode", mode], input=json.dumps(event), env=hook_env,
                                capture_output=True, text=True, check=True, timeout=12)
        return (json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
                if result.stdout.strip() else "")

    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/healthz", timeout=0.25).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("server did not start")
        assert hook("codex", codex) == ""  # no project: never choose or register automatically
        _pin(tmp_path, codex["session_id"], "--pin", "default")
        _pin(tmp_path, claude["session_id"], "--pin", "default")
        assert "joined" in hook("codex", codex)
        assert "joined" in hook("claude", claude)
        assert "joined" not in hook("codex", codex)  # idempotent: no second listener
        for _ in range(60):
            online = {p["peer"] for p in _mcp_call(base, token, "bus_peers", {
                "project": "default"})["peers"] if p["online"]}
            if {"codex-codex-thread-1", "claude-claude-thread-2"} <= online:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("autojoin listeners did not become online")
        assert _mcp_call(base, token, "bus_send", {"project": "default", "to":
            "codex-codex-thread-1", "body": "from claude"})["delivered"]
        assert _mcp_call(base, token, "bus_send", {"project": "default", "to":
            "claude-claude-thread-2", "body": "from codex"})["delivered"]
        for platform, event in (("codex", codex), ("claude", claude)):
            for _ in range(60):
                hint = hook(platform, event, "check")
                if "new message(s)" in hint:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("no saved message for " + platform)
            assert "from codex" not in hint and "from claude" not in hint
            assert hook(platform, event, "check") == ""  # no repeated prompt notifications
        created = _mcp_call(base, token, "project_create", {"name": "alice.private",
                             "visibility": "private", "schema": "bare"})
        assert created.get("ok") is not False
        _pin(tmp_path, codex["session_id"], "--pin", "alice.private")
        assert "project=alice.private" in hook("codex", codex)
        assert "new message(s)" not in hook("codex", codex, "check")
        for _ in range(60):
            other = _mcp_call(base, token, "bus_peers", {"project": "alice.private"})
            if any(peer["peer"] == "codex-codex-thread-1" and peer["online"]
                   for peer in other["peers"]):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("listener did not switch projects")
    finally:
        hook("codex", codex, "stop")
        hook("claude", claude, "stop")
        server.terminate()
        server.wait(timeout=3)
