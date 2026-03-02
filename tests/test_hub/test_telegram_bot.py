from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.hub.telegram_bot import TelegramBot, format_agent_message, parse_start_command


def test_format_agent_message():
    text = format_agent_message(
        from_agent="serverlab/infra",
        message="Add reverse proxy for newapp",
    )
    assert "serverlab/infra" in text
    assert "Add reverse proxy" in text


def test_format_agent_message_human():
    text = format_agent_message(
        from_agent="human",
        message="Use port 3457",
    )
    assert "human" not in text or "Gilles" in text  # human messages formatted differently


def test_parse_start_command_full():
    machine, project, mission = parse_start_command('vps/nginx "Check SSL certs"')
    assert machine == "vps"
    assert project == "nginx"
    assert mission == "Check SSL certs"


def test_parse_start_command_just_target():
    machine, project, mission = parse_start_command("vps/nginx")
    assert machine == "vps"
    assert project == "nginx"
    assert mission == ""


def test_parse_start_command_invalid():
    with pytest.raises(ValueError):
        parse_start_command("")


# --- /attention command tests ---


class TestCmdAttention:
    @pytest.fixture
    def bot(self):
        with patch("src.hub.telegram_bot.Application") as MockApp:
            mock_app = MagicMock()
            MockApp.builder.return_value.token.return_value.build.return_value = mock_app
            mock_app.add_handler = MagicMock()
            return TelegramBot(
                bot_token="fake-token",
                supergroup_id=-100123,
                allowed_users=[42],
            )

    @pytest.mark.asyncio
    async def test_attention_no_dashboard_url(self, bot):
        """Without dashboard_url, replies with 'not configured'."""
        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await bot._cmd_attention(update, context)

        update.message.reply_text.assert_called_once_with(
            "Dashboard URL not configured."
        )

    @pytest.mark.asyncio
    async def test_attention_with_dashboard_url(self, bot):
        """With dashboard_url, replies with InlineKeyboardMarkup URL button."""
        bot.dashboard_url = "https://myhost.ts.net/attention"
        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await bot._cmd_attention(update, context)

        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args
        assert call_kwargs.kwargs.get("parse_mode") == "Markdown"
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.url == "https://myhost.ts.net/attention"

    @pytest.mark.asyncio
    async def test_attention_unauthorized(self, bot):
        """Unauthorized users get no response."""
        update = MagicMock()
        update.effective_user.id = 999  # Not in allowed_users
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await bot._cmd_attention(update, context)

        update.message.reply_text.assert_not_called()


class TestSendAttentionNotification:
    @pytest.fixture
    def bot(self):
        with patch("src.hub.telegram_bot.Application") as MockApp:
            mock_app = MagicMock()
            MockApp.builder.return_value.token.return_value.build.return_value = mock_app
            mock_app.add_handler = MagicMock()
            mock_app.bot = AsyncMock()
            b = TelegramBot(
                bot_token="fake-token",
                supergroup_id=-100123,
                allowed_users=[42],
                dashboard_url="https://myhost.ts.net/attention",
            )
            return b

    @pytest.mark.asyncio
    async def test_sends_notification(self, bot):
        """Sends a Telegram message with URL button for a WAITING session."""
        session = MagicMock()
        session.machine = "serverlab"
        session.project = "my-project"
        session.session_name = "test-session"
        session.session_id = "sess-12345678"
        session.prompt = None

        await bot.send_attention_notification(session)

        bot.app.bot.send_message.assert_called_once()
        call_kwargs = bot.app.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == -100123
        assert "Attention requise" in call_kwargs["text"]
        assert "serverlab/my-project" in call_kwargs["text"]
        keyboard = call_kwargs["reply_markup"]
        assert keyboard.inline_keyboard[0][0].url == "https://myhost.ts.net/attention"

    @pytest.mark.asyncio
    async def test_no_notification_without_url(self, bot):
        """No notification sent if dashboard_url is empty."""
        bot.dashboard_url = ""
        session = MagicMock()

        await bot.send_attention_notification(session)

        bot.app.bot.send_message.assert_not_called()
