"""Tests for project discovery and current-project detection."""

import os
from pathlib import Path

from src.cli import _detect_current_project
from src.daemon.main import _discover_projects
from src.shared.config import IntercomConfig


def test_discover_projects_includes_home(tmp_path):
    """Home project is always included, pointing to $HOME."""
    # Create a scan_path with one project
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Test")

    projects = _discover_projects([str(tmp_path)])

    ids = [p["id"] for p in projects]
    assert "home" in ids

    home_proj = next(p for p in projects if p["id"] == "home")
    assert home_proj["path"] == str(Path.home())
    assert "admin" in home_proj["capabilities"]


def test_discover_projects_finds_claude_md(tmp_path):
    """Projects with CLAUDE.md are discovered."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# App")

    projects = _discover_projects([str(tmp_path)])
    ids = [p["id"] for p in projects]
    assert "my-app" in ids


def test_discover_projects_finds_claude_dir(tmp_path):
    """Projects with .claude/ directory are discovered."""
    proj = tmp_path / "another"
    (proj / ".claude").mkdir(parents=True)

    projects = _discover_projects([str(tmp_path)])
    ids = [p["id"] for p in projects]
    assert "another" in ids


def test_detect_current_project_matches_project(tmp_path, monkeypatch):
    """CWD inside a known project returns its ID."""
    proj = tmp_path / "cool-project"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Cool")
    sub = proj / "src" / "lib"
    sub.mkdir(parents=True)

    monkeypatch.chdir(sub)

    config = IntercomConfig(
        discovery={"scan_paths": [str(tmp_path)]},
        projects=[],
    )
    assert _detect_current_project(config) == "cool-project"


def test_detect_current_project_returns_home(tmp_path, monkeypatch):
    """CWD outside all projects returns 'home'."""
    nowhere = tmp_path / "random"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)

    config = IntercomConfig(
        discovery={"scan_paths": [str(tmp_path / "projects")]},
        projects=[],
    )
    assert _detect_current_project(config) == "home"
