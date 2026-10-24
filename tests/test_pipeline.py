import asyncio
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.database.crud import add_channel, create_pending_task, list_pending_tasks, set_setting
from bot.services.task_queue import SqliteTaskStore, Task, TaskQueue
from bot.ui.status import ChatEditLimiter
from bot.services.pipeline import (
    Failed, NoDiskSpace, Published, SendFailed, TooLarge,
    download_and_publish, execute_task, outcome_message, restore_pending_tasks,
)
from tests.fakebot import FakeBot

# 这些测试使用 conftest 里的内存 SQLite（db fixture），pending_tasks / post_logs 都是真实读写

_RESULT = {
    "file_path": "/tmp/test.mp4", "tmp_dir": "/tmp/testdir",
    "duration": 10, "width": 1920, "height": 1080, "file_size_mb": 25.0,
}


def _patches(*, disk_ok=True, download=None, publish_id=999):
    download = download or AsyncMock(return_value=_RESULT)
    return [
        patch("bot.services.pipeline.check_disk_space", return_value=(disk_ok, 5000)),
        patch("bot.services.pipeline.download_video", download),
        patch("bot.services.pipeline.publish_video", new_callable=AsyncMock, return_value=publish_id),
        patch("shutil.rmtree"),
    ]


def _run(bot, slot_mgr, url="https://x.com/u/status/1", channel=-100123):
    # 测试里不限速：限速规则由 test_status.py 用假时钟单独验证
    return download_and_publish(bot=bot, task_queue=slot_mgr, limiter=ChatEditLimiter(interval=0), task=Task(
        user_id=111, user_chat_id=111, source_url=url, caption="test",
        channel_chat_id=channel, max_resolution=1080,
    ))


async def _until(cond, timeout=2.0):
    # 轮询直到条件满足；aiosqlite 在后台线程执行，需要真正等待而不是只让出事件循环
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def _bot():
    bot = FakeBot()
    bot.chat_info[-100123] = SimpleNamespace(username="demo", title="Demo")
    return bot


async def _add_channel(db):
    await add_channel(db, chat_id=-100123, title="Demo")


@pytest.mark.asyncio
async def test_pipeline_success_path(db):
    await _add_channel(db)
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = _bot()
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        await _run(bot, slot_mgr, url="https://x.com/u/status/1", channel=-100123)
    status, push = bot.messages(111)
    # 一个 Task 只有一条 status message，最后编辑成 Done 并带帖子链接
    assert status.format == "rich" and "Done" in status.text
    assert "https://x.com/u/status/1" in status.text and "Demo" in status.text
    assert [b["url"] for b in status.buttons] == ["https://t.me/demo/999"]
    assert push.text == "✅ Published to Demo"
    assert "Task accepted" not in " ".join(m.text for m in bot.messages(111))
    # NEG-004：不用流式草稿和 ephemeral 消息
    assert not [m for m in bot.methods() if m == "sendRichMessageDraft" or "Ephemeral" in m]
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []
    cursor = await db.execute("SELECT status, message_id FROM post_logs")
    assert [tuple(r) for r in await cursor.fetchall()] == [("done", 999)]


@pytest.mark.asyncio
async def test_private_channel_post_link_uses_internal_id(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = FakeBot()  # get_chat 失败：按私有频道拼链接，标题退回 chat_id
    with ExitStack() as stack:
        for p in _patches(publish_id=5):
            stack.enter_context(p)
        await _run(bot, slot_mgr, channel=-1001234567890)
    status, _ = bot.messages(111)
    assert [b["url"] for b in status.buttons] == ["https://t.me/c/1234567890/5"]


@pytest.mark.asyncio
async def test_queued_task_shows_position_then_runs(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    blocker = await slot_mgr.submit(
        Task(user_id=1, user_chat_id=1, source_url="https://busy", caption="",
             channel_chat_id=-1, max_resolution=720), max_slots=1)
    bot = _bot()
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        task = asyncio.create_task(_run(bot, slot_mgr))
        await _until(lambda: bot.messages(111))
        assert "Queued (#1)" in bot.last(111).text
        async with slot_mgr.slot(blocker):
            pass
        await asyncio.wait_for(task, 2)
    status, push = bot.messages(111)
    assert "Done" in status.text and push.text.startswith("✅")


@pytest.mark.asyncio
async def test_queued_position_moves_up_in_the_same_message(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    busy = Task(user_id=1, user_chat_id=1, source_url="https://busy", caption="", channel_chat_id=-1, max_resolution=720)
    blocker = await slot_mgr.submit(busy, max_slots=1)
    ahead = await slot_mgr.submit(busy, max_slots=1)
    bot = _bot()
    release_ahead = asyncio.Event()

    async def hold_ahead():
        async with slot_mgr.slot(ahead):
            await release_ahead.wait()

    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        holder = asyncio.create_task(hold_ahead())
        task = asyncio.create_task(_run(bot, slot_mgr))
        await _until(lambda: bot.messages(111))
        assert "Queued (#2)" in bot.last(111).text
        async with slot_mgr.slot(blocker):
            pass
        await _until(lambda: "Queued (#1)" in bot.last(111).text)
        assert len(bot.messages(111)) == 1
        release_ahead.set()
        await asyncio.wait_for(asyncio.gather(holder, task), 2)


@pytest.mark.asyncio
async def test_failure_outcome_is_shown_in_status_and_pushed(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = _bot()
    big = AsyncMock(return_value={**_RESULT, "file_size_mb": 3000.0})
    with ExitStack() as stack:
        for p in _patches(download=big):
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    status, push = bot.messages(111)
    assert "Failed" in status.text and "too large" in status.text and status.buttons == []
    assert "too large" in push.text


@pytest.mark.asyncio
async def test_close_keeps_queued_and_late_tasks_for_restart(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    blocker = await slot_mgr.submit(
        Task(user_id=1, user_chat_id=1, source_url="https://busy", caption="",
             channel_chat_id=-1, max_resolution=720), max_slots=1)
    bot = _bot()
    download = AsyncMock(return_value=_RESULT)
    with ExitStack() as stack:
        for p in _patches(download=download):
            stack.enter_context(p)
        queued = asyncio.create_task(_run(bot, slot_mgr, url="https://queued"))
        await _until(lambda: len(slot_mgr.get_queued_tasks()) == 1)
        slot_mgr.close()
        await asyncio.wait_for(queued, 1)
        await _run(bot, slot_mgr, url="https://late")
        # 正在运行的 blocker 正常结束，记录删除
        async with slot_mgr.slot(blocker):
            pass

    download.assert_not_awaited()
    texts = [m.text for m in bot.messages(111)]
    assert "saved" in texts[-1] and "saved" in texts[-2]
    rows = await list_pending_tasks(db)
    assert [(r["source_url"], r["status"]) for r in rows] == [
        ("https://queued", "queued"), ("https://late", "queued"),
    ]


def _fake_application(slot_mgr):
    app = MagicMock()
    app.bot = _bot()
    app.bot_data = {"task_queue": slot_mgr, "status_limiter": ChatEditLimiter(interval=0)}
    app.tasks = []
    app.create_task = lambda coro, **kw: app.tasks.append(asyncio.create_task(coro)) or app.tasks[-1]
    return app


async def _add_pending(db, url, *, running=False):
    task_id = await create_pending_task(
        db, user_id=111, user_chat_id=111, source_url=url,
        caption="c", channel_chat_id=-100123, max_resolution=720,
    )
    if running:
        await db.execute("UPDATE pending_tasks SET status = 'running' WHERE id = ?", (task_id,))
        await db.commit()
    return task_id


@pytest.mark.asyncio
async def test_restore_resumes_queued_tasks_in_order_with_new_status_messages(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    await _add_pending(db, "https://a")
    await _add_pending(db, "https://b")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    app = _fake_application(slot_mgr)
    download = AsyncMock(return_value=_RESULT)
    with ExitStack() as stack:
        for p in _patches(download=download):
            stack.enter_context(p)
        assert await restore_pending_tasks(app) == 2
        await asyncio.wait_for(asyncio.gather(*app.tasks), 2)

    assert [c.args[0] for c in download.await_args_list] == ["https://a", "https://b"]
    assert download.await_args_list[0].kwargs["max_resolution"] == 720
    statuses = [m for m in app.bot.messages(111) if m.format == "rich"]
    assert len(statuses) == 2
    assert all(m.text.startswith("🔄 Bot restarted, resuming") for m in statuses)
    assert {u for u in ("https://a", "https://b") if any(u in m.text for m in statuses)} == {"https://a", "https://b"}
    assert await list_pending_tasks(db) == []
    assert slot_mgr.get_active_tasks() == []


@pytest.mark.asyncio
async def test_restore_drops_interrupted_task_even_if_notification_fails(db):
    await _add_pending(db, "https://was-running", running=True)
    await _add_pending(db, "https://queued")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    app = _fake_application(slot_mgr)
    # 中断提醒发不出去，后面的任务仍要恢复
    app.bot.fail("sendMessage", RuntimeError("blocked"), times=1)
    download = AsyncMock(return_value=_RESULT)
    with ExitStack() as stack:
        for p in _patches(download=download):
            stack.enter_context(p)
        assert await restore_pending_tasks(app) == 1
        await asyncio.wait_for(asyncio.gather(*app.tasks), 2)

    first = app.bot.calls[0]
    assert first[1] == "sendMessage" and "interrupted" in first[2]["text"] and "https://was-running" in first[2]["text"]
    # 中断任务不重跑，避免重复发布
    assert [c.args[0] for c in download.await_args_list] == ["https://queued"]
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_status_message_failures_do_not_affect_the_task(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    blocker = await slot_mgr.submit(
        Task(user_id=1, user_chat_id=1, source_url="https://busy", caption="",
             channel_chat_id=-1, max_resolution=720), max_slots=1)
    bot = _bot()
    # 用户屏蔽了 bot：所有消息都发不出去
    for method in ("sendRichMessage", "sendMessage", "editMessageText"):
        bot.fail(method, RuntimeError("bot was blocked by the user"))
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        task = asyncio.create_task(_run(bot, slot_mgr))
        await _until(lambda: len(slot_mgr.get_queued_tasks()) == 1)
        async with slot_mgr.slot(blocker):
            pass
        await asyncio.wait_for(task, 2)

    cursor = await db.execute("SELECT status, message_id FROM post_logs")
    assert [tuple(r) for r in await cursor.fetchall()] == [("done", 999)]
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_progress_from_the_download_thread_reaches_the_status_message(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = _bot()
    MB = 1024 * 1024

    async def download(url, *, max_resolution, progress_callback):
        # yt-dlp 在线程里调用进度回调
        await asyncio.to_thread(progress_callback, {
            "status": "downloading", "downloaded_bytes": 50 * MB, "total_bytes": 100 * MB, "speed": MB, "eta": 50,
        })
        await asyncio.sleep(0.05)
        return _RESULT

    with ExitStack() as stack:
        for p in _patches(download=download):
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    edits = [kw["content"] for _, m, kw in bot.calls if m == "editMessageText"]
    assert any("Downloading" in c for c in edits)


async def _post_logs(db):
    cursor = await db.execute("SELECT status, message_id, error_message FROM post_logs")
    return [tuple(r) for r in await cursor.fetchall()]


def _execute(db, *, max_file_size=2000):
    task = Task(user_id=111, user_chat_id=111, source_url="https://x.com/u/status/1",
                caption="test", channel_chat_id=-100123, max_resolution=720)
    return execute_task(
        bot=AsyncMock(), db=db, task_queue=TaskQueue(SqliteTaskStore(db)), task_id=1, task=task,
        max_concurrent=2, max_file_size=max_file_size,
    )


@pytest.mark.asyncio
async def test_execute_task_rejects_oversized_file(db):
    big = AsyncMock(return_value={**_RESULT, "file_size_mb": 100.0})
    with ExitStack() as stack:
        for p in _patches(download=big):
            stack.enter_context(p)
        outcome = await _execute(db, max_file_size=50)
    assert outcome == TooLarge(size_mb=100.0, limit_mb=50)
    assert await _post_logs(db) == [("failed", None, "File too large: 100.0 MB")]


@pytest.mark.asyncio
async def test_execute_task_publishes_and_records_done(db):
    with ExitStack() as stack:
        for p in _patches(publish_id=999):
            stack.enter_context(p)
        outcome = await _execute(db)
    assert outcome == Published(message_id=999)
    assert await _post_logs(db) == [("done", 999, None)]


@pytest.mark.asyncio
async def test_execute_task_without_disk_space_skips_post_log(db):
    download = AsyncMock(return_value=_RESULT)
    with ExitStack() as stack:
        for p in _patches(disk_ok=False, download=download):
            stack.enter_context(p)
        outcome = await _execute(db)
    assert outcome == NoDiskSpace()
    download.assert_not_awaited()
    assert await _post_logs(db) == []


@pytest.mark.asyncio
async def test_execute_task_reports_channel_send_failure(db):
    with ExitStack() as stack:
        for p in _patches(publish_id=None):
            stack.enter_context(p)
        outcome = await _execute(db)
    assert outcome == SendFailed()
    assert await _post_logs(db) == [("failed", None, "Failed to send to channel")]


@pytest.mark.asyncio
async def test_execute_task_turns_exceptions_into_failed(db):
    failing = AsyncMock(side_effect=RuntimeError("yt-dlp error"))
    with ExitStack() as stack:
        for p in _patches(download=failing):
            stack.enter_context(p)
        outcome = await _execute(db)
    assert outcome == Failed(error="yt-dlp error")
    assert await _post_logs(db) == [("failed", None, "yt-dlp error")]


def _outcome_cases():
    return [
        (Published(message_id=42), "Published successfully! (message #42)"),
        (TooLarge(size_mb=100.04, limit_mb=50), "Video too large (100.0 MB), exceeds limit (50 MB)."),
        (NoDiskSpace(), "Insufficient disk space, please try again later."),
        (SendFailed(), "Send failed, please check Bot permissions."),
        (Failed(error="yt-dlp error"), "Download failed: yt-dlp error"),
    ]


@pytest.mark.parametrize("outcome,expected", _outcome_cases())
def test_outcome_message_keeps_existing_wording(outcome, expected):
    assert outcome_message(outcome) == expected
