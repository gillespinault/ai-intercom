import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.daemon.upgrade import (
    _detect_install_info,
    _detect_repo,
    _detect_venv,
    save_install_info,
    load_install_info,
    INSTALL_INFO_PATH,
)


def test_detect_install_info_returns_dict():
    info = _detect_install_info()
    assert isinstance(info, dict)
    assert "method" in info
    assert "venv" in info
    assert "repo" in info
    assert "binary" in info


def test_detect_repo_finds_project_root():
    repo = _detect_repo()
    # In test context, we're inside the AI-intercom project
    if repo:
        assert (repo / "pyproject.toml").exists()
        assert (repo / ".git").is_dir()


def test_detect_venv_none_for_none():
    assert _detect_venv(None) is None


def test_detect_venv_with_valid_venv(tmp_path):
    venv_dir = tmp_path / "venv"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr")
    binary = bin_dir / "ai-intercom"
    binary.touch()
    assert _detect_venv(binary) == venv_dir


def test_detect_venv_without_pyvenv_cfg(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "ai-intercom"
    binary.touch()
    assert _detect_venv(binary) is None


def test_save_and_load_install_info(tmp_path, monkeypatch):
    test_path = tmp_path / "install.json"
    monkeypatch.setattr("src.daemon.upgrade.INSTALL_INFO_PATH", test_path)

    info = {"method": "git-editable", "venv": "/tmp/venv", "repo": "/tmp/repo", "binary": "/tmp/venv/bin/ai-intercom"}
    save_install_info(info)

    assert test_path.exists()
    loaded = json.loads(test_path.read_text())
    assert loaded["method"] == "git-editable"
    assert loaded["repo"] == "/tmp/repo"


def test_load_install_info_auto_detects(tmp_path, monkeypatch):
    test_path = tmp_path / "install.json"
    monkeypatch.setattr("src.daemon.upgrade.INSTALL_INFO_PATH", test_path)

    info = load_install_info()
    assert isinstance(info, dict)
    assert "method" in info
    # Should have saved the detected info
    assert test_path.exists()
