from __future__ import annotations
import asyncio
import logging
import shutil
from dataclasses import dataclass

from telegram import Bot

from bot.database.connection import get_db
from bot.database.crud import create_post_log, get_setting, list_channels, update_post_log_status
from bot.services.downloader import check_disk_space, download_video
from bot.services.publisher import publish_video
from bot.services.task_queue import Task, Ticket, TaskQueue
from bot.ui.channels import channel_info
from bot.ui.markup import post_link
from bot.ui.sender import RichSender
from bot.ui.status import ChatEditLimiter, StatusReporter

logger = logging.getLogger(__name__)


_SAVED_FOR_RESTART = "Bot is restarting. Your task is saved and will start automatically after the restart."


@dataclass(frozen=True)
class Published:
    message_id: int


@dataclass(frozen=True)
class TooLarge:
    size_mb: float
    limit_mb: int


@dataclass(frozen=True)
class NoDiskSpace:
    pass


@dataclass(frozen=True)
class SendFailed:
    pass


@dataclass(frozen=True)
class Failed:
    error: str


# 一次 Task 执行的最终结果，post log 的最终状态和用户通知都由它决定
Outcome = Published | TooLarge | NoDiskSpace | SendFailed | Failed


def outcome_message(outcome: Outcome) -> str:
    """把执行结果转成发给用户的通知文案。"""
    match outcome:
        case Published(message_id):
            return f"Published successfully! (message #{message_id})"
        case TooLarge(size_mb, limit_mb):
            return f"Video too large ({size_mb:.1f} MB), exceeds limit ({limit_mb} MB)."
        case NoDiskSpace():
            return "Insufficient disk space, please try again later."
        case SendFailed():
            return "Send failed, please check Bot permissions."
        case Failed(error):
            return f"Download failed: {error}"
    raise TypeError(f"Unknown outcome: {outcome!r}")


async def _read_limits(db) -> tuple[int, int]:
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    max_file_size = int(await get_setting(db, "max_file_size_mb") or "2000")
    return max_concurrent, max_file_size


async def download_and_publish(
    *, bot: Bot, task_queue: TaskQueue, task: Task, limiter: ChatEditLimiter,
) -> None:
    db = await get_db()
    max_concurrent, max_file_size = await _read_limits(db)
    ticket = await task_queue.submit(task, max_slots=max_concurrent)
    await _process(
        bot=bot, db=db, task_queue=task_queue, ticket=ticket, task=task,
        max_concurrent=max_concurrent, max_file_size=max_file_size,
        limiter=limiter,
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
    for task in interrupted:
        try:
            await bot.send_message(
                chat_id=task.user_chat_id,
                text=f"Your task was interrupted by a bot restart and could not be resumed. Please resend the link:\n{task.source_url}",
            )
        except Exception:
            logger.warning("Failed to notify user %d about interrupted task %s", task.user_chat_id, task.source_url)

    limiter = application.bot_data["status_limiter"]
    for task, ticket in resumed:
        application.create_task(_process(
            bot=bot, db=db, task_queue=task_queue, ticket=ticket, task=task,
            max_concurrent=max_concurrent, max_file_size=max_file_size, limiter=limiter, restored=True,
        ))
    return len(resumed)


async def _process(
    *, bot: Bot, db, task_queue: TaskQueue, ticket: Ticket, task: Task,
    max_concurrent: int, max_file_size: int, limiter: ChatEditLimiter, restored: bool = False,
) -> None:
    user_chat_id = task.user_chat_id
    if ticket.closed:
        # 提交时已在关闭：记录已落库，重启后恢复
        await bot.send_message(chat_id=user_chat_id, text=_SAVED_FOR_RESTART)
        return

    channel_title, channel_username = await channel_info(bot, await list_channels(db), task.channel_chat_id)
    status = StatusReporter(
        RichSender(bot), limiter, chat_id=user_chat_id, url=task.source_url,
        channel_title=channel_title, resolution=task.max_resolution, restored=restored,
    )
    status.start(ticket.position)

    async with task_queue.slot(ticket, on_position_changed=status.position_changed) as ok:
        if not ok:
            # 正在关闭：pending 记录保留，重启后恢复
            await status.wait_idle()
            await bot.send_message(chat_id=user_chat_id, text=_SAVED_FOR_RESTART)
            return
        status.running()
        outcome = await execute_task(
            bot=bot, db=db, task_queue=task_queue, task_id=ticket.task_id, task=task,
            max_concurrent=max_concurrent, max_file_size=max_file_size, status=status,
        )

    # 结果通知发不出去（如用户屏蔽了 bot）时只记日志：post log 已是最终状态，不受影响
    if isinstance(outcome, Published):
        url = post_link(task.channel_chat_id, channel_username, outcome.message_id)
        await status.finished(success=True, post_url=url)
    else:
        await status.finished(success=False, detail=outcome_message(outcome))


async def execute_task(
    *, bot: Bot, db, task_queue: TaskQueue, task_id: int, task: Task,
    max_concurrent: int, max_file_size: int, status: StatusReporter | None = None,
) -> Outcome:
    """下载并发布一个 Task，过程中更新 post log 和 status message，返回最终结果。不向外抛异常。"""
    # 拿到槽位后再检查磁盘：排队期间剩余空间可能已经变化
    has_space, _ = check_disk_space(max_concurrent, max_file_size)
    if not has_space:
        return NoDiskSpace()

    loop = asyncio.get_running_loop()

    def _report(d: dict, total: float) -> None:
        # 在事件循环里执行：StatusReporter 不是线程安全的
        if d.get("status") == "downloading":
            status.downloading()
            status.progress(downloaded=d.get("downloaded_bytes", 0), total=total or None,
                            speed=d.get("speed"), eta=d.get("eta"))
        elif d.get("status") == "finished":
            status.converting()

    def _progress_hook(d: dict) -> None:
        # yt-dlp 在下载线程里调用
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total > 0 else 0.0
            task_queue.update_progress(task_id, pct, "downloading")
        elif d.get("status") == "finished":
            task_queue.update_progress(task_id, 100.0, "processing")
        if status is not None:
            loop.call_soon_threadsafe(_report, d, total)

    log_id = None
    tmp_dir = None
    try:
        log_id = await create_post_log(
            db, admin_user_id=task.user_id, source_url=task.source_url,
            channel_chat_id=task.channel_chat_id, caption=task.caption,
        )

        result = await download_video(task.source_url, max_resolution=task.max_resolution, progress_callback=_progress_hook)
        tmp_dir = result["tmp_dir"]

        if result["file_size_mb"] > max_file_size:
            await update_post_log_status(db, log_id, status="failed", error_message=f"File too large: {result['file_size_mb']:.1f} MB")
            return TooLarge(size_mb=result["file_size_mb"], limit_mb=max_file_size)

        task_queue.update_progress(task_id, 100.0, "publishing")
        if status is not None:
            status.uploading()
        await update_post_log_status(db, log_id, status="publishing")
        message_id = await publish_video(
            bot, channel_chat_id=task.channel_chat_id, file_path=result["file_path"],
            caption=task.caption, duration=result.get("duration"), width=result.get("width"), height=result.get("height"),
        )

        if message_id:
            await update_post_log_status(db, log_id, status="done", message_id=message_id)
            return Published(message_id=message_id)
        await update_post_log_status(db, log_id, status="failed", error_message="Failed to send to channel")
        return SendFailed()

    except Exception as e:
        logger.exception("Pipeline error for %s", task.source_url)
        if log_id:
            await update_post_log_status(db, log_id, status="failed", error_message=str(e))
        return Failed(error=str(e))
    finally:
        # 槽位由外层 _process 的 slot() 统一释放
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
