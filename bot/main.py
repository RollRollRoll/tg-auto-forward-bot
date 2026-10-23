import logging
import os

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler

from bot.config import API_BASE_URL, BOT_TOKEN, DOWNLOAD_DIR, SUPER_ADMIN_ID
from bot.database.connection import close_db, init_db
from bot.database.crud import add_admin, fail_interrupted_post_logs, is_admin
from bot.database.models import create_tables
from bot.handlers.admin import (
    add_admin_handler, add_channel_handler, get_setting_handler,
    list_admins_handler, list_channels_handler, remove_admin_handler,
    remove_channel_handler, set_setting_handler, settings_handler,
    tasks_handler,
)
from bot.handlers.conversation import build_conversation_handler
from bot.handlers.menu import menu_callback_handler
from bot.handlers.start import help_handler, start_handler
from bot.services.downloader import cleanup_stale_files
from bot.services.pipeline import restore_pending_tasks
from bot.services.task_queue import SqliteTaskStore, TaskQueue

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


class BotApplication(Application):
    async def start(self) -> None:
        await super().start()
        # 必须在 start() 之后恢复：此时 create_task 创建的任务才会被 stop() 等待
        try:
            restored = await restore_pending_tasks(self)
        except Exception:
            # 恢复失败不影响 Bot 启动，未恢复的记录留在 pending_tasks 里，下次启动再试
            logger.exception("Failed to restore pending download tasks")
            return
        if restored:
            logger.info("Restored %d pending download tasks", restored)

    async def stop(self) -> None:
        # stop() 会等待所有 create_task 任务结束；先让排队中的任务退出，
        # 否则重启时要等整个队列下载完才能退出。它们仍保存在 pending_tasks 里，下次启动时恢复
        task_queue = self.bot_data.get("task_queue")
        if task_queue is not None:
            task_queue.close()
        await super().stop()


async def post_init(application) -> None:
    from bot.database.connection import get_db
    db_path = os.path.join("data", "bot.db")
    os.makedirs("data", exist_ok=True)
    await init_db(db_path)
    db = await get_db()
    await create_tables(db)
    # 依赖数据库连接，所以在这里而不是 build 时创建
    application.bot_data["task_queue"] = TaskQueue(SqliteTaskStore(db))
    interrupted = await fail_interrupted_post_logs(db)
    if interrupted:
        logger.info("Marked %d interrupted post logs as failed", interrupted)
    if not await is_admin(db, user_id=SUPER_ADMIN_ID):
        await add_admin(db, user_id=SUPER_ADMIN_ID, username="super_admin")
        logger.info("Super admin %d added to database", SUPER_ADMIN_ID)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    removed = cleanup_stale_files()
    if removed:
        logger.info("Cleaned up %d stale download files", removed)
    await application.bot.set_my_commands([
        BotCommand("start", "Open menu"),
        BotCommand("help", "Show help"),
        BotCommand("tasks", "View active downloads"),
        BotCommand("list_channels", "List channels"),
        BotCommand("settings", "View settings"),
        BotCommand("add_channel", "Add channel"),
        BotCommand("remove_channel", "Remove channel"),
        BotCommand("set", "Update setting"),
        BotCommand("list_admins", "List admins"),
        BotCommand("add_admin", "Add admin"),
        BotCommand("remove_admin", "Remove admin"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    me = await application.bot.get_me()
    logger.info("Bot started: @%s (id=%d)", me.username, me.id)


async def post_stop(application) -> None:
    """Called after Application.stop() awaits all create_task tasks, before shutdown."""
    await close_db()


def main() -> None:
    app = (
        ApplicationBuilder()
        .application_class(BotApplication)
        .token(BOT_TOKEN)
        .base_url(f"{API_BASE_URL}/bot")
        .base_file_url(f"{API_BASE_URL}/file/bot")
        .local_mode(True)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.add_handler(build_conversation_handler())
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("add_admin", add_admin_handler))
    app.add_handler(CommandHandler("remove_admin", remove_admin_handler))
    app.add_handler(CommandHandler("list_admins", list_admins_handler))
    app.add_handler(CommandHandler("add_channel", add_channel_handler))
    app.add_handler(CommandHandler("remove_channel", remove_channel_handler))
    app.add_handler(CommandHandler("list_channels", list_channels_handler))
    app.add_handler(CommandHandler("set", set_setting_handler))
    app.add_handler(CommandHandler("get", get_setting_handler))
    app.add_handler(CommandHandler("settings", settings_handler))
    app.add_handler(CommandHandler("tasks", tasks_handler))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern=r"^menu:"))
    logger.info("Starting bot polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
