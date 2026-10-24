from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.database.crud import add_admin, add_channel
from bot.handlers import conversation as conv
from bot.services.task_queue import Task
from tests.fakebot import FakeBot

# 对话 handler 直接调用；数据库用 conftest 的内存 SQLite，bot 用 FakeBot，只替换链接解析

CHAT = USER = 111
URL = "https://x.com/u/status/1"


@pytest.fixture
async def admin(db):
    await add_admin(db, user_id=USER)
    return db


async def _channels(db, *titles):
    for i, title in enumerate(titles):
        await add_channel(db, chat_id=-100123 - i, title=title)


def _ctx():
    ctx = MagicMock()
    ctx.bot = FakeBot()
    ctx.user_data = {}
    ctx.bot_data = {"task_queue": MagicMock(), "status_limiter": MagicMock()}
    ctx.application.create_task = MagicMock(side_effect=lambda coro, **kw: (coro.close(), MagicMock())[1])
    return ctx


def _base_update(user_id):
    update = MagicMock(spec=Update)
    update.effective_chat = SimpleNamespace(id=CHAT, type=ChatType.PRIVATE)
    update.effective_user = SimpleNamespace(id=user_id)
    return update


def _text(text, user_id=USER):
    update = _base_update(user_id)
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _press(msg, label, user_id=USER):
    # callback_data 在创建时取出，模拟用户点的是当时消息上的那个按钮
    update = _base_update(user_id)
    update.message = None
    query = MagicMock()
    query.data = msg.button(label)["callback_data"]
    query.message = msg
    query.answer = AsyncMock()
    update.callback_query = query
    return update


def _resolutions(*heights):
    return patch("bot.handlers.conversation.extract_available_resolutions",
                 new_callable=AsyncMock, return_value=list(heights))


def _labels(msg):
    return [b["text"] for b in msg.buttons]


def _expired(update):
    update.callback_query.answer.assert_awaited_once_with("This action has expired")


async def _to_card(ctx, *, heights=(720,), caption=None):
    """走完一遍流程停在 confirmation card，返回卡片消息。需要已有频道。"""
    assert await conv.entry_handler(_text(URL), ctx) == conv.WAITING_CAPTION
    with _resolutions(*heights):
        if caption is None:
            state = await conv.skip_caption_handler(_text("/skip"), ctx)
        else:
            state = await conv.caption_handler(_text(caption), ctx)
    assert state == conv.CONFIRMING
    return ctx.bot.last(CHAT)


# ---- REQ-001 发链接流程的按钮 ----

@pytest.mark.asyncio
async def test_link_prompt_offers_skip_and_cancel_buttons(admin):
    ctx = _ctx()
    assert await conv.entry_handler(_text(URL), ctx) == conv.WAITING_CAPTION
    prompt = ctx.bot.last(CHAT)
    assert prompt.format == "rich" and URL in prompt.text
    assert _labels(prompt) == ["Skip caption", "✖ Cancel"]


@pytest.mark.asyncio
async def test_skip_button_turns_prompt_into_result_and_asks_resolution(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    prompt = ctx.bot.last(CHAT)
    with _resolutions(480, 720):
        state = await conv.caption_button_handler(_press(prompt, "Skip caption"), ctx)
    assert state == conv.WAITING_RESOLUTION
    assert (prompt.text, prompt.buttons) == ("Caption: skipped", [])
    assert _labels(ctx.bot.last(CHAT)) == ["480p", "720p HD", "✖ Cancel"]


@pytest.mark.asyncio
async def test_skip_command_removes_prompt_buttons_too(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    prompt = ctx.bot.last(CHAT)
    with _resolutions(480, 720):
        assert await conv.skip_caption_handler(_text("/skip"), ctx) == conv.WAITING_RESOLUTION
    assert (prompt.text, prompt.buttons) == ("Caption: skipped", [])


@pytest.mark.asyncio
async def test_cancel_button_discards_the_draft(admin):
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    prompt = ctx.bot.last(CHAT)
    assert await conv.caption_button_handler(_press(prompt, "✖ Cancel"), ctx) == ConversationHandler.END
    assert (prompt.text, prompt.buttons) == ("Cancelled.", [])
    assert "draft" not in ctx.user_data


@pytest.mark.asyncio
async def test_caption_too_long_keeps_waiting(admin):
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    update = _text("a" * 1025)
    assert await conv.caption_handler(update, ctx) == conv.WAITING_CAPTION
    assert "1025" in str(update.message.reply_text.call_args)


@pytest.mark.asyncio
async def test_pickers_turn_into_results(admin):
    await _channels(admin, "Chan A", "Chan B")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720, 1080):
        await conv.skip_caption_handler(_text("/skip"), ctx)
    picker = ctx.bot.last(CHAT)
    assert await conv.resolution_handler(_press(picker, "720p HD"), ctx) == conv.WAITING_CHANNEL
    assert (picker.text, picker.buttons) == ("Resolution: 720p HD", [])
    channels = ctx.bot.last(CHAT)
    assert _labels(channels) == ["Chan A", "Chan B", "✖ Cancel"]
    assert await conv.channel_handler(_press(channels, "Chan B"), ctx) == conv.CONFIRMING
    assert (channels.text, channels.buttons) == ("Channel: Chan B", [])


@pytest.mark.asyncio
async def test_cancel_button_on_resolution_picker(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720, 1080):
        await conv.skip_caption_handler(_text("/skip"), ctx)
    picker = ctx.bot.last(CHAT)
    assert await conv.cancel_button_handler(_press(picker, "✖ Cancel"), ctx) == ConversationHandler.END
    assert (picker.text, picker.buttons) == ("Cancelled.", [])
    assert "draft" not in ctx.user_data


@pytest.mark.asyncio
async def test_resolution_list_respects_max_resolution_setting(admin):
    from bot.database.crud import set_setting
    await set_setting(admin, "max_resolution", "720")
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(480, 720, 1080, 2160):
        await conv.skip_caption_handler(_text("/skip"), ctx)
    assert _labels(ctx.bot.last(CHAT)) == ["480p", "720p HD", "✖ Cancel"]


@pytest.mark.asyncio
async def test_no_video_ends_the_flow(admin):
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions():
        assert await conv.skip_caption_handler(_text("/skip"), ctx) == ConversationHandler.END
    assert "No video found" in ctx.bot.last(CHAT).text
    assert "draft" not in ctx.user_data


# ---- REQ-002 confirmation card ----

@pytest.mark.asyncio
async def test_single_options_skip_straight_to_the_card(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx, heights=(720,))
    assert "Confirm the task" in card.text
    assert URL in card.text and "(no caption)" in card.text and "720p HD" in card.text and "Demo" in card.text
    assert _labels(card) == ["✅ Confirm", "✏️ Edit", "✖ Cancel"]
    styles = {b["text"]: b.get("style") for b in card.buttons}
    assert styles["✅ Confirm"] == "success" and styles["✖ Cancel"] == "danger"
    ctx.application.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_card_renders_caption_as_published(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx, caption="<b>Title</b> & more")
    assert "**Title** & more" in card.content
    assert "<b>" not in card.content


@pytest.mark.asyncio
async def test_confirm_submits_exactly_one_task(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx, caption="<b>ok</b>")
    with patch("bot.handlers.conversation.download_and_publish", new=MagicMock()) as dap:
        first = _press(card, "✅ Confirm")
        second = _press(card, "✅ Confirm")
        assert await conv.card_handler(first, ctx) == ConversationHandler.END
        # 第二次点击：会话已结束，按过期处理
        await conv.expired_button_handler(second, ctx)
    assert dap.call_count == 1
    assert dap.call_args.kwargs["task"] == Task(
        user_id=USER, user_chat_id=CHAT, source_url=URL, caption="<b>ok</b>",
        channel_chat_id=-100123, max_resolution=720,
    )
    assert dap.call_args.kwargs["limiter"] is ctx.bot_data["status_limiter"]
    assert ctx.application.create_task.call_count == 1
    assert "Submitted" in card.text and card.buttons == []
    _expired(second)


@pytest.mark.asyncio
async def test_cancel_on_card_submits_nothing(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    assert await conv.card_handler(_press(card, "✖ Cancel"), ctx) == ConversationHandler.END
    assert (card.text, card.buttons) == ("Cancelled.", [])
    ctx.application.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_no_channels_ends_the_flow(admin):
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720):
        assert await conv.skip_caption_handler(_text("/skip"), ctx) == ConversationHandler.END
    assert "No channels configured" in ctx.bot.last(CHAT).text


# ---- REQ-003 从 confirmation card 修改 ----

@pytest.mark.asyncio
async def test_edit_menu_hides_options_with_a_single_choice(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx, heights=(720,))
    assert await conv.card_handler(_press(card, "✏️ Edit"), ctx) == conv.CONFIRMING
    assert _labels(card) == ["Caption", "« Back"]


@pytest.mark.asyncio
async def test_edit_resolution_reuses_the_first_lookup(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720, 1080) as lookup:
        await conv.skip_caption_handler(_text("/skip"), ctx)
        picker = ctx.bot.last(CHAT)
        await conv.resolution_handler(_press(picker, "1080p FHD"), ctx)
        card = ctx.bot.last(CHAT)
        assert "1080p FHD" in card.text
        await conv.card_handler(_press(card, "✏️ Edit"), ctx)
        assert _labels(card) == ["Caption", "Resolution", "« Back"]
        await conv.card_handler(_press(card, "Resolution"), ctx)
        assert _labels(card) == ["720p HD", "1080p FHD", "« Back"]
        assert await conv.card_handler(_press(card, "720p HD"), ctx) == conv.CONFIRMING
    assert lookup.await_count == 1
    assert "720p HD" in card.text and _labels(card) == ["✅ Confirm", "✏️ Edit", "✖ Cancel"]


@pytest.mark.asyncio
async def test_edit_channel(admin):
    await _channels(admin, "Chan A", "Chan B")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720):
        await conv.skip_caption_handler(_text("/skip"), ctx)
    await conv.channel_handler(_press(ctx.bot.last(CHAT), "Chan A"), ctx)
    card = ctx.bot.last(CHAT)
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    await conv.card_handler(_press(card, "Channel"), ctx)
    assert await conv.card_handler(_press(card, "Chan B"), ctx) == conv.CONFIRMING
    assert "Chan B" in card.text and "Chan A" not in card.text


@pytest.mark.asyncio
async def test_edit_channel_lists_channels_configured_now(admin):
    await _channels(admin, "Chan A")
    ctx = _ctx()
    card = await _to_card(ctx)
    # 卡片显示之后又加了一个 Channel
    await add_channel(admin, chat_id=-100999, title="Chan New")
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    assert "Channel" in _labels(card)
    await conv.card_handler(_press(card, "Channel"), ctx)
    assert _labels(card) == ["Chan A", "Chan New", "« Back"]
    await conv.card_handler(_press(card, "Chan New"), ctx)
    assert "Chan New" in card.text


@pytest.mark.asyncio
async def test_edit_caption_with_new_text_returns_to_card(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    assert await conv.card_handler(_press(card, "Caption"), ctx) == conv.EDIT_CAPTION
    prompt = ctx.bot.last(CHAT)
    assert _labels(prompt) == ["Skip caption", "Keep current"]
    assert await conv.caption_handler(_text("<i>new</i>"), ctx) == conv.CONFIRMING
    new_card = ctx.bot.last(CHAT)
    assert "_new_" in new_card.content and _labels(new_card) == ["✅ Confirm", "✏️ Edit", "✖ Cancel"]
    assert prompt.buttons == []
    # 旧卡片已失效，按钮去掉
    assert card.buttons == []


@pytest.mark.asyncio
async def test_edit_caption_keep_current(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx, caption="old")
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    await conv.card_handler(_press(card, "Caption"), ctx)
    prompt = ctx.bot.last(CHAT)
    assert await conv.caption_button_handler(_press(prompt, "Keep current"), ctx) == conv.CONFIRMING
    assert "old" in ctx.bot.last(CHAT).text


@pytest.mark.asyncio
async def test_edit_caption_validation_error_keeps_waiting(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    await conv.card_handler(_press(card, "Caption"), ctx)
    update = _text("a" * 1025)
    assert await conv.caption_handler(update, ctx) == conv.EDIT_CAPTION
    assert "1025" in str(update.message.reply_text.call_args)


@pytest.mark.asyncio
async def test_back_from_edit_menu(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    await conv.card_handler(_press(card, "✏️ Edit"), ctx)
    assert await conv.card_handler(_press(card, "« Back"), ctx) == conv.CONFIRMING
    assert _labels(card) == ["✅ Confirm", "✏️ Edit", "✖ Cancel"]


# ---- REQ-007 过期按钮 / REQ-009 中途发新链接 ----

@pytest.mark.asyncio
async def test_new_link_on_card_restarts_and_old_card_expires(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    old_card = await _to_card(ctx)
    other = "https://x.com/u/status/2"
    assert await conv.entry_handler(_text(other), ctx) == conv.WAITING_CAPTION
    assert other in ctx.bot.last(CHAT).text
    assert old_card.buttons == []
    stale = _press(old_card, "✅ Confirm") if old_card.buttons else None
    assert stale is None  # 旧卡片已经没有可点的按钮


@pytest.mark.asyncio
async def test_button_from_a_replaced_draft_is_expired(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    old_card = await _to_card(ctx)
    stale = _press(old_card, "✅ Confirm")
    await conv.entry_handler(_text("https://x.com/u/status/2"), ctx)
    with patch("bot.handlers.conversation.download_and_publish", new=MagicMock()) as dap:
        # None：ConversationHandler 保持当前状态不变
        assert await conv.card_handler(stale, ctx) is None
    dap.assert_not_called()
    _expired(stale)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,label", [
    ("card_handler", "✅ Confirm"), ("cancel_button_handler", "✖ Cancel"),
])
async def test_stale_buttons_leave_the_current_card_untouched(admin, handler, label):
    await _channels(admin, "Demo")
    ctx = _ctx()
    old_card = await _to_card(ctx)
    # 旧卡片的按钮因为去键盘失败（如 429）还留着
    stale_data = {b["text"]: b["callback_data"] for b in old_card.buttons}
    stale_data["✖ Cancel"] = stale_data["✖ Cancel"].replace("card:", "flow:", 1)
    card = await _to_card(ctx)
    old_card.keyboard = [[{"text": label, "callback_data": stale_data[label]}]]
    assert await getattr(conv, handler)(_press(old_card, label), ctx) is None
    # 当前卡片仍然可以正常确认
    with patch("bot.handlers.conversation.download_and_publish", new=MagicMock()) as dap:
        assert await conv.card_handler(_press(card, "✅ Confirm"), ctx) == ConversationHandler.END
    assert dap.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["resolution_handler", "channel_handler", "caption_button_handler"])
async def test_stale_picker_buttons_return_none(admin, handler):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await _to_card(ctx)
    msg = await ctx.bot.send_message(CHAT, "old")
    msg.keyboard = [[{"text": "x", "callback_data": "res:deadbeef:720"}]]
    update = _press(msg, "x")
    assert await getattr(conv, handler)(update, ctx) is None
    _expired(update)


@pytest.mark.asyncio
async def test_link_while_waiting_for_caption_is_the_caption(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    await conv.entry_handler(_text(URL), ctx)
    with _resolutions(720):
        assert await conv.caption_handler(_text("see https://e.com"), ctx) == conv.CONFIRMING
    assert "https://e.com" in ctx.bot.last(CHAT).text


@pytest.mark.asyncio
async def test_expired_button_after_restart_or_cancel(admin):
    ctx = _ctx()
    msg = await ctx.bot.send_message(CHAT, "old", reply_markup=None)
    msg.keyboard = [[{"text": "✅ Confirm", "callback_data": "card:deadbeef:confirm"}]]
    update = _press(msg, "✅ Confirm")
    assert await conv.expired_button_handler(update, ctx) is None
    _expired(update)
    assert msg.buttons == []


@pytest.mark.asyncio
async def test_cancel_command_expires_open_keyboards(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    update = _text("/cancel")
    assert await conv.cancel_handler(update, ctx) == ConversationHandler.END
    update.message.reply_text.assert_awaited_once_with("Cancelled.")
    assert card.buttons == [] and "draft" not in ctx.user_data


# ---- REQ-012 权限 ----

@pytest.mark.asyncio
async def test_non_admin_buttons_do_nothing(admin):
    await _channels(admin, "Demo")
    ctx = _ctx()
    card = await _to_card(ctx)
    for handler in (conv.card_handler, conv.caption_button_handler, conv.cancel_button_handler,
                    conv.resolution_handler, conv.channel_handler, conv.expired_button_handler):
        update = _press(card, "✅ Confirm", user_id=999)
        assert await handler(update, ctx) is None
    ctx.application.create_task.assert_not_called()
    assert _labels(card) == ["✅ Confirm", "✏️ Edit", "✖ Cancel"]
