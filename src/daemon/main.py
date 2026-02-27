from __future__ import annotations

import asyncio
import logging
import subprocess

import uvicorn

from src.daemon.api import create_app
from src.daemon.agent_launcher import AgentLauncher
from src.shared.config import IntercomConfig

logger = logging.getLogger(__name__)


def _detect_tailscale_ip() -> str:
    """Detect this machine's Tailscale IPv4 address."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return ""


async def run_daemon(config: IntercomConfig) -> None:
    logger.info("Starting AI-Intercom Daemon (machine=%s)", config.machine_id)

    token = config.auth.get("token", "")
    app = create_app(machine_id=config.machine_id, token=token)

    launcher_cfg = config.agent_launcher
    launcher = AgentLauncher(
        default_command=launcher_cfg.get("default_command", "claude"),
        default_args=launcher_cfg.get("default_args", ["-p"]),
        allowed_paths=launcher_cfg.get("allowed_paths", []),
        max_duration=launcher_cfg.get("max_mission_duration", 1800),
    )
    app.state.launcher = launcher

    # Build project_paths mapping for agent launching
    projects = config.projects
    if not projects:
        scan_paths = config.discovery.get("scan_paths", [])
        if scan_paths:
            projects = _discover_projects(scan_paths)
    app.state.project_paths = {p["id"]: p.get("path", ".") for p in projects}
    logger.info("Project paths: %s", app.state.project_paths)

    # Register with hub
    hub_url = config.hub.get("url", "")
    if hub_url:
        await _register_with_hub(hub_url, config, token)
        asyncio.create_task(_heartbeat_loop(hub_url, config.machine_id, token))

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=7700, log_level="info")
    )
    await server.serve()


def _discover_projects(scan_paths: list[str]) -> list[dict]:
    """Auto-discover Claude Code projects by looking for .claude/ or CLAUDE.md markers."""
    from pathlib import Path

    projects = []
    seen = set()

    for scan_path in scan_paths:
        base = Path(scan_path).expanduser()
        if not base.is_dir():
            continue

        # Look for CLAUDE.md files (max depth 3)
        for depth_pattern in ["*/CLAUDE.md", "*/*/CLAUDE.md", "*/*/*/CLAUDE.md"]:
            for claude_md in base.glob(depth_pattern):
                project_dir = claude_md.parent
                project_id = project_dir.name.lower().replace(" ", "-")
                if project_id in seen:
                    continue
                seen.add(project_id)
                projects.append({
                    "id": project_id,
                    "description": f"Project at {project_dir}",
                    "capabilities": ["code"],
                    "path": str(project_dir),
                    "agent_command": "claude",
                })

        # Also look for .claude/ directories
        for depth_pattern in ["*/.claude", "*/*/.claude", "*/*/*/.claude"]:
            for claude_dir in base.glob(depth_pattern):
                project_dir = claude_dir.parent
                project_id = project_dir.name.lower().replace(" ", "-")
                if project_id in seen:
                    continue
                seen.add(project_id)
                projects.append({
                    "id": project_id,
                    "description": f"Project at {project_dir}",
                    "capabilities": ["code"],
                    "path": str(project_dir),
                    "agent_command": "claude",
                })

    return projects


async def _register_with_hub(hub_url: str, config: IntercomConfig, token: str) -> None:
    import httpx
    import json
    from src.shared.auth import sign_request

    # Detect Tailscale IP for accurate daemon_url
    tailscale_ip = _detect_tailscale_ip()
    if tailscale_ip:
        logger.info("Detected Tailscale IP: %s", tailscale_ip)

    # Auto-discover projects if none configured
    projects = config.projects
    if not projects:
        scan_paths = config.discovery.get("scan_paths", [])
        if scan_paths:
            projects = _discover_projects(scan_paths)
            logger.info("Auto-discovered %d projects: %s",
                        len(projects), [p["id"] for p in projects])

    body = json.dumps({
        "machine_id": config.machine_id,
        "display_name": config.machine.get("display_name", config.machine_id),
        "tailscale_ip": tailscale_ip,
        "projects": projects,
    }).encode()

    headers = sign_request(body, config.machine_id, token)
    headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{hub_url}/api/register", content=body, headers=headers)
            logger.info("Registered with hub: %s", resp.json())
    except Exception as e:
        logger.warning("Failed to register with hub: %s", e)


async def _heartbeat_loop(hub_url: str, machine_id: str, token: str) -> None:
    import httpx
    import json
    from src.shared.auth import sign_request

    while True:
        await asyncio.sleep(30)
        try:
            body = json.dumps({"machine_id": machine_id}).encode()
            headers = sign_request(body, machine_id, token)
            headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{hub_url}/api/heartbeat", content=body, headers=headers)
        except Exception:
            pass
