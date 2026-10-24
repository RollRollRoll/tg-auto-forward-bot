from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, RetryAfter

from bot.database.crud import add_admin, add_channel, create_post_log, set_setting, update_post_log_status
from bot.handlers import admin as admin_handlers
from bot.handlers.menu import menu_callback_handler, tasks_refresh_handler
from bot.handlers.start import start_handler
from bot.services.task_queue import InMemoryTaskStore, Task, TaskQueue
from tests.fakebot import FakeBot

# Tasks 页（issue #5）：数据库用 conftest 的内存 SQLite，bot 用 FakeBot。
# "当前时间"固定为 NOW，post log 的 updated_at 按它倒推，相对时间才能稳定复现

CHAT = ADMIN = 111
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
DEMO = -100123


@pytest.fixture
async def admin(db):
    await add_admin(db, user_id=ADMIN)
    await add_channel(db, chat_id=DEMO, title="Demo")
    return db


@pytest.fixture(autouse=True)
def fixed_now():
    with patch("bot.ui.pages.utc_now", return_value=NOW):
        yield


def _ctx():
    ctx = MagicMock()
    ctx.bot = FakeBot()
    ctx.bot.chat_info[DEMO] = SimpleNamespace(username="demo", title="Demo")
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
    update.callback_query = query
    return update


def _labels(msg):
    return [b["text"] for b in msg.buttons]


async def _open_from_menu(ctx, user_id=ADMIN):
    await start_handler(_command(user_id), ctx)
    menu = ctx.bot.last(CHAT)
    await menu_callback_handler(_press(menu, "📋 Tasks", user_id), ctx)
    return menu


async def _command_page(ctx, user_id=ADMIN):
    await admin_handlers.tasks_handler(_command(user_id), ctx)
    return ctx.bot.last(CHAT)


async def _refresh(ctx, msg, user_id=ADMIN):
    update = _press(msg, "🔄 Refresh", user_id)
    handler = tasks_refresh_handler if update.callback_query.data == "tasks:refresh" else menu_callback_handler
    await handler(update, ctx)
    return update


def _task(url="https://www.youtube.com/watch?v=5DslLmsx9s0", channel=DEMO):
    return Task(user_id=1, user_chat_id=1, source_url=url, caption="", channel_chat_id=channel, max_resolution=720)


async def _finished_log(db, *, ago, status="done", message_id=None, error=None,
                        url="https://archive.org/details/BigBuckBunny_124", channel=DEMO):
    log_id = await create_post_log(db, admin_user_id=1, source_url=url, channel_chat_id=channel, caption="")
    await update_post_log_status(db, log_id, status=status, message_id=message_id, error_message=error)
    finished = (NOW - ago).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("UPDATE post_logs SET updated_at = ? WHERE id = ?", (finished, log_id))
    await db.commit()
    return log_id


def _section(text, title, next_titles=("In progress", "Queued", "Recent")):
    """取某一节的文字（到下一个标题为止）。"""
    lines = text.splitlines()
    start = lines.index(title) + 1
    end = next((i for i in range(start, len(lines)) if lines[i] in next_titles), len(lines))
    return [line for line in lines[start:end] if line]


# ---- REQ-001 整体布局 ----

@pytest.mark.asyncio
async def test_page_with_running_and_queued_tasks(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    running = await queue.submit(_task(), max_slots=1)
    queue.update_progress(running.task_id, 62.0, "downloading")
    await queue.submit(_task("https://archive.org/details/x"), max_slots=1)
    page = await _open_from_menu(ctx)
    lines = [line for line in page.text.splitlines() if line]
    assert lines[0] == "📋 Tasks"
    assert lines[1] == "Running 1/2 · Queued 1"
    assert [line for line in lines if line in ("In progress", "Queued", "Recent")] == ["In progress", "Queued", "Recent"]
    assert _labels(page) == ["🔄 Refresh", "« Back"]


@pytest.mark.asyncio
async def test_empty_page_from_the_command(admin):
    ctx = _ctx()
    page = await _command_page(ctx)
    assert page.format == "rich"
    assert "Running 0/2 · Queued 0" in page.text
    assert _section(page.text, "In progress") == ["No active tasks."]
    assert _section(page.text, "Recent") == ["No tasks yet."]
    assert "Queued\n" not in page.text + "\n" and "\nQueued" not in page.text
    assert _labels(page) == ["🔄 Refresh"]


@pytest.mark.asyncio
async def test_page_falls_back_to_html_with_the_same_content(admin):
    ctx = _ctx()
    ctx.bot.fail("sendRichMessage", BadRequest("Rich_message_markdown_invalid"))
    await _finished_log(admin, ago=timedelta(minutes=12), message_id=79)
    page = await _command_page(ctx)
    assert page.format == "html"
    assert "Running 0/2 · Queued 0" in page.text and "✅ Demo · 12m ago · Open post" in page.text
    assert '<a href="https://t.me/demo/79">Open post</a>' in page.content
    assert _labels(page) == ["🔄 Refresh"]


@pytest.mark.asyncio
async def test_summary_uses_the_current_concurrency_setting(admin):
    await set_setting(admin, "max_concurrent_downloads", "4")
    ctx = _ctx()
    page = await _command_page(ctx)
    assert "Running 0/4 · Queued 0" in page.text


# ---- REQ-002 In progress / REQ-003 Queued ----

@pytest.mark.asyncio
async def test_downloading_task_shows_stage_channel_elapsed_link_and_progress(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(_task(), max_slots=2)
    queue.update_progress(ticket.task_id, 62.0, "downloading")
    with patch("bot.services.task_queue.time.time", return_value=queue.get_active_tasks()[0]["start_time"] + 72):
        page = await _command_page(ctx)
    assert _section(page.text, "In progress") == [
        "⬇️ Downloading · Demo · 1m 12s",
        "youtube.com/watch?v=5DslLmsx9s0",
        "[██████░░░░] 62.0%",
    ]
    # 不显示速度、ETA 和提交人（NEG-003）
    assert "/s" not in page.text and "ETA" not in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,first_line", [
    ("starting", "⏳ Starting"), ("processing", "⚙️ Converting"), ("publishing", "⬆️ Uploading"),
])
async def test_stage_names_match_the_status_message(admin, status, first_line):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(_task(), max_slots=2)
    queue.update_progress(ticket.task_id, 100.0, status)
    page = await _command_page(ctx)
    block = _section(page.text, "In progress")
    assert block[0].startswith(first_line + " · Demo · ")
    assert len(block) == 2  # 只有下载阶段有进度条
    assert "Processing" not in page.text and "Publishing" not in page.text
    assert "Progress:" not in page.text


@pytest.mark.asyncio
async def test_queued_tasks_in_submit_order(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    await queue.submit(_task("https://busy"), max_slots=1)
    await queue.submit(_task("https://first.example/v"), max_slots=1)
    await queue.submit(_task("https://second.example/v"), max_slots=1)
    page = await _command_page(ctx)
    block = _section(page.text, "Queued")
    assert block[0].startswith("1. Demo · waiting ") and block[1] == "first.example/v"
    assert block[2].startswith("2. Demo · waiting ") and block[3] == "second.example/v"


# ---- REQ-004 Recent / REQ-005 Channel 名称 ----

@pytest.mark.asyncio
async def test_recent_done_and_failed(admin):
    await _finished_log(admin, ago=timedelta(hours=1), status="failed", error="Timed out")
    await _finished_log(admin, ago=timedelta(minutes=12), message_id=79)
    ctx = _ctx()
    page = await _command_page(ctx)
    assert _section(page.text, "Recent") == [
        "✅ Demo · 12m ago · Open post",
        "archive.org/details/BigBuckBunny_124",
        "❌ Demo · 1h ago",
        "archive.org/details/BigBuckBunny_124",
        "Timed out",
    ]
    assert "[Open post](https://t.me/demo/79)" in page.content


@pytest.mark.asyncio
async def test_recent_only_lists_finished_tasks_by_finish_time(admin):
    # 8 条已结束，外加 1 条还在下载
    for minutes in (50, 40, 30, 20, 10, 5, 3, 1):
        await _finished_log(admin, ago=timedelta(minutes=minutes), url=f"https://e.com/{minutes}")
    await create_post_log(admin, admin_user_id=1, source_url="https://e.com/running", channel_chat_id=DEMO, caption="")
    ctx = _ctx()
    page = await _command_page(ctx)
    recent = _section(page.text, "Recent")
    assert [line for line in recent if line.startswith("e.com/")] == [
        "e.com/1", "e.com/3", "e.com/5", "e.com/10", "e.com/20",
    ]
    assert "e.com/running" not in page.text


@pytest.mark.asyncio
async def test_task_that_ran_long_but_just_finished_is_just_now(admin):
    # A 先提交、下载很久、刚结束；B 后提交、10 分钟前结束
    a = await _finished_log(admin, ago=timedelta(seconds=10), url="https://e.com/a")
    await _finished_log(admin, ago=timedelta(minutes=10), url="https://e.com/b")
    await admin.execute("UPDATE post_logs SET created_at = '2026-10-24 11:30:00' WHERE id = ?", (a,))
    await admin.commit()
    ctx = _ctx()
    page = await _command_page(ctx)
    assert _section(page.text, "Recent")[:4] == [
        "✅ Demo · just now", "e.com/a", "✅ Demo · 10m ago", "e.com/b",
    ]


@pytest.mark.asyncio
async def test_same_finish_time_is_ordered_by_newest_record(admin):
    await _finished_log(admin, ago=timedelta(minutes=5), url="https://e.com/older")
    await _finished_log(admin, ago=timedelta(minutes=5), url="https://e.com/newer")
    ctx = _ctx()
    page = await _command_page(ctx)
    recent = [line for line in _section(page.text, "Recent") if line.startswith("e.com/")]
    assert recent == ["e.com/newer", "e.com/older"]


@pytest.mark.asyncio
async def test_interrupted_task_shows_in_recent(admin):
    from bot.database.crud import fail_interrupted_post_logs
    await create_post_log(admin, admin_user_id=1, source_url="https://e.com/x", channel_chat_id=DEMO, caption="")
    await fail_interrupted_post_logs(admin)
    await admin.execute("UPDATE post_logs SET updated_at = ?", ((NOW - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),))
    await admin.commit()
    ctx = _ctx()
    page = await _command_page(ctx)
    assert _section(page.text, "In progress") == ["No active tasks."]
    assert _section(page.text, "Recent") == ["❌ Demo · 2m ago", "e.com/x", "Interrupted by restart"]


@pytest.mark.asyncio
async def test_private_channel_post_link(admin):
    await add_channel(admin, chat_id=-1001234567890, title="Private")
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=5, channel=-1001234567890)
    ctx = _ctx()
    ctx.bot.chat_info[-1001234567890] = SimpleNamespace(username=None, title="Private")
    page = await _command_page(ctx)
    assert "[Open post](https://t.me/c/1234567890/5)" in page.content


@pytest.mark.asyncio
async def test_done_without_post_id_has_no_open_post(admin):
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=None)
    ctx = _ctx()
    page = await _command_page(ctx)
    assert _section(page.text, "Recent")[0] == "✅ Demo · 1m ago"
    assert "Open post" not in page.text


@pytest.mark.asyncio
async def test_long_failure_reason_is_truncated(admin):
    await _finished_log(admin, ago=timedelta(minutes=1), status="failed", error="x" * 300)
    ctx = _ctx()
    page = await _command_page(ctx)
    assert _section(page.text, "Recent")[2] == "x" * 100 + "…"


@pytest.mark.asyncio
async def test_removed_channel_falls_back_to_bot_title_then_chat_id(admin):
    await _finished_log(admin, ago=timedelta(minutes=2), message_id=1, channel=-100777)
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=2, channel=-100888)
    ctx = _ctx()
    # -100777 已不在配置里但 Bot 还能取到标题；-100888 什么都取不到
    ctx.bot.chat_info[-100777] = SimpleNamespace(username="old", title="Old channel")
    page = await _command_page(ctx)
    recent = _section(page.text, "Recent")
    assert recent[0] == "✅ -100888 · 1m ago · Open post"
    assert recent[2] == "✅ Old channel · 2m ago · Open post"
    assert "[Open post](https://t.me/c/888/2)" in page.content
    assert "[Open post](https://t.me/old/1)" in page.content


@pytest.mark.asyncio
async def test_channel_lookups_are_one_per_channel(admin):
    for minutes in (1, 2, 3):
        await _finished_log(admin, ago=timedelta(minutes=minutes), message_id=minutes)
    ctx = _ctx()
    await _command_page(ctx)
    assert ctx.bot.methods().count("getChat") == 1


@pytest.mark.asyncio
async def test_channel_titles_are_escaped(admin):
    await add_channel(admin, chat_id=-100555, title="A_b*c [x]")
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=1, channel=-100555)
    ctx = _ctx()
    ctx.bot.chat_info[-100555] = SimpleNamespace(username="ab", title="A_b*c [x]")
    page = await _command_page(ctx)
    assert "✅ A_b*c [x] · 1m ago · Open post" in page.text
    assert r"A\_b\*c \[x\]" in page.content


# ---- REQ-007 Refresh ----

@pytest.mark.asyncio
async def test_refresh_on_the_menu_page_updates_in_place(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(_task(), max_slots=2)
    queue.update_progress(ticket.task_id, 10.0, "downloading")
    page = await _open_from_menu(ctx)
    assert "Running 1/2" in page.text
    # 这个 Task 发布成功
    async with queue.slot(ticket):
        pass
    await _finished_log(admin, ago=timedelta(seconds=5), message_id=80)
    update = await _refresh(ctx, page)
    assert ctx.bot.last(CHAT) is page
    assert _section(page.text, "In progress") == ["No active tasks."]
    assert _section(page.text, "Recent")[0] == "✅ Demo · just now · Open post"
    assert _labels(page) == ["🔄 Refresh", "« Back"]
    update.callback_query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
async def test_refresh_on_the_command_reply_keeps_only_refresh(admin):
    ctx = _ctx()
    page = await _command_page(ctx)
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=1)
    update = await _refresh(ctx, page)
    assert ctx.bot.last(CHAT) is page
    assert _section(page.text, "Recent")[0] == "✅ Demo · 1m ago · Open post"
    assert _labels(page) == ["🔄 Refresh"]
    update.callback_query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
async def test_refresh_with_nothing_new_still_says_updated(admin):
    ctx = _ctx()
    page = await _command_page(ctx)
    before = page.content
    ctx.bot.fail("editMessageText", BadRequest("Message is not modified: specified new message content is the same"))
    update = await _refresh(ctx, page)
    assert page.content == before
    update.callback_query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", ["menu", "command"])
async def test_failed_refresh_keeps_the_page(admin, opener):
    ctx = _ctx()
    page = await (_open_from_menu(ctx) if opener == "menu" else _command_page(ctx))
    before = (page.content, _labels(page))
    await _finished_log(admin, ago=timedelta(minutes=1))
    ctx.bot.fail("editMessageText", RetryAfter(5))
    update = await _refresh(ctx, page)
    assert (page.content, _labels(page)) == before
    update.callback_query.answer.assert_awaited_once_with("Couldn't refresh, try again")


# ---- REQ-008 权限 ----

@pytest.mark.asyncio
async def test_non_admin_cannot_refresh(admin):
    ctx = _ctx()
    page = await _command_page(ctx)
    await _finished_log(admin, ago=timedelta(minutes=1))
    calls = len(ctx.bot.calls)
    await _refresh(ctx, page, user_id=999)
    assert len(ctx.bot.calls) == calls
    assert "No tasks yet." in page.text


@pytest.mark.asyncio
async def test_non_admin_gets_nothing_from_the_command(admin):
    ctx = _ctx()
    await admin_handlers.tasks_handler(_command(999), ctx)
    assert ctx.bot.messages(CHAT) == []


# ---- NEG-001 / NEG-002 / NEG-004 ----

@pytest.mark.asyncio
async def test_page_has_no_task_actions_paging_filters_or_counts(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(_task(), max_slots=1)
    queue.update_progress(ticket.task_id, 30.0, "downloading")
    await queue.submit(_task("https://queued.example/v"), max_slots=1)
    for minutes in range(1, 9):
        await _finished_log(admin, ago=timedelta(minutes=minutes), status="failed" if minutes % 2 else "done",
                            message_id=minutes, error="boom")
    page = await _open_from_menu(ctx)
    assert _labels(page) == ["🔄 Refresh", "« Back"]
    assert sum(line.startswith(("✅", "❌")) for line in _section(page.text, "Recent")) == 5
    for word in ("Cancel", "Retry", "Next", "More", "Filter", "succeeded", "failed:"):
        assert word not in page.text


@pytest.mark.asyncio
async def test_page_does_not_update_by_itself(admin):
    ctx = _ctx()
    queue = ctx.bot_data["task_queue"]
    ticket = await queue.submit(_task(), max_slots=2)
    page = await _command_page(ctx)
    calls = len(ctx.bot.calls)
    queue.update_progress(ticket.task_id, 80.0, "downloading")
    async with queue.slot(ticket):
        pass
    await _finished_log(admin, ago=timedelta(seconds=1))
    assert [m for _, m, kw in ctx.bot.calls[calls:] if kw.get("message_id") == page.message_id] == []


@pytest.mark.asyncio
async def test_running_and_queued_tasks_of_a_removed_channel_use_the_bot_title(admin):
    # Task 提交后 Channel 被 /remove_channel 删掉：名称同样按 配置 → Bot 取到的标题 → chat_id
    ctx = _ctx()
    ctx.bot.chat_info[-100777] = SimpleNamespace(username="old", title="Old channel")
    queue = ctx.bot_data["task_queue"]
    await queue.submit(_task(channel=-100777), max_slots=1)
    await queue.submit(_task(channel=-100777), max_slots=1)
    await queue.submit(_task(channel=-100888), max_slots=1)
    page = await _command_page(ctx)
    assert _section(page.text, "In progress")[0].startswith("⏳ Starting · Old channel · ")
    queued = _section(page.text, "Queued")
    assert queued[0].startswith("1. Old channel · waiting ")
    assert queued[2].startswith("2. -100888 · waiting ")
    # 已配置的 Channel 不查；未配置的每个只查一次
    assert sorted(kw["chat_id"] for _, m, kw in ctx.bot.calls if m == "getChat") == [-100888, -100777]


@pytest.mark.asyncio
async def test_failed_channel_lookup_logs_the_error(admin, caplog):
    await _finished_log(admin, ago=timedelta(minutes=1), message_id=1, channel=-100888)
    ctx = _ctx()
    with caplog.at_level("WARNING"):
        await _command_page(ctx)
    assert any("-100888" in r.getMessage() and "Chat not found" in r.getMessage() for r in caplog.records)
