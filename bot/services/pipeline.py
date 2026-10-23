from __future__ import annotations
import logging
import shutil

from telegram import Bot

from bot.database.connection import get_db
from bot.database.crud import (
    create_pending_task, create_post_log, delete_pending_task, get_setting,
    list_pending_tasks, mark_pending_task_running, update_post_log_status,
)
from bot.services.downloader import DownloadSlotManager, check_disk_space, download_video
from bot.services.publisher import publish_video

logger = logging.getLogger(__name__)


_SAVED_FOR_RESTART = "Bot is restarting. Your task is saved and will start automatically after the restart."


async def _read_limits(db) -> tuple[int, int]:
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    max_file_size = int(await get_setting(db, "max_file_size_mb") or "2000")
    return max_concurrent, max_file_size


async def download_and_publish(
    *, bot: Bot, slot_manager: DownloadSlotManager, user_chat_id: int,
    user_id: int, source_url: str, caption: str, channel_chat_id: int,
    max_resolution: int = 1080,
) -> None:
    db = await get_db()
    max_concurrent, max_file_size = await _read_limits(db)

    # 先落库再排队：进程重启后可以从 pending_tasks 恢复
    task_id = await create_pending_task(
        db, user_id=user_id, user_chat_id=user_chat_id, source_url=source_url,
        caption=caption, channel_chat_id=channel_chat_id, max_resolution=max_resolution,
    )
    entry = await slot_manager.enqueue(max_concurrent, task_id=task_id, url=source_url, user_id=user_id)
    await _process(
        bot=bot, db=db, slot_manager=slot_manager, entry=entry, task_id=task_id,
        user_chat_id=user_chat_id, user_id=user_id, source_url=source_url,
        caption=caption, channel_chat_id=channel_chat_id, max_resolution=max_resolution,
        max_concurrent=max_concurrent, max_file_size=max_file_size,
    )


async def restore_pending_tasks(application) -> int:
    """启动后调用：恢复上次退出时还在排队的任务，返回恢复的数量。

    上次退出时已在下载的任务（status=running）无法确认是否已发到频道，
    为避免重复发布，不自动重跑，而是通知用户重新发送链接。
    必须在 Application.start() 之后调用，create_task 创建的任务才会在 stop() 时被等待。
    """
    db = await get_db()
    bot = application.bot
    slot_manager: DownloadSlotManager = application.bot_data["slot_manager"]
    max_concurrent, max_file_size = await _read_limits(db)

    restored = 0
    for row in await list_pending_tasks(db):
        if row["status"] == "running":
            await delete_pending_task(db, row["id"])
            try:
                await bot.send_message(
                    chat_id=row["user_chat_id"],
                    text=f"Your task was interrupted by a bot restart and could not be resumed. Please resend the link:\n{row['source_url']}",
                )
            except Exception:
                logger.warning("Failed to notify user %d about interrupted task %d", row["user_chat_id"], row["id"])
            continue

        # 按入队顺序依次登记，保证恢复后的排队顺序与重启前一致
        entry = await slot_manager.enqueue(
            max_concurrent, task_id=row["id"], url=row["source_url"], user_id=row["user_id"],
        )
        application.create_task(_process(
            bot=bot, db=db, slot_manager=slot_manager, entry=entry, task_id=row["id"],
            user_chat_id=row["user_chat_id"], user_id=row["user_id"], source_url=row["source_url"],
            caption=row["caption"], channel_chat_id=row["channel_chat_id"],
            max_resolution=row["max_resolution"], max_concurrent=max_concurrent,
            max_file_size=max_file_size, restored=True,
        ))
        restored += 1
    return restored


async def _process(
    *, bot: Bot, db, slot_manager: DownloadSlotManager, entry: tuple[int, int] | None,
    task_id: int, user_chat_id: int, user_id: int, source_url: str, caption: str,
    channel_chat_id: int, max_resolution: int, max_concurrent: int, max_file_size: int,
    restored: bool = False,
) -> None:
    if entry is None:
        # 正在关闭，不再接收任务；pending_tasks 里的记录保留，重启后恢复
        await bot.send_message(chat_id=user_chat_id, text=_SAVED_FOR_RESTART)
        return
    _, position = entry

    keep_pending = False
    try:
        if position > 0:
            prefix = "Bot restarted. " if restored else f"All {max_concurrent} download slots are busy. "
            await bot.send_message(
                chat_id=user_chat_id,
                text=f"{prefix}Your task is queued at position {position} and will start automatically.",
            )
            if not await slot_manager.wait_for_slot(task_id):
                keep_pending = True
                await bot.send_message(chat_id=user_chat_id, text=_SAVED_FOR_RESTART)
                return
        elif restored:
            await bot.send_message(chat_id=user_chat_id, text=f"Bot restarted. Resuming your task:\n{source_url}")

        await mark_pending_task_running(db, task_id)
        await _run_task(
            bot=bot, db=db, slot_manager=slot_manager, task_id=task_id,
            user_chat_id=user_chat_id, user_id=user_id, source_url=source_url,
            caption=caption, channel_chat_id=channel_chat_id,
            max_resolution=max_resolution, max_concurrent=max_concurrent, max_file_size=max_file_size,
        )
    finally:
        await slot_manager.release_slot(task_id)
        if not keep_pending:
            await delete_pending_task(db, task_id)


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
