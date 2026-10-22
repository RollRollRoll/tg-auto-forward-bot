import asyncio
from contextlib import ExitStack

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.services.pipeline import download_and_publish
from bot.services.downloader import DownloadSlotManager


async def _make_get_setting(overrides=None):
    defaults = {"max_concurrent_downloads": "1", "max_file_size_mb": "2000", "max_resolution": "1080"}
    if overrides:
        defaults.update(overrides)
    async def _get_setting(db, key):
        return defaults.get(key)
    return _get_setting


_RESULT = {
    "file_path": "/tmp/test.mp4", "tmp_dir": "/tmp/testdir",
    "duration": 10, "width": 1920, "height": 1080, "file_size_mb": 25.0,
}


def _success_patches(get_setting_fn):
    return [
        patch("bot.services.pipeline.get_db", new_callable=AsyncMock),
        patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn),
        patch("bot.services.pipeline.check_disk_space", return_value=(True, 5000)),
        patch("bot.services.pipeline.create_post_log", new_callable=AsyncMock, return_value=1),
        patch("bot.services.pipeline.update_post_log_status", new_callable=AsyncMock),
        patch("bot.services.pipeline.download_video", new_callable=AsyncMock, return_value=_RESULT),
        patch("bot.services.pipeline.publish_video", new_callable=AsyncMock, return_value=999),
        patch("shutil.rmtree"),
    ]


def _run(bot, slot_mgr):
    return download_and_publish(
        bot=bot, slot_manager=slot_mgr, user_chat_id=111, user_id=111,
        source_url="https://x.com/u/status/1", caption="test", channel_chat_id=-100123,
    )


@pytest.mark.asyncio
async def test_pipeline_queues_when_no_slot_then_runs():
    slot_mgr = DownloadSlotManager()
    busy_id, _ = await slot_mgr.enqueue(1)
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    with ExitStack() as stack:
        for p in _success_patches(get_setting_fn):
            stack.enter_context(p)
        task = asyncio.create_task(_run(bot, slot_mgr))
        # 等任务进入队列并发出排队通知
        for _ in range(50):
            if slot_mgr.queued_count:
                break
            await asyncio.sleep(0)
        assert slot_mgr.queued_count == 1
        assert "queued at position 1" in str(bot.send_message.call_args_list[0])
        assert not task.done()

        await slot_mgr.release_slot(busy_id)
        await asyncio.wait_for(task, 1)

    assert "success" in str(bot.send_message.call_args).lower()
    assert slot_mgr.active_count == 0
    assert slot_mgr.queued_count == 0


@pytest.mark.asyncio
async def test_pipeline_queued_task_cancelled_on_close():
    slot_mgr = DownloadSlotManager()
    await slot_mgr.enqueue(1)
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    with ExitStack() as stack:
        for p in _success_patches(get_setting_fn):
            stack.enter_context(p)
        download_mock = stack.enter_context(
            patch("bot.services.pipeline.download_video", new_callable=AsyncMock, return_value=_RESULT)
        )
        task = asyncio.create_task(_run(bot, slot_mgr))
        for _ in range(50):
            if slot_mgr.queued_count:
                break
            await asyncio.sleep(0)
        slot_mgr.close()
        await asyncio.wait_for(task, 1)

    download_mock.assert_not_awaited()
    assert "restarting" in str(bot.send_message.call_args).lower()
    assert slot_mgr.queued_count == 0


@pytest.mark.asyncio
async def test_pipeline_rejects_after_close():
    slot_mgr = DownloadSlotManager()
    slot_mgr.close()
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    with patch("bot.services.pipeline.get_db", new_callable=AsyncMock), \
         patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn):
        await _run(bot, slot_mgr)
    bot.send_message.assert_awaited_once()
    assert "shutting down" in str(bot.send_message.call_args).lower()


@pytest.mark.asyncio
async def test_pipeline_rejects_when_no_disk_space():
    slot_mgr = DownloadSlotManager()
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    with patch("bot.services.pipeline.get_db", new_callable=AsyncMock), \
         patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn), \
         patch("bot.services.pipeline.check_disk_space", return_value=(False, 100)):
        await download_and_publish(
            bot=bot, slot_manager=slot_mgr, user_chat_id=111, user_id=111,
            source_url="https://x.com/u/status/1", caption="test", channel_chat_id=-100123,
        )
    assert "disk" in str(bot.send_message.call_args).lower()
    assert slot_mgr.active_count == 0


@pytest.mark.asyncio
async def test_pipeline_rejects_oversized_file():
    slot_mgr = DownloadSlotManager()
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting({"max_file_size_mb": "50"})
    mock_result = {
        "file_path": "/tmp/test.mp4", "tmp_dir": "/tmp/testdir",
        "duration": 10, "width": 1920, "height": 1080, "file_size_mb": 100.0,
    }
    with patch("bot.services.pipeline.get_db", new_callable=AsyncMock), \
         patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn), \
         patch("bot.services.pipeline.check_disk_space", return_value=(True, 5000)), \
         patch("bot.services.pipeline.create_post_log", new_callable=AsyncMock, return_value=1), \
         patch("bot.services.pipeline.update_post_log_status", new_callable=AsyncMock), \
         patch("bot.services.pipeline.download_video", new_callable=AsyncMock, return_value=mock_result), \
         patch("shutil.rmtree"):
        await download_and_publish(
            bot=bot, slot_manager=slot_mgr, user_chat_id=111, user_id=111,
            source_url="https://x.com/u/status/1", caption="test", channel_chat_id=-100123,
        )
    assert "too large" in str(bot.send_message.call_args).lower()


@pytest.mark.asyncio
async def test_pipeline_success_path():
    slot_mgr = DownloadSlotManager()
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    mock_result = {
        "file_path": "/tmp/test.mp4", "tmp_dir": "/tmp/testdir",
        "duration": 10, "width": 1920, "height": 1080, "file_size_mb": 25.0,
    }
    with patch("bot.services.pipeline.get_db", new_callable=AsyncMock), \
         patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn), \
         patch("bot.services.pipeline.check_disk_space", return_value=(True, 5000)), \
         patch("bot.services.pipeline.create_post_log", new_callable=AsyncMock, return_value=1), \
         patch("bot.services.pipeline.update_post_log_status", new_callable=AsyncMock), \
         patch("bot.services.pipeline.download_video", new_callable=AsyncMock, return_value=mock_result), \
         patch("bot.services.pipeline.publish_video", new_callable=AsyncMock, return_value=999), \
         patch("shutil.rmtree"):
        await download_and_publish(
            bot=bot, slot_manager=slot_mgr, user_chat_id=111, user_id=111,
            source_url="https://x.com/u/status/1", caption="test", channel_chat_id=-100123,
        )
    assert "success" in str(bot.send_message.call_args).lower()
    assert slot_mgr.active_count == 0


@pytest.mark.asyncio
async def test_pipeline_download_failure():
    slot_mgr = DownloadSlotManager()
    bot = AsyncMock()
    get_setting_fn = await _make_get_setting()
    with patch("bot.services.pipeline.get_db", new_callable=AsyncMock), \
         patch("bot.services.pipeline.get_setting", side_effect=get_setting_fn), \
         patch("bot.services.pipeline.check_disk_space", return_value=(True, 5000)), \
         patch("bot.services.pipeline.create_post_log", new_callable=AsyncMock, return_value=1), \
         patch("bot.services.pipeline.update_post_log_status", new_callable=AsyncMock), \
         patch("bot.services.pipeline.download_video", new_callable=AsyncMock, side_effect=RuntimeError("yt-dlp error")):
        await download_and_publish(
            bot=bot, slot_manager=slot_mgr, user_chat_id=111, user_id=111,
            source_url="https://x.com/u/status/1", caption="test", channel_chat_id=-100123,
        )
    assert "failed" in str(bot.send_message.call_args).lower()
    assert slot_mgr.active_count == 0
