"""Explicitly configurable independent web UI listener."""

import pytest
import socket

from hivemind_server.config import Config


def test_ui_defaults_and_disabled_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path))
    cfg = Config()
    assert (cfg.ui_enabled, cfg.ui_host, cfg.ui_port) == (True, "127.0.0.1", 8788)
    (tmp_path / "hivemind.toml").write_text("[web_ui]\nenabled = false\n")
    assert Config().ui_enabled is False


def test_ui_env_overrides_toml_and_port_cannot_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path))
    (tmp_path / "hivemind.toml").write_text("[web_ui]\nenabled = false\nport = 9000\n")
    monkeypatch.setenv("HIVEMIND_UI_ENABLED", "true")
    monkeypatch.setenv("HIVEMIND_UI_PORT", "8788")
    assert Config().ui_port == 8788 and Config().ui_enabled
    monkeypatch.setenv("HIVEMIND_UI_PORT", "8787")
    with pytest.raises(ValueError, match="port"):
        Config()


def test_ui_invalid_toml_and_non_boolean_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path))
    path = tmp_path / "hivemind.toml"
    path.write_text("[web_ui]\nenabled = 'maybe'\n")
    with pytest.raises(ValueError):
        Config()
    path.write_text("[web_ui\n")
    with pytest.raises(ValueError):
        Config()


def test_ui_listener_config_skips_auth_off_and_disabled(tmp_path, monkeypatch):
    from hivemind_server.app import ui_listener_app
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIVEMIND_REQUIRE_AUTH", "0")
    assert ui_listener_app(Config(), None) is None
    monkeypatch.setenv("HIVEMIND_REQUIRE_AUTH", "1")
    (tmp_path / "hivemind.toml").write_text("[web_ui]\nenabled = false\n")
    assert ui_listener_app(Config(), None) is None


def test_listener_fails_clearly_if_port_already_bound():
    from hivemind_server.app import _open_listener
    with socket.create_server(("127.0.0.1", 0)) as occupied:
        with pytest.raises(OSError):
            _open_listener("127.0.0.1", occupied.getsockname()[1])


def test_release_and_bundled_agent_guides_share_version_1_5_0():
    import json
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    for path in (root / "pyproject.toml", root / "packages/hivemind-server/pyproject.toml",
                 root / "packages/hivemind-client/pyproject.toml"):
        assert tomllib.loads(path.read_text())["project"]["version"] == "1.5.0"
    for path in (root / "plugin/.claude-plugin/plugin.json",
                 root / "plugins/hivemind/.codex-plugin/plugin.json"):
        assert json.loads(path.read_text())["version"].startswith("1.5.0")
    for path in (root / "plugin/skills/hivemind/SKILL.md",
                 root / "plugins/hivemind/skills/hivemind/SKILL.md"):
        guide = path.read_text()
        assert "agent_instruction_inbox" in guide and "agent_capabilities_set" in guide
    client_init = (root / "packages/hivemind-client/src/hivemind/__init__.py").read_text()
    assert '__version__ = "1.5.0"' in client_init
