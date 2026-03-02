"""Self-upgrade mechanism for AI-Intercom daemons."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_INFO_PATH = Path("~/.config/ai-intercom/install.json").expanduser()


def _detect_install_info() -> dict:
    """Detect how ai-intercom was installed on this machine."""
    info: dict = {"method": "unknown", "venv": "", "repo": "", "binary": ""}

    # Find the ai-intercom binary
    binary = _find_binary()
    if binary:
        info["binary"] = str(binary)

    # Detect venv from binary location
    venv = _detect_venv(binary)
    if venv:
        info["venv"] = str(venv)

    # Detect git repo
    repo = _detect_repo()
    if repo:
        info["repo"] = str(repo)
        info["method"] = "git-editable"
    elif venv:
        info["method"] = "pip"

    return info


def _find_binary() -> Path | None:
    """Find the ai-intercom binary."""
    try:
        result = subprocess.run(
            ["which", "ai-intercom"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _detect_venv(binary: Path | None) -> Path | None:
    """Detect venv from binary location (e.g., /path/venv/bin/ai-intercom -> /path/venv)."""
    if not binary:
        return None
    bin_dir = binary.parent
    venv_dir = bin_dir.parent
    if (venv_dir / "pyvenv.cfg").exists():
        return venv_dir
    return None


def _detect_repo() -> Path | None:
    """Detect git repo if ai-intercom was installed in editable mode."""
    # Check if there's a .git in common locations
    candidates = [
        Path(__file__).resolve().parent.parent.parent,  # src/daemon/upgrade.py -> project root
        Path.home() / "AI-intercom",
        Path.home() / "projects" / "AI-intercom",
    ]
    for candidate in candidates:
        if (candidate / ".git").is_dir() and (candidate / "pyproject.toml").is_file():
            # Verify it's the ai-intercom repo
            try:
                text = (candidate / "pyproject.toml").read_text()
                if 'name = "ai-intercom"' in text:
                    return candidate
            except Exception:
                pass
    return None


def save_install_info(info: dict | None = None) -> Path:
    """Save install metadata to disk."""
    if info is None:
        info = _detect_install_info()
    INSTALL_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_INFO_PATH.write_text(json.dumps(info, indent=2))
    logger.info("Saved install info to %s", INSTALL_INFO_PATH)
    return INSTALL_INFO_PATH


def load_install_info() -> dict:
    """Load install metadata from disk, or detect if not saved."""
    if INSTALL_INFO_PATH.exists():
        try:
            return json.loads(INSTALL_INFO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Auto-detect and save
    info = _detect_install_info()
    save_install_info(info)
    return info


def run_self_upgrade(target_version: str = "") -> dict:
    """Perform self-upgrade: git pull + pip install + restart daemon.

    Returns a dict with status and details.
    """
    info = load_install_info()
    result: dict = {"status": "unknown", "install_info": info, "steps": []}

    if info["method"] == "git-editable" and info["repo"]:
        repo = Path(info["repo"])
        # git pull
        try:
            pull = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            result["steps"].append({
                "action": "git pull",
                "returncode": pull.returncode,
                "stdout": pull.stdout.strip(),
                "stderr": pull.stderr.strip(),
            })
            if pull.returncode != 0:
                result["status"] = "git_pull_failed"
                return result
        except Exception as e:
            result["status"] = "git_pull_error"
            result["error"] = str(e)
            return result

        # pip install
        pip_result = _pip_install(info, repo)
        result["steps"].append(pip_result)
        if pip_result.get("returncode", 1) != 0:
            result["status"] = "pip_install_failed"
            return result

    elif info["method"] == "pip":
        # pip install --upgrade ai-intercom
        pip_result = _pip_upgrade(info, target_version)
        result["steps"].append(pip_result)
        if pip_result.get("returncode", 1) != 0:
            result["status"] = "pip_upgrade_failed"
            return result
    else:
        result["status"] = "unknown_install_method"
        return result

    # Get new version
    try:
        from importlib.metadata import version
        # Force reimport by clearing cache
        import importlib
        importlib.invalidate_caches()
        new_version = version("ai-intercom")
    except Exception:
        new_version = "unknown"
    result["new_version"] = new_version

    # Restart daemon
    restart = _restart_daemon_service()
    result["steps"].append(restart)
    result["status"] = "upgraded" if restart.get("restarted") else "upgraded_restart_pending"

    return result


def _pip_install(info: dict, repo: Path) -> dict:
    """Run pip install in editable mode from repo."""
    pip_bin = "pip"
    if info["venv"]:
        pip_bin = str(Path(info["venv"]) / "bin" / "pip")

    try:
        proc = subprocess.run(
            [pip_bin, "install", "-e", str(repo)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "action": "pip install -e",
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[-500:],
            "stderr": proc.stderr.strip()[-500:],
        }
    except Exception as e:
        return {"action": "pip install -e", "returncode": 1, "error": str(e)}


def _pip_upgrade(info: dict, target_version: str = "") -> dict:
    """Run pip install --upgrade ai-intercom."""
    pip_bin = "pip"
    if info["venv"]:
        pip_bin = str(Path(info["venv"]) / "bin" / "pip")

    package = "ai-intercom"
    if target_version:
        package = f"ai-intercom=={target_version}"

    try:
        proc = subprocess.run(
            [pip_bin, "install", "--upgrade", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "action": f"pip install --upgrade {package}",
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[-500:],
            "stderr": proc.stderr.strip()[-500:],
        }
    except Exception as e:
        return {"action": "pip upgrade", "returncode": 1, "error": str(e)}


def _restart_daemon_service() -> dict:
    """Try to restart the daemon service."""
    # Try systemd system service
    try:
        proc = subprocess.run(
            ["sudo", "systemctl", "restart", "ai-intercom-daemon"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return {"action": "systemctl restart", "restarted": True, "method": "system"}
    except Exception:
        pass

    # Try systemd user service (needs XDG_RUNTIME_DIR for D-Bus access)
    try:
        uid = os.getuid()
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        proc = subprocess.run(
            ["systemctl", "--user", "restart", "ai-intercom-daemon"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if proc.returncode == 0:
            return {"action": "systemctl --user restart", "restarted": True, "method": "user"}
    except Exception:
        pass

    return {
        "action": "restart",
        "restarted": False,
        "message": "Could not auto-restart. Restart manually: systemctl restart ai-intercom-daemon",
    }
