from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from src.hub.approval import ApprovalEngine, ApprovalLevel
from src.hub.registry import Registry
from src.hub.router import Router
from src.hub.telegram_bot import TelegramBot, parse_start_command
from src.shared.auth import sign_request
from src.shared.config import IntercomConfig
from src.shared.models import Message

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

    # Ensure data directory exists
    Path("data").mkdir(parents=True, exist_ok=True)

    # Initialize components
    registry = Registry("data/registry.db")
    await registry.init()

    # Load policies (check multiple locations)
    import yaml
    policies = {"defaults": {"require_approval": "once"}, "rules": []}
    for policies_path in [
        Path("config/policies.yml"),  # Docker mount
        Path("~/.config/ai-intercom/policies.yml").expanduser(),  # User config
    ]:
        if policies_path.exists():
            with open(policies_path) as f:
                policies = yaml.safe_load(f) or policies
            logger.info("Loaded policies from %s (%d rules)", policies_path, len(policies.get("rules", [])))
            break
    else:
        logger.warning("No policies.yml found, using defaults (require_approval=once)")

    approval = ApprovalEngine(policies)

    # Telegram bot callback handlers

    async def on_human_message(command: str, update, context) -> None:
        """Handle human commands from Telegram."""
        if command == "list_agents":
            agents = await registry.list_agents()
            if not agents:
                text = "No agents registered."
            else:
                lines = [f"- {a['machine_id']}/{a['project_id']} ({a['machine_status']})" for a in agents]
                text = "**Registered agents:**\n" + "\n".join(lines)
            await update.message.reply_text(text)

        elif command == "list_machines":
            machines = await registry.list_machines()
            if not machines:
                text = "No machines registered."
            else:
                lines = [f"- {m['id']} ({m['status']}) - {m['display_name']}" for m in machines]
                text = "**Machines:**\n" + "\n".join(lines)
            await update.message.reply_text(text)

        elif command.startswith("topic_message:"):
            # Human intervention in a mission topic
            parts = command.split(":", 2)
            if len(parts) == 3:
                logger.info("Human message in topic %s: %s", parts[1], parts[2])

    async def on_start_command(text: str, update, context) -> None:
        """Handle /start_agent command from Telegram."""
        try:
            machine, project, mission = parse_start_command(text)
        except ValueError as e:
            await update.message.reply_text(f"Error: {e}")
            return

        import uuid
        msg = Message(
            id=str(uuid.uuid4()),
            from_agent="human",
            to_agent=f"{machine}/{project}",
            type="start_agent",
            payload={"mission": mission or "Start agent"},
            mission_id=str(uuid.uuid4()),
        )
        result = await router.route(msg)
        status = result.get("status", "unknown")
        await update.message.reply_text(f"Agent start: {status}")

    async def on_approval_response(callback_data: str, update, context) -> None:
        """Handle approval inline keyboard responses."""
        # Format: approve:<msg_id>:<level>
        parts = callback_data.split(":")
        if len(parts) != 3 or parts[0] != "approve":
            return
        _, msg_id, level_str = parts

        if level_str == "deny":
            await update.callback_query.edit_message_text("Denied.")
            bot.resolve_approval(msg_id, None)
        else:
            await update.callback_query.edit_message_text(f"Approved ({level_str}).")
            bot.resolve_approval(msg_id, level_str)

    # Telegram bot
    tg_config = config.telegram
    bot = TelegramBot(
        bot_token=tg_config["bot_token"],
        supergroup_id=int(tg_config.get("supergroup_id", 0)),
        allowed_users=[int(u) for u in tg_config.get("security", {}).get("allowed_users", [])],
        on_human_message=on_human_message,
        on_start_command=on_start_command,
        on_approval_response=on_approval_response,
    )

    # Router
    router = Router(
        registry=registry,
        approval_engine=approval,
        send_to_daemon=send_to_daemon,
        send_telegram=bot.post_to_mission,
        request_approval=bot.request_approval,
    )

    # Hub HTTP API
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
