from __future__ import annotations
import logging
import shutil

from telegram import Bot

from bot.database.connection import get_db
from bot.database.crud import create_post_log, get_setting, update_post_log_status
from bot.services.downloader import check_disk_space, download_video
from bot.services.publisher import publish_video
from bot.services.task_queue import Ticket, TaskQueue

logger = logging.getLogger(__name__)


_SAVED_FOR_RESTART = "Bot is restarting. Your task is saved and will start automatically after the restart."


async def _read_limits(db) -> tuple[int, int]:
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    max_file_size = int(await get_setting(db, "max_file_size_mb") or "2000")
    return max_concurrent, max_file_size


async def download_and_publish(
    *, bot: Bot, task_queue: TaskQueue, user_chat_id: int,
    user_id: int, source_url: str, caption: str, channel_chat_id: int,
    max_resolution: int = 1080,
) -> None:
    db = await get_db()
    max_concurrent, max_file_size = await _read_limits(db)
    task = dict(
        user_id=user_id, user_chat_id=user_chat_id, source_url=source_url,
        caption=caption, channel_chat_id=channel_chat_id, max_resolution=max_resolution,
    )
    ticket = await task_queue.submit(task, max_slots=max_concurrent)
    await _process(
        bot=bot, db=db, task_queue=task_queue, ticket=ticket, task=task,
        max_concurrent=max_concurrent, max_file_size=max_file_size,
    )


async def restore_pending_tasks(application) -> int:
    """启动后调用：恢复上次退出时还在排队的任务，返回恢复的数量。

    必须在 Application.start() 之后调用，create_task 创建的任务才会在 stop() 时被等待。
    """
    db = await get_db()
    bot = application.bot
    task_queue: TaskQueue = application.bot_data["task_queue"]
    max_concurrent, max_file_size = await _read_limits(db)

    resumed, interrupted = await task_queue.restore(max_slots=max_concurrent)
    for row in interrupted:
        try:
            await bot.send_message(
                chat_id=row["user_chat_id"],
                text=f"Your task was interrupted by a bot restart and could not be resumed. Please resend the link:\n{row['source_url']}",
            )
        except Exception:
            logger.warning("Failed to notify user %d about interrupted task %d", row["user_chat_id"], row["id"])

    for row, ticket in resumed:
        application.create_task(_process(
            bot=bot, db=db, task_queue=task_queue, ticket=ticket, task=row,
            max_concurrent=max_concurrent, max_file_size=max_file_size, restored=True,
        ))
    return len(resumed)


async def _process(
    *, bot: Bot, db, task_queue: TaskQueue, ticket: Ticket, task: dict,
    max_concurrent: int, max_file_size: int, restored: bool = False,
) -> None:
    user_chat_id = task["user_chat_id"]
    if ticket.position > 0:
        prefix = "Bot restarted. " if restored else f"All {max_concurrent} download slots are busy. "
        await bot.send_message(
            chat_id=user_chat_id,
            text=f"{prefix}Your task is queued at position {ticket.position} and will start automatically.",
        )
    async with task_queue.slot(ticket) as ok:
        if not ok:
            # 正在关闭：pending 记录保留，重启后恢复
            await bot.send_message(chat_id=user_chat_id, text=_SAVED_FOR_RESTART)
            return
        if restored and ticket.position == 0:
            await bot.send_message(chat_id=user_chat_id, text=f"Bot restarted. Resuming your task:\n{task['source_url']}")
        await _run_task(
            bot=bot, db=db, task_queue=task_queue, task_id=ticket.task_id,
            user_chat_id=user_chat_id, user_id=task["user_id"], source_url=task["source_url"],
            caption=task["caption"], channel_chat_id=task["channel_chat_id"],
            max_resolution=task["max_resolution"], max_concurrent=max_concurrent, max_file_size=max_file_size,
        )


async def _run_task(
    *, bot: Bot, db, task_queue: TaskQueue, task_id: int,
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
            task_queue.update_progress(task_id, pct, "downloading")
        elif d.get("status") == "finished":
            task_queue.update_progress(task_id, 100.0, "processing")

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

        task_queue.update_progress(task_id, 100.0, "publishing")
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
        # 槽位由外层 _process 的 slot() 统一释放
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
