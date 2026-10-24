from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update
from telegram.constants import ChatType

from bot.database.crud import add_admin, add_channel, get_setting, set_setting
from bot.handlers import admin as admin_handlers
from bot.handlers.menu import menu_callback_handler, settings_callback_handler
from bot.handlers.start import start_handler
from bot.services.task_queue import InMemoryTaskStore, TaskQueue
from tests.fakebot import FakeBot

# 菜单和列表命令：数据库用 conftest 的内存 SQLite，bot 用 FakeBot。conftest 里 SUPER_ADMIN_ID=0

CHAT = ADMIN = 111
SUPER = 0


@pytest.fixture
async def admin(db):
    await add_admin(db, user_id=ADMIN)
    return db


def _ctx():
    ctx = MagicMock()
    ctx.bot = FakeBot()
    ctx.user_data = {}
    ctx.bot_data = {"task_queue": TaskQueue(InMemoryTaskStore())}
    return ctx


def _base_update(user_id):
    update = MagicMock(spec=Update)
    update.effective_chat = SimpleNamespace(id=CHAT, type=ChatType.PRIVATE)
    update.effective_user = SimpleNamespace(id=user_id)
    return update


def _command(user_id=ADMIN):
    update = _base_update(user_id)
    update.callback_query = None
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _press(msg, label, user_id=ADMIN):
    update = _base_update(user_id)
    update.message = None
    query = MagicMock()
    query.data = msg.button(label)["callback_data"]
    query.message = msg
    query.answer = AsyncMock()
    query.delete_message = AsyncMock()
    update.callback_query = query
    return update


def _labels(msg):
    return [b["text"] for b in msg.buttons]


async def _menu(ctx, user_id=ADMIN):
    await start_handler(_command(user_id), ctx)
    return ctx.bot.last(CHAT)


async def _click(ctx, msg, label, user_id=ADMIN):
    update = _press(msg, label, user_id)
    data = update.callback_query.data
    handler = settings_callback_handler if data.startswith("set:") else menu_callback_handler
    await handler(update, ctx)
    return update


@pytest.mark.asyncio
async def test_start_shows_rich_main_menu_for_admin(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    assert menu.format == "rich" and "Video Forward Bot" in menu.text
    assert _labels(menu) == ["📋 Tasks", "📢 Channels", "⚙️ Settings", "❓ Help", "✖ Close"]


@pytest.mark.asyncio
async def test_super_admin_also_sees_admins(admin):
    ctx = _ctx()
    menu = await _menu(ctx, user_id=SUPER)
    assert "👤 Admins" in _labels(menu)


@pytest.mark.asyncio
async def test_settings_page_lists_values_with_a_button_per_setting(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "⚙️ Settings")
    assert "max_resolution = 1080" in menu.text.replace("`", "")
    assert _labels(menu) == [
        "max_concurrent_downloads: 2", "max_file_size_mb: 2000", "max_resolution: 1080", "« Back",
    ]


@pytest.mark.asyncio
async def test_pick_a_preset_writes_the_setting_and_returns_to_settings(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "⚙️ Settings")
    await _click(ctx, menu, "max_resolution: 1080")
    assert _labels(menu) == ["360p", "480p", "720p", "1080p", "1440p", "2160p", "« Back"]
    styles = {b["text"]: b.get("style") for b in menu.buttons}
    assert styles["1080p"] == "primary" and styles["720p"] is None
    await _click(ctx, menu, "720p")
    assert await get_setting(admin, "max_resolution") == "720"
    assert "max_resolution = 720" in menu.text.replace("`", "")
    assert "max_resolution: 720" in _labels(menu)


@pytest.mark.asyncio
async def test_value_outside_presets_is_shown_without_highlight(admin):
    await set_setting(admin, "max_file_size_mb", "800")
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "⚙️ Settings")
    await _click(ctx, menu, "max_file_size_mb: 800")
    assert "800" in menu.text
    assert _labels(menu) == ["500 MB", "1000 MB", "1500 MB", "2000 MB", "« Back"]
    assert all(b.get("style") is None for b in menu.buttons)


@pytest.mark.asyncio
async def test_tampered_setting_value_is_rejected(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "⚙️ Settings")
    await _click(ctx, menu, "max_resolution: 1080")
    menu.button("720p")["callback_data"] = "set:max_resolution:999"
    update = await _click(ctx, menu, "720p")
    assert await get_setting(admin, "max_resolution") == "1080"
    assert "must be one of" in update.callback_query.answer.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,heading", [("📢 Channels", "Channels (1)"), ("👤 Admins", "Admins (1)")])
async def test_channels_and_admins_pages_have_only_back(admin, label, heading):
    await add_channel(admin, chat_id=-100123, title="Demo")
    ctx = _ctx()
    menu = await _menu(ctx, user_id=SUPER)
    await _click(ctx, menu, label, user_id=SUPER)
    assert heading in menu.text
    assert _labels(menu) == ["« Back"]


@pytest.mark.asyncio
async def test_back_returns_to_main_menu(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "❓ Help")
    assert "How to use" in menu.text and menu.format == "rich"
    await _click(ctx, menu, "« Back")
    assert _labels(menu)[0] == "📋 Tasks"


@pytest.mark.asyncio
async def test_tasks_page(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "📋 Tasks")
    assert "Active Downloads (0/2)" in menu.text and "No active tasks." in menu.text


@pytest.mark.asyncio
async def test_non_admin_buttons_change_nothing(admin):
    ctx = _ctx()
    menu = await _menu(ctx)
    await _click(ctx, menu, "⚙️ Settings")
    await _click(ctx, menu, "max_resolution: 1080")
    before = (menu.content, _labels(menu))
    await _click(ctx, menu, "720p", user_id=999)
    assert await get_setting(admin, "max_resolution") == "1080"
    assert (menu.content, _labels(menu)) == before


@pytest.mark.asyncio
async def test_regular_admin_cannot_open_admins_page(admin):
    ctx = _ctx()
    menu = await _menu(ctx, user_id=SUPER)
    before = menu.content
    await _click(ctx, menu, "👤 Admins", user_id=ADMIN)
    assert menu.content == before


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,heading", [
    (admin_handlers.settings_handler, "Settings"),
    (admin_handlers.tasks_handler, "Active Downloads"),
    (admin_handlers.list_channels_handler, "No channels configured."),
])
async def test_list_commands_reply_with_the_same_rich_page(admin, handler, heading):
    ctx = _ctx()
    await handler(_command(), ctx)
    msg = ctx.bot.last(CHAT)
    assert msg.format == "rich" and heading in msg.text and msg.buttons == []


@pytest.mark.asyncio
async def test_list_admins_command_is_rich_for_super_admin(admin):
    ctx = _ctx()
    await admin_handlers.list_admins_handler(_command(SUPER), ctx)
    msg = ctx.bot.last(CHAT)
    assert msg.format == "rich" and "Admins (1)" in msg.text


@pytest.mark.asyncio
async def test_tasks_page_keeps_the_existing_wording(admin):
    from bot.services.task_queue import Task
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(Task(user_id=1, user_chat_id=1, source_url="https://a", caption="",
                                     channel_chat_id=-1, max_resolution=720), max_slots=2)
    queue.update_progress(ticket.task_id, 100.0, "processing")
    await admin_handlers.tasks_handler(_command(), ctx)
    text = ctx.bot.last(CHAT).text
    assert "Processing" in text and "Progress: Converting format..." in text
    queue.update_progress(ticket.task_id, 100.0, "publishing")
    await admin_handlers.tasks_handler(_command(), ctx)
    text = ctx.bot.last(CHAT).text
    assert "Publishing" in text and "Progress: Uploading to channel..." in text
