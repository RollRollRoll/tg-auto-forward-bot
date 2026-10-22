from __future__ import annotations
import logging
import shutil

from telegram import Bot

from bot.database.connection import get_db
from bot.database.crud import create_post_log, get_setting, update_post_log_status
from bot.services.downloader import DownloadSlotManager, check_disk_space, download_video
from bot.services.publisher import publish_video

logger = logging.getLogger(__name__)


async def download_and_publish(
    *, bot: Bot, slot_manager: DownloadSlotManager, user_chat_id: int,
    user_id: int, source_url: str, caption: str, channel_chat_id: int,
    max_resolution: int = 1080,
) -> None:
    db = await get_db()
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    max_file_size = int(await get_setting(db, "max_file_size_mb") or "2000")

    entry = await slot_manager.enqueue(max_concurrent, url=source_url, user_id=user_id)
    if entry is None:
        await bot.send_message(chat_id=user_chat_id, text="Bot is shutting down, please resend your link later.")
        return
    task_id, position = entry

    try:
        if position > 0:
            await bot.send_message(
                chat_id=user_chat_id,
                text=f"All {max_concurrent} download slots are busy. Your task is queued at position {position} and will start automatically.",
            )
            if not await slot_manager.wait_for_slot(task_id):
                await bot.send_message(chat_id=user_chat_id, text="Bot is restarting, the queued task was cancelled. Please resend your link.")
                return

        await _run_task(
            bot=bot, db=db, slot_manager=slot_manager, task_id=task_id,
            user_chat_id=user_chat_id, user_id=user_id, source_url=source_url,
            caption=caption, channel_chat_id=channel_chat_id,
            max_resolution=max_resolution, max_concurrent=max_concurrent, max_file_size=max_file_size,
        )
    finally:
        await slot_manager.release_slot(task_id)


async def _run_task(
    *, bot: Bot, db, slot_manager: DownloadSlotManager, task_id: int,
    user_chat_id: int, user_id: int, source_url: str, caption: str,
    channel_chat_id: int, max_resolution: int, max_concurrent: int, max_file_size: int,
) -> None:
    # 拿到槽位后再检查磁盘：排队期间剩余空间可能已经变化
    has_space, free_mb = check_disk_space(max_concurrent, max_file_size)
    if not has_space:
        await bot.send_message(chat_id=user_chat_id, text="Insufficient disk space, please try again later.")
        return

    def _progress_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total > 0 else 0.0
            slot_manager.update_progress(task_id, pct, "downloading")
        elif d.get("status") == "finished":
            slot_manager.update_progress(task_id, 100.0, "processing")

    log_id = None
    tmp_dir = None
    try:
        log_id = await create_post_log(
            db, admin_user_id=user_id, source_url=source_url,
            channel_chat_id=channel_chat_id, caption=caption,
        )

        result = await download_video(source_url, max_resolution=max_resolution, progress_callback=_progress_hook)
        tmp_dir = result["tmp_dir"]

        if result["file_size_mb"] > max_file_size:
            await update_post_log_status(db, log_id, status="failed", error_message=f"File too large: {result['file_size_mb']:.1f} MB")
            await bot.send_message(chat_id=user_chat_id, text=f"Video too large ({result['file_size_mb']:.1f} MB), exceeds limit ({max_file_size} MB).")
            return

        slot_manager.update_progress(task_id, 100.0, "publishing")
        await update_post_log_status(db, log_id, status="publishing")
        message_id = await publish_video(
            bot, channel_chat_id=channel_chat_id, file_path=result["file_path"],
            caption=caption, duration=result.get("duration"), width=result.get("width"), height=result.get("height"),
        )

        if message_id:
            await update_post_log_status(db, log_id, status="done", message_id=message_id)
            await bot.send_message(chat_id=user_chat_id, text=f"Published successfully! (message #{message_id})")
        else:
            await update_post_log_status(db, log_id, status="failed", error_message="Failed to send to channel")
            await bot.send_message(chat_id=user_chat_id, text="Send failed, please check Bot permissions.")

    except Exception as e:
        logger.exception("Pipeline error for %s", source_url)
        if log_id:
            await update_post_log_status(db, log_id, status="failed", error_message=str(e))
        await bot.send_message(chat_id=user_chat_id, text=f"Download failed: {e}")
    finally:
        # 槽位由外层 download_and_publish 统一释放
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
