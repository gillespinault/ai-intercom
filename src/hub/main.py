from __future__ import annotations

import asyncio
import logging

import httpx

from src.hub.approval import ApprovalEngine
from src.hub.registry import Registry
from src.hub.router import Router
from src.hub.telegram_bot import TelegramBot
from src.shared.auth import sign_request
from src.shared.config import IntercomConfig

logger = logging.getLogger(__name__)


async def send_to_daemon(daemon_url: str, message: dict, token: str) -> dict:
    import json
    body = json.dumps(message).encode()
    headers = sign_request(body, "hub", token)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{daemon_url}/api/message", content=body, headers=headers)
        return resp.json()


async def run_hub(config: IntercomConfig) -> None:
    logger.info("Starting AI-Intercom Hub (mode=%s)", config.mode)

    # Initialize components
    registry = Registry("data/registry.db")
    await registry.init()

    # Load policies
    import yaml
    from pathlib import Path
    policies_path = Path("~/.config/ai-intercom/policies.yml").expanduser()
    if policies_path.exists():
        with open(policies_path) as f:
            policies = yaml.safe_load(f) or {}
    else:
        policies = {"defaults": {"require_approval": "once"}, "rules": []}

    approval = ApprovalEngine(policies)

    # Telegram bot
    tg_config = config.telegram
    bot = TelegramBot(
        bot_token=tg_config["bot_token"],
        supergroup_id=int(tg_config.get("supergroup_id", 0)),
        allowed_users=[int(u) for u in tg_config.get("security", {}).get("allowed_users", [])],
    )

    # Router
    router = Router(
        registry=registry,
        approval_engine=approval,
        send_to_daemon=send_to_daemon,
        send_telegram=bot.post_to_mission,
        request_approval=bot.request_approval,
    )

    # Hub HTTP API (for daemons to register, heartbeat, join)
    from src.hub.hub_api import create_hub_api
    hub_api = create_hub_api(registry, router, config)

    # Run everything
    import uvicorn

    api_task = asyncio.create_task(
        uvicorn.Server(
            uvicorn.Config(hub_api, host="0.0.0.0", port=7700, log_level="info")
        ).serve()
    )

    logger.info("Starting Telegram bot polling...")
    await bot.app.initialize()
    await bot.app.start()
    await bot.app.updater.start_polling()

    try:
        await asyncio.Event().wait()  # Run forever
    finally:
        await bot.app.updater.stop()
        await bot.app.stop()
        await bot.app.shutdown()
        api_task.cancel()
        await registry.close()
