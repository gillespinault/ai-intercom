import pytest
from src.shared.config import IntercomConfig, load_config


def test_load_config_from_dict():
    cfg = IntercomConfig(
        mode="daemon",
        machine={"id": "test", "display_name": "Test"},
        hub={"url": "http://localhost:7700"},
    )
    assert cfg.mode == "daemon"
    assert cfg.machine["id"] == "test"
    assert cfg.is_daemon is True
    assert cfg.is_hub is False
    assert cfg.machine_id == "test"


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("""
mode: hub
machine:
  id: serverlab
  display_name: ServerLab
hub:
  listen: "0.0.0.0:7700"
""")
    cfg = load_config(str(config_file))
    assert cfg.mode == "hub"
    assert cfg.machine["id"] == "serverlab"
    assert cfg.is_hub is True
    assert cfg.is_daemon is False


def test_config_env_var_override(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yml"
    config_file.write_text("""
mode: hub
machine:
  id: test
telegram:
  bot_token: ""
""")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
    cfg = load_config(str(config_file))
    assert cfg.telegram["bot_token"] == "test-token-123"


def test_config_standalone_mode():
    cfg = IntercomConfig(mode="standalone")
    assert cfg.is_hub is True
    assert cfg.is_daemon is True


def test_config_missing_file():
    cfg = load_config("/nonexistent/path/config.yml")
    assert cfg.mode == "standalone"
    assert cfg.machine_id == "unknown"


def test_config_env_supergroup_id(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yml"
    config_file.write_text("mode: hub\nmachine:\n  id: test\n")
    monkeypatch.setenv("TELEGRAM_SUPERGROUP_ID", "-1001234567890")
    cfg = load_config(str(config_file))
    assert cfg.telegram["supergroup_id"] == -1001234567890
