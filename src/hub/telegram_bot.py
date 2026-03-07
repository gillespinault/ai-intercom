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

from src.hub.voice_services import VoiceConfig, transcribe
from src.shared.models import Message

logger = logging.getLogger(__name__)


def _tg_esc(text: str) -> str:
    """Escape Markdown V1 special characters for Telegram."""
    if not text:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))


from src.hub.telegram_helpers import _sanitize_markdown_v1, _split_message  # noqa: F401


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
        on_dispatch: Any = None,
        dashboard_url: str = "",
        voice_config: VoiceConfig | None = None,
    ):
        self.supergroup_id = supergroup_id
        self.allowed_users = allowed_users
        self.on_human_message = on_human_message
        self.on_start_command = on_start_command
        self.on_approval_response = on_approval_response
        self.on_dispatch = on_dispatch
        self.dashboard_url = dashboard_url
        self.voice_config = voice_config

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
        self.app.add_handler(CommandHandler("attention", self._cmd_attention))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
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
        try:
            await bot.send_message(
                chat_id=self.supergroup_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            # Fallback to plain text if Markdown parsing fails
            await bot.send_message(
                chat_id=self.supergroup_id,
                message_thread_id=topic_id,
                text=text,
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

        try:
            await bot.send_message(
                chat_id=self.supergroup_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception:
            await bot.send_message(
                chat_id=self.supergroup_id,
                text=text,
                reply_markup=keyboard,
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

    async def post_text_to_mission(self, mission_id: str, text: str) -> bool:
        """Post raw text to a mission's forum topic. Returns False if no topic exists."""
        topic_id = self._mission_topics.get(mission_id)
        if topic_id is None:
            return False
        bot: Bot = self.app.bot
        try:
            await bot.send_message(
                chat_id=self.supergroup_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(
                    chat_id=self.supergroup_id,
                    message_thread_id=topic_id,
                    text=text,
                )
            except Exception:
                return False
        return True

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

    async def _cmd_attention(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /attention command - link to the PWA Attention Hub dashboard."""
        if not self._is_authorized(update.effective_user.id):
            return
        if not self.dashboard_url:
            await update.message.reply_text("Dashboard URL not configured.")
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("\U0001f4ca Attention Hub", url=self.dashboard_url)]]
        )
        await update.message.reply_text(
            "\U0001f517 *Attention Hub Dashboard*",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def send_attention_notification(self, session) -> None:
        """Send a Telegram notification when an agent needs attention."""
        if not self.dashboard_url:
            return
        bot: Bot = self.app.bot

        # Build a readable project name from the path
        project_short = (session.project or "").rstrip("/").rsplit("/", 1)[-1] or "?"
        session_label = session.session_name or project_short

        # Type emoji
        type_emoji = {
            "permission": "\U0001f527",  # 🔧
            "question": "\u2753",        # ❓
            "text_input": "\u270d\ufe0f", # ✍️
        }

        prompt = session.prompt
        ptype = prompt.type if prompt else "unknown"
        emoji = type_emoji.get(ptype, "\U0001f514")

        # Header: emoji + project@machine
        text = f"{emoji} *{_tg_esc(session_label)}* `@{_tg_esc(session.machine)}`\n"

        # Context snippet depending on prompt type
        if prompt:
            if ptype == "permission":
                tool = prompt.tool or "Tool"
                text += f"Permission: *{_tg_esc(tool)}*\n"
                if prompt.command_preview:
                    preview = prompt.command_preview
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    text += f"`{_tg_esc(preview)}`\n"
            elif ptype == "question":
                question = prompt.question or ""
                if question:
                    if len(question) > 200:
                        question = question[:197] + "..."
                    text += f"{_tg_esc(question)}\n"
                # Show choices inline
                if prompt.choices:
                    labels = [f"[{_tg_esc(c.label)}]" for c in prompt.choices[:5]]
                    text += " ".join(labels) + "\n"
            elif ptype == "text_input":
                if prompt.raw_text:
                    lines = prompt.raw_text.strip().split("\n")
                    tail = lines[-1] if lines else ""
                    if len(tail) > 120:
                        tail = tail[:117] + "..."
                    if tail:
                        text += f"`{_tg_esc(tail)}`\n"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("\U0001f4ca Dashboard", url=self.dashboard_url)]]
        )
        try:
            await bot.send_message(
                chat_id=self.supergroup_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(
                    chat_id=self.supergroup_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning("Failed to send attention notification: %s", e)

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
        """Handle plain text messages: topic messages or natural language dispatch."""
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
            return

        # Message in general chat or DM → intelligent dispatcher
        if self.on_dispatch and update.message and update.message.text:
            await update.message.chat.send_action("typing")
            await self.on_dispatch(update.message.text, update, context)

    async def _handle_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle voice messages: transcribe via STT then dispatch as text."""
        if not self._is_authorized(update.effective_user.id):
            return

        vc = self.voice_config
        if not vc or not vc.enabled or not vc.stt_url:
            await update.message.reply_text(
                "Voice non active. Configurez la section `voice:` dans config.yml."
            )
            return

        await update.message.chat.send_action("typing")

        try:
            voice = update.message.voice
            voice_file = await voice.get_file()
            ogg_bytes = bytes(await voice_file.download_as_bytearray())

            text = await transcribe(ogg_bytes, vc.stt_url, vc.tts_language)
            await update.message.reply_text(f"_{text}_", parse_mode="Markdown")

            if self.on_dispatch:
                await self.on_dispatch(text, update, context)
        except Exception as e:
            logger.exception("Voice transcription failed")
            await update.message.reply_text(
                f"Erreur transcription vocale : {e}"
            )
