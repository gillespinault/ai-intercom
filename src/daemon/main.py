from __future__ import annotations

import asyncio
import logging

import uvicorn

from src.daemon.api import create_app
from src.daemon.agent_launcher import AgentLauncher
from src.shared.config import IntercomConfig

logger = logging.getLogger(__name__)


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

    # Register with hub
    hub_url = config.hub.get("url", "")
    if hub_url:
        await _register_with_hub(hub_url, config, token)
        asyncio.create_task(_heartbeat_loop(hub_url, config.machine_id, token))

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=7700, log_level="info")
    )
    await server.serve()


async def _register_with_hub(hub_url: str, config: IntercomConfig, token: str) -> None:
    import httpx
    import json
    from src.shared.auth import sign_request

    body = json.dumps({
        "machine_id": config.machine_id,
        "display_name": config.machine.get("display_name", config.machine_id),
        "tailscale_ip": "",  # Filled by hub from request
        "projects": config.projects,
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
