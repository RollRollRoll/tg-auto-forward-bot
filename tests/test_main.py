from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.main import BotApplication, main, post_stop


def test_httpx_info_logs_are_silenced():
    # httpx 的 INFO 日志会带出含 token 的 URL
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


@pytest.mark.asyncio
async def test_post_stop_closes_db():
    application = MagicMock()
    with patch("bot.main.close_db", new_callable=AsyncMock) as close_db_mock:
        await post_stop(application)
    close_db_mock.assert_awaited_once()


def test_main_registers_post_stop_hook():
    builder = MagicMock()
    app = MagicMock()
    app.bot_data = {}

    builder.application_class.return_value = builder
    builder.token.return_value = builder
    builder.base_url.return_value = builder
    builder.base_file_url.return_value = builder
    builder.local_mode.return_value = builder
    builder.post_init.return_value = builder
    builder.post_stop.return_value = builder
    builder.build.return_value = app

    with patch("bot.main.ApplicationBuilder", return_value=builder), \
         patch("bot.main.build_conversation_handler", return_value="conversation_handler"), \
         patch("bot.main.CommandHandler", side_effect=lambda *args, **kwargs: ("command", args, kwargs)):
        main()

    builder.post_stop.assert_called_once_with(post_stop)
    builder.application_class.assert_called_once_with(BotApplication)
    app.add_handler.assert_any_call("conversation_handler")
    app.run_polling.assert_called_once()


def test_main_registers_settings_and_expired_button_handlers():
    builder = MagicMock()
    app = MagicMock()
    for name in ("application_class", "token", "base_url", "base_file_url", "local_mode", "post_init", "post_stop"):
        getattr(builder, name).return_value = builder
    builder.build.return_value = app

    with patch("bot.main.ApplicationBuilder", return_value=builder), \
         patch("bot.main.build_conversation_handler", return_value="conversation_handler"):
        main()

    patterns = [c.args[0].pattern.pattern for c in app.add_handler.call_args_list
                if hasattr(c.args[0], "pattern") and c.args[0].pattern is not None]
    assert r"^set:" in patterns
    assert r"^tasks:refresh$" in patterns
    assert r"^(flow|res|ch|card):" in patterns
