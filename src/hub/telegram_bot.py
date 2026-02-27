from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.shared.models import Message

logger = logging.getLogger(__name__)


def format_agent_message(from_agent: str, message: str) -> str:
    """Format a message for display in Telegram."""
    if from_agent == "human":
        return f"\U0001f9d1 *Gilles*: {message}"
    return f"\U0001f916 *{from_agent}*: {message}"


def parse_start_command(text: str) -> tuple[str, str, str]:
    """Parse a /start_agent command argument into (machine, project, mission).

    Accepted formats:
        machine/project
        machine/project "mission text"
        machine/project mission text
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty command")

    # Match: machine/project "optional mission"
    match = re.match(r'^(\w[\w-]*)/(\w[\w-]*)(?:\s+"([^"]*)"|\s+(.+))?$', text)
    if not match:
        # Try without mission
        match = re.match(r'^(\w[\w-]*)/(\w[\w-]*)$', text)
        if not match:
            raise ValueError(
                f"Invalid format: {text!r}. Expected: machine/project [\"mission\"]"
            )
        return match.group(1), match.group(2), ""

    machine = match.group(1)
    project = match.group(2)
    mission = match.group(3) or match.group(4) or ""
    return machine, project, mission


def build_approval_keyboard(msg: Message) -> InlineKeyboardMarkup:
    """Build an inline keyboard for approval responses."""
    prefix = f"approve:{msg.id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\u2705 Once", callback_data=f"{prefix}:once"
                ),
                InlineKeyboardButton(
                    "\u2705 This mission", callback_data=f"{prefix}:mission"
                ),
            ],
            [
                InlineKeyboardButton(
                    "\u2705 Always", callback_data=f"{prefix}:always"
                ),
                InlineKeyboardButton(
                    "\u274c Deny", callback_data=f"{prefix}:deny"
                ),
            ],
        ]
    )


class TelegramBot:
    """Telegram bot for AI-Intercom hub.

    Manages forum topics per mission, approval keyboards, and user commands.
    """

    def __init__(
        self,
        bot_token: str,
        supergroup_id: int,
        allowed_users: list[int],
        on_human_message: Any = None,
        on_start_command: Any = None,
        on_approval_response: Any = None,
    ):
        self.supergroup_id = supergroup_id
        self.allowed_users = allowed_users
        self.on_human_message = on_human_message
        self.on_start_command = on_start_command
        self.on_approval_response = on_approval_response

        self.app = Application.builder().token(bot_token).build()
        self._setup_handlers()

        # topic_id cache: mission_id -> telegram topic id
        self._mission_topics: dict[str, int] = {}
        # Pending approval futures: msg_id -> Future[ApprovalLevel | None]
        self._pending_approvals: dict[str, asyncio.Future] = {}

    def _setup_handlers(self) -> None:
        """Register all command and message handlers."""
        self.app.add_handler(CommandHandler("agents", self._cmd_agents))
        self.app.add_handler(CommandHandler("start_agent", self._cmd_start))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("machines", self._cmd_machines))
        self.app.add_handler(CommandHandler("policy", self._cmd_policy))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a Telegram user is allowed to interact with the bot."""
        return user_id in self.allowed_users

    async def post_to_mission(self, msg: Message) -> int:
        """Post a message to the mission's forum topic. Creates topic if needed.

        Returns the topic's message_thread_id.
        """
        bot: Bot = self.app.bot
        topic_id = self._mission_topics.get(msg.mission_id)

        if topic_id is None:
            payload_msg = msg.payload.get("message", "")
            topic_name = f"{msg.to_agent}: {payload_msg[:50]}"
            topic = await bot.create_forum_topic(
                chat_id=self.supergroup_id,
                name=topic_name,
            )
            topic_id = topic.message_thread_id
            self._mission_topics[msg.mission_id] = topic_id

        text = format_agent_message(msg.from_agent, msg.payload.get("message", ""))
        await bot.send_message(
            chat_id=self.supergroup_id,
            message_thread_id=topic_id,
            text=text,
            parse_mode="Markdown",
        )
        return topic_id

    async def request_approval(self, msg: Message, timeout: int = 300) -> str | None:
        """Send an approval request and wait for human response.

        Returns the approval level string ('once', 'mission', 'always') or None if denied/timeout.
        """
        bot: Bot = self.app.bot
        text = (
            f"\U0001f514 *Approval Required*\n\n"
            f"*From:* {msg.from_agent}\n"
            f"*To:* {msg.to_agent}\n"
            f"*Type:* {msg.type}\n\n"
            f"{msg.payload.get('message', '')[:500]}"
        )
        keyboard = build_approval_keyboard(msg)

        # Create a future that the callback handler will resolve
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_approvals[msg.id] = future

        await bot.send_message(
            chat_id=self.supergroup_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("Approval timeout for message %s", msg.id)
            return None
        finally:
            self._pending_approvals.pop(msg.id, None)

    def resolve_approval(self, msg_id: str, level: str | None) -> None:
        """Resolve a pending approval future (called by callback handler)."""
        future = self._pending_approvals.get(msg_id)
        if future and not future.done():
            future.set_result(level)
        elif not future:
            logger.warning("No pending approval for message %s", msg_id)

    async def get_thread_history(
        self, mission_id: str, limit: int = 20
    ) -> list[dict]:
        """Get message history from a mission topic.

        Note: Telegram Bot API doesn't support reading topic history directly.
        This is populated via mission_store in the hub main loop.
        """
        return []  # Implemented via mission_store in hub main

    # Command handlers

    async def _cmd_agents(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /agents command - list connected agents."""
        if not self._is_authorized(update.effective_user.id):
            return
        if self.on_human_message:
            await self.on_human_message("list_agents", update, context)

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start_agent command - start a remote agent."""
        if not self._is_authorized(update.effective_user.id):
            return
        if self.on_start_command:
            text = " ".join(context.args) if context.args else ""
            await self.on_start_command(text, update, context)

    async def _cmd_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop command - stop a remote agent."""
        if not self._is_authorized(update.effective_user.id):
            return
        if self.on_human_message:
            text = " ".join(context.args) if context.args else ""
            await self.on_human_message(f"stop:{text}", update, context)

    async def _cmd_machines(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /machines command - list registered machines."""
        if not self._is_authorized(update.effective_user.id):
            return
        if self.on_human_message:
            await self.on_human_message("list_machines", update, context)

    async def _cmd_policy(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /policy command - view or modify approval policies."""
        if not self._is_authorized(update.effective_user.id):
            return
        if self.on_human_message:
            text = " ".join(context.args) if context.args else "list"
            await self.on_human_message(f"policy:{text}", update, context)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline keyboard button presses (approval responses)."""
        query = update.callback_query
        if not self._is_authorized(query.from_user.id):
            await query.answer("Unauthorized")
            return

        await query.answer()
        if self.on_approval_response:
            await self.on_approval_response(query.data, update, context)

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle plain text messages in forum topics (human intervention)."""
        if not self._is_authorized(update.effective_user.id):
            return
        if update.message and update.message.message_thread_id:
            # Message in a topic = human intervention in a mission
            if self.on_human_message:
                await self.on_human_message(
                    f"topic_message:{update.message.message_thread_id}:{update.message.text}",
                    update,
                    context,
                )
