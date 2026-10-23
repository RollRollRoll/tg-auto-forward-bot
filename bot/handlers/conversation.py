from __future__ import annotations
import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from bot.database.connection import get_db
from bot.database.crud import get_setting, list_channels
from bot.handlers.filters import admin_only, private_chat_only
from bot.services.downloader import extract_available_resolutions
from bot.services.pipeline import download_and_publish
from bot.services.task_queue import Task
from bot.utils.validators import extract_url, sanitize_caption

logger = logging.getLogger(__name__)


@dataclass
class TaskDraft:
    """会话中逐步填写的 Task，填完后由 to_task() 生成 Task。"""
    source_url: str
    caption: str = ""
    channel_chat_id: int | None = None
    # 兜底默认值；正常流程里分辨率选择步骤一定会设置它
    max_resolution: int = 1080

    def to_task(self, *, user_id: int, user_chat_id: int) -> Task:
        return Task(
            user_id=user_id, user_chat_id=user_chat_id, source_url=self.source_url,
            caption=self.caption, channel_chat_id=self.channel_chat_id,
            max_resolution=self.max_resolution,
        )

WAITING_CAPTION = 0
WAITING_RESOLUTION = 1
WAITING_CHANNEL = 2

_RESOLUTION_LABELS = {
    240: "240p", 360: "360p", 480: "480p",
    720: "720p HD", 1080: "1080p FHD",
    1440: "1440p 2K", 2160: "4K",
}


@private_chat_only
@admin_only
async def entry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = extract_url(update.message.text)
    if not url:
        return ConversationHandler.END

    context.user_data["draft"] = TaskDraft(source_url=url)
    await update.message.reply_text("Link received. Please enter the caption (HTML format supported, max 1024 chars).\nSend /skip to publish without caption.")
    return WAITING_CAPTION


def _reply_target(update: Update, reply_msg=None):
    # 优先回复调用方指定的消息，否则回复按钮所在消息或用户发来的消息
    if reply_msg is not None:
        return reply_msg
    return update.callback_query.message if update.callback_query else update.message


async def _fetch_and_ask_resolution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = context.user_data["draft"].source_url
    status_msg = await _reply_target(update).reply_text("Checking available resolutions...")

    try:
        heights = await extract_available_resolutions(url)
    except Exception:
        logger.warning("Failed to extract formats for %s", url)
        heights = []

    if not heights:
        await status_msg.edit_text("No video found at this URL, or the site is not supported.")
        return ConversationHandler.END

    # 只提供不超过 max_resolution 设置的分辨率；若全部超出，则退回到最低的那一档
    db = await get_db()
    max_resolution = int(await get_setting(db, "max_resolution") or "1080")
    resolutions = [h for h in heights if h <= max_resolution] or [min(heights)]

    if len(resolutions) == 1:
        context.user_data["draft"].max_resolution = resolutions[0]
        label = _RESOLUTION_LABELS.get(resolutions[0], f"{resolutions[0]}p")
        await status_msg.edit_text(f"Only one resolution available: {label}. Proceeding...")
        return await _ask_channel_or_download(update, context, status_msg)

    keyboard = []
    row: list[InlineKeyboardButton] = []
    for h in resolutions:
        label = _RESOLUTION_LABELS.get(h, f"{h}p")
        row.append(InlineKeyboardButton(label, callback_data=f"res:{h}"))
        if len(row) >= 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await status_msg.edit_text(
        "Select video resolution:", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_RESOLUTION


async def skip_caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"].caption = ""
    return await _fetch_and_ask_resolution(update, context)


async def caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_caption = update.message.text
    sanitized, error = sanitize_caption(raw_caption)
    if error:
        await update.message.reply_text(error)
        return WAITING_CAPTION

    context.user_data["draft"].caption = sanitized
    return await _fetch_and_ask_resolution(update, context)


async def _ask_channel_or_download(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_msg=None) -> int:
    db = await get_db()
    channels = await list_channels(db)

    if not channels:
        await _reply_target(update, reply_msg).reply_text("No channels configured. Use /add_channel first.")
        return ConversationHandler.END

    if len(channels) == 1:
        context.user_data["draft"].channel_chat_id = channels[0]["chat_id"]
        return await _start_download(update, context, reply_msg)

    keyboard = [
        [InlineKeyboardButton(ch["title"], callback_data=f"ch:{ch['chat_id']}")]
        for ch in channels
    ]
    await _reply_target(update, reply_msg).reply_text("Select target channel:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_CHANNEL


async def resolution_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    resolution = int(query.data.removeprefix("res:"))
    context.user_data["draft"].max_resolution = resolution
    return await _ask_channel_or_download(update, context)


async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["draft"].channel_chat_id = int(query.data.removeprefix("ch:"))
    return await _start_download(update, context)


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def _start_download(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_msg=None) -> int:
    task = context.user_data["draft"].to_task(
        user_id=update.effective_user.id, user_chat_id=update.effective_chat.id,
    )
    await _reply_target(update, reply_msg).reply_text("Task accepted. Downloading video...")

    context.application.create_task(
        download_and_publish(bot=context.bot, task_queue=context.bot_data["task_queue"], task=task),
        update=update,
    )

    context.user_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    url_filter = filters.TEXT & filters.Regex(r"https?://\S+")
    return ConversationHandler(
        entry_points=[MessageHandler(url_filter, entry_handler)],
        states={
            WAITING_CAPTION: [
                CommandHandler("skip", skip_caption_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_handler),
            ],
            WAITING_RESOLUTION: [CallbackQueryHandler(resolution_handler, pattern=r"^res:")],
            WAITING_CHANNEL: [CallbackQueryHandler(channel_handler, pattern=r"^ch:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_user=True,
        per_chat=True,
    )
