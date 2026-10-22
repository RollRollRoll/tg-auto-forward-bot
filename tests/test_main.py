from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.main import BotApplication, main, post_stop


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
