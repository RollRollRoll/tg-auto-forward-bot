import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, Chat, Message
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.services.task_queue import Task

from bot.handlers.conversation import (
    WAITING_CAPTION,
    WAITING_CHANNEL,
    WAITING_RESOLUTION,
    TaskDraft,
    _start_download,
    cancel_handler,
    caption_handler,
)


def _make_update(text="", user_id=111, chat_type=ChatType.PRIVATE):
    update = MagicMock(spec=Update)
    update.effective_chat = MagicMock(spec=Chat)
    update.effective_chat.type = chat_type
    update.effective_chat.id = user_id
    update.effective_user = MagicMock(spec=User)
    update.effective_user.id = user_id
    update.message = MagicMock(spec=Message)
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _make_context(bot_data=None):
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = bot_data or {"task_queue": MagicMock()}
    ctx.bot = AsyncMock()
    ctx.application = MagicMock()
    ctx.application.create_task = MagicMock(
        side_effect=lambda coro, **kwargs: (coro.close(), MagicMock())[1]
    )
    return ctx


@pytest.mark.asyncio
async def test_caption_handler_too_long():
    update = _make_update("a" * 1025)
    ctx = _make_context()
    result = await caption_handler(update, ctx)
    assert result == WAITING_CAPTION
    update.message.reply_text.assert_awaited_once()
    assert "1025" in str(update.message.reply_text.call_args)


@pytest.mark.asyncio
async def test_caption_handler_single_channel():
    update = _make_update("valid caption")
    ctx = _make_context()
    ctx.user_data["draft"] = TaskDraft(source_url="https://x.com/u/status/1")
    channels = [{"chat_id": -100123, "title": "Test"}]
    with patch("bot.handlers.conversation.get_db", new_callable=AsyncMock), \
         patch("bot.handlers.conversation.list_channels", new_callable=AsyncMock, return_value=channels), \
         patch("bot.handlers.conversation.get_setting", new_callable=AsyncMock, return_value="1080"), \
         patch("bot.handlers.conversation.extract_available_resolutions", new_callable=AsyncMock, return_value=[720]):
        result = await caption_handler(update, ctx)
    assert result == ConversationHandler.END
    ctx.application.create_task.assert_called_once()
    assert ctx.application.create_task.call_args.kwargs["update"] is update


@pytest.mark.asyncio
async def test_caption_handler_multi_resolution_shows_keyboard():
    update = _make_update("valid caption")
    ctx = _make_context()
    ctx.user_data["draft"] = TaskDraft(source_url="https://x.com/u/status/1")
    channels = [{"chat_id": -100123, "title": "Chan A"}, {"chat_id": -100456, "title": "Chan B"}]
    with patch("bot.handlers.conversation.get_db", new_callable=AsyncMock), \
         patch("bot.handlers.conversation.list_channels", new_callable=AsyncMock, return_value=channels), \
         patch("bot.handlers.conversation.get_setting", new_callable=AsyncMock, return_value="1080"), \
         patch("bot.handlers.conversation.extract_available_resolutions", new_callable=AsyncMock, return_value=[720, 1080]):
        result = await caption_handler(update, ctx)
    assert result == WAITING_RESOLUTION


@pytest.mark.asyncio
async def test_cancel_handler():
    update = _make_update("/cancel")
    ctx = _make_context()
    ctx.user_data["draft"] = TaskDraft(source_url="https://x.com/u/status/1")
    result = await cancel_handler(update, ctx)
    assert result == ConversationHandler.END
    assert len(ctx.user_data) == 0


@pytest.mark.asyncio
async def test_start_download_uses_application_create_task_and_clears_state():
    update = _make_update("valid caption")
    task_queue = MagicMock()
    ctx = _make_context({"task_queue": task_queue})
    ctx.user_data["draft"] = TaskDraft(
        source_url="https://x.com/u/status/1", caption="<b>ok</b>", channel_chat_id=-100123,
    )

    with patch("bot.handlers.conversation.download_and_publish", new=MagicMock()) as dap:
        result = await _start_download(update, ctx)

    assert result == ConversationHandler.END
    update.message.reply_text.assert_awaited_once_with("Task accepted. Downloading video...")
    # 会话层把整个 Task 交给 pipeline，不再拆成 6 个参数
    assert dap.call_args.kwargs == {
        "bot": ctx.bot, "task_queue": task_queue,
        "task": Task(user_id=111, user_chat_id=111, source_url="https://x.com/u/status/1",
                     caption="<b>ok</b>", channel_chat_id=-100123, max_resolution=1080),
    }
    ctx.application.create_task.assert_called_once()
    assert ctx.application.create_task.call_args.kwargs["update"] is update
    assert ctx.user_data == {}


def _make_status_msg(update):
    # 让 reply_text 返回一个可 await edit_text 的状态消息，便于检查分辨率键盘
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    status_msg.reply_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=status_msg)
    return status_msg


@pytest.mark.asyncio
async def test_resolution_keyboard_respects_max_resolution_setting():
    update = _make_update("valid caption")
    status_msg = _make_status_msg(update)
    ctx = _make_context()
    ctx.user_data["draft"] = TaskDraft(source_url="https://x.com/u/status/1")
    with patch("bot.handlers.conversation.get_db", new_callable=AsyncMock), \
         patch("bot.handlers.conversation.get_setting", new_callable=AsyncMock, return_value="720"), \
         patch("bot.handlers.conversation.extract_available_resolutions", new_callable=AsyncMock, return_value=[480, 720, 1080, 2160]):
        result = await caption_handler(update, ctx)
    assert result == WAITING_RESOLUTION
    markup = status_msg.edit_text.call_args.kwargs["reply_markup"]
    offered = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert offered == ["res:480", "res:720"]


@pytest.mark.asyncio
async def test_resolution_falls_back_to_lowest_when_all_exceed_limit():
    update = _make_update("valid caption")
    status_msg = _make_status_msg(update)
    ctx = _make_context()
    ctx.user_data["draft"] = TaskDraft(source_url="https://x.com/u/status/1")
    channels = [{"chat_id": -100123, "title": "Test"}]
    with patch("bot.handlers.conversation.get_db", new_callable=AsyncMock), \
         patch("bot.handlers.conversation.list_channels", new_callable=AsyncMock, return_value=channels), \
         patch("bot.handlers.conversation.get_setting", new_callable=AsyncMock, return_value="360"), \
         patch("bot.handlers.conversation.extract_available_resolutions", new_callable=AsyncMock, return_value=[1080, 2160]):
        result = await caption_handler(update, ctx)
    assert result == ConversationHandler.END
    status_msg.edit_text.assert_awaited_once_with("Only one resolution available: 1080p FHD. Proceeding...")
    ctx.application.create_task.assert_called_once()


def test_task_draft_builds_task_with_default_resolution():
    draft = TaskDraft(source_url="https://x.com/u/status/1")
    draft.caption = "<b>ok</b>"
    draft.channel_chat_id = -100123
    assert draft.to_task(user_id=111, user_chat_id=222) == Task(
        user_id=111, user_chat_id=222, source_url="https://x.com/u/status/1",
        caption="<b>ok</b>", channel_chat_id=-100123, max_resolution=1080,
    )
