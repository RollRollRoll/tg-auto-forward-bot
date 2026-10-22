import logging
import os

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler

from bot.config import API_BASE_URL, BOT_TOKEN, DOWNLOAD_DIR, SUPER_ADMIN_ID
from bot.database.connection import close_db, init_db
from bot.database.crud import add_admin, is_admin
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
from bot.services.downloader import DownloadSlotManager, cleanup_stale_files

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


class BotApplication(Application):
    async def stop(self) -> None:
        # stop() 会等待所有 create_task 任务结束；先取消排队中的任务，
        # 否则重启时要等整个队列下载完才能退出
        slot_manager = self.bot_data.get("slot_manager")
        if slot_manager is not None:
            slot_manager.close()
        await super().stop()


async def post_init(application) -> None:
    from bot.database.connection import get_db
    db_path = os.path.join("data", "bot.db")
    os.makedirs("data", exist_ok=True)
    await init_db(db_path)
    db = await get_db()
    await create_tables(db)
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
    app.bot_data["slot_manager"] = DownloadSlotManager()
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
