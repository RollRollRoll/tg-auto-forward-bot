import asyncio
from contextlib import ExitStack

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.database.crud import create_pending_task, list_pending_tasks, set_setting
from bot.services.task_queue import SqliteTaskStore, TaskQueue
from bot.services.pipeline import download_and_publish, restore_pending_tasks

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


def _run(bot, slot_mgr, url="https://x.com/u/status/1"):
    return download_and_publish(
        bot=bot, task_queue=slot_mgr, user_chat_id=111, user_id=111,
        source_url=url, caption="test", channel_chat_id=-100123,
    )


async def _until(cond, timeout=2.0):
    # 轮询直到条件满足；aiosqlite 在后台线程执行，需要真正等待而不是只让出事件循环
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def _texts(bot):
    return [c.kwargs["text"] for c in bot.send_message.call_args_list]


@pytest.mark.asyncio
async def test_pipeline_success_path(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = AsyncMock()
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    assert "success" in _texts(bot)[-1].lower()
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []
    cursor = await db.execute("SELECT status, message_id FROM post_logs")
    assert [tuple(r) for r in await cursor.fetchall()] == [("done", 999)]


@pytest.mark.asyncio
async def test_pipeline_rejects_when_no_disk_space(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = AsyncMock()
    with ExitStack() as stack:
        for p in _patches(disk_ok=False):
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    assert "disk" in _texts(bot)[-1].lower()
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_pipeline_rejects_oversized_file(db):
    await set_setting(db, "max_file_size_mb", "50")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = AsyncMock()
    big = AsyncMock(return_value={**_RESULT, "file_size_mb": 100.0})
    with ExitStack() as stack:
        for p in _patches(download=big):
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    assert "too large" in _texts(bot)[-1].lower()
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_pipeline_download_failure(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    bot = AsyncMock()
    failing = AsyncMock(side_effect=RuntimeError("yt-dlp error"))
    with ExitStack() as stack:
        for p in _patches(download=failing):
            stack.enter_context(p)
        await _run(bot, slot_mgr)
    assert "failed" in _texts(bot)[-1].lower()
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []

@pytest.mark.asyncio
async def test_queued_task_notifies_position_then_runs(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    blocker = await slot_mgr.submit(
        dict(user_id=1, user_chat_id=1, source_url="https://busy", caption="",
             channel_chat_id=-1, max_resolution=720), max_slots=1)
    bot = AsyncMock()
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        task = asyncio.create_task(_run(bot, slot_mgr))
        await _until(lambda: bot.send_message.await_count == 1)
        assert "queued at position 1" in _texts(bot)[0]
        async with slot_mgr.slot(blocker):
            pass
        await asyncio.wait_for(task, 1)
    assert "success" in _texts(bot)[-1].lower()


@pytest.mark.asyncio
async def test_task_submitted_after_close_tells_user_it_is_saved(db):
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    slot_mgr.close()
    bot = AsyncMock()
    await _run(bot, slot_mgr)
    assert "saved" in _texts(bot)[-1]
    assert [r["status"] for r in await list_pending_tasks(db)] == ["queued"]


def _fake_application(slot_mgr):
    app = MagicMock()
    app.bot = AsyncMock()
    app.bot_data = {"task_queue": slot_mgr}
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
async def test_restore_resumes_queued_tasks_in_order(db):
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
        await asyncio.wait_for(asyncio.gather(*app.tasks), 1)

    assert [c.args[0] for c in download.await_args_list] == ["https://a", "https://b"]
    assert download.await_args_list[0].kwargs["max_resolution"] == 720
    texts = [c.kwargs["text"] for c in app.bot.send_message.call_args_list]
    assert any("Resuming your task" in t and "https://a" in t for t in texts)
    assert any("Bot restarted. Your task is queued at position 1" in t for t in texts)
    assert await list_pending_tasks(db) == []
    assert slot_mgr.get_active_tasks() == []


@pytest.mark.asyncio
async def test_restore_does_not_rerun_interrupted_running_task(db):
    await _add_pending(db, "https://was-running", running=True)
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    app = _fake_application(slot_mgr)
    download = AsyncMock(return_value=_RESULT)
    with ExitStack() as stack:
        for p in _patches(download=download):
            stack.enter_context(p)
        assert await restore_pending_tasks(app) == 0

    download.assert_not_awaited()
    text = app.bot.send_message.call_args.kwargs["text"]
    assert "interrupted" in text and "https://was-running" in text
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_restore_survives_notification_failure(db):
    await _add_pending(db, "https://was-running", running=True)
    await _add_pending(db, "https://queued")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    app = _fake_application(slot_mgr)
    # 第一条通知（中断提醒）失败，后面的任务仍要恢复
    app.bot.send_message.side_effect = [RuntimeError("blocked"), None, None]
    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        assert await restore_pending_tasks(app) == 1
        await asyncio.wait_for(asyncio.gather(*app.tasks), 1)
    assert await list_pending_tasks(db) == []


@pytest.mark.asyncio
async def test_failed_queue_notification_does_not_leak_slot(db):
    await set_setting(db, "max_concurrent_downloads", "1")
    slot_mgr = TaskQueue(SqliteTaskStore(db))
    blocker = await slot_mgr.submit(
        dict(user_id=1, user_chat_id=1, source_url="https://busy", caption="",
             channel_chat_id=-1, max_resolution=720), max_slots=1)
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("bot was blocked by the user")
    with pytest.raises(RuntimeError):
        await _run(bot, slot_mgr)

    async with slot_mgr.slot(blocker):
        pass
    # 通知失败的任务不能占着唯一的 slot，pending 记录也要删掉，否则重启后会被重跑
    assert slot_mgr.get_active_tasks() == []
    assert await list_pending_tasks(db) == []
