from __future__ import annotations
import logging

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
from bot.utils.validators import extract_url, sanitize_caption

logger = logging.getLogger(__name__)

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

    context.user_data["source_url"] = url
    await update.message.reply_text("Link received. Please enter the caption (HTML format supported, max 1024 chars).\nSend /skip to publish without caption.")
    return WAITING_CAPTION


async def _fetch_and_ask_resolution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = context.user_data["source_url"]
    msg = update.callback_query.message if update.callback_query else update.message
    status_msg = await msg.reply_text("Checking available resolutions...")

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
        context.user_data["max_resolution"] = resolutions[0]
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
    context.user_data["caption"] = ""
    return await _fetch_and_ask_resolution(update, context)


async def caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_caption = update.message.text
    sanitized, error = sanitize_caption(raw_caption)
    if error:
        await update.message.reply_text(error)
        return WAITING_CAPTION

    context.user_data["caption"] = sanitized
    return await _fetch_and_ask_resolution(update, context)


async def _ask_channel_or_download(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_msg=None) -> int:
    db = await get_db()
    channels = await list_channels(db)

    if not channels:
        msg = reply_msg or (update.callback_query.message if update.callback_query else update.message)
        await msg.reply_text("No channels configured. Use /add_channel first.")
        return ConversationHandler.END

    if len(channels) == 1:
        context.user_data["channel_chat_id"] = channels[0]["chat_id"]
        return await _start_download(update, context, reply_msg)

    msg = reply_msg or (update.callback_query.message if update.callback_query else update.message)
    keyboard = [
        [InlineKeyboardButton(ch["title"], callback_data=f"ch:{ch['chat_id']}")]
        for ch in channels
    ]
    await msg.reply_text("Select target channel:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_CHANNEL


async def resolution_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    resolution = int(query.data.removeprefix("res:"))
    context.user_data["max_resolution"] = resolution
    return await _ask_channel_or_download(update, context)


async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["channel_chat_id"] = int(query.data.removeprefix("ch:"))
    return await _start_download(update, context)


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def _start_download(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_msg=None) -> int:
    source_url = context.user_data["source_url"]
    caption = context.user_data["caption"]
    channel_chat_id = context.user_data["channel_chat_id"]
    user_id = update.effective_user.id
    user_chat_id = update.effective_chat.id

    msg = reply_msg or (update.callback_query.message if update.callback_query else update.message)
    await msg.reply_text("Task accepted. Downloading video...")

    max_resolution = context.user_data.get("max_resolution", 1080)

    context.application.create_task(
        download_and_publish(
            bot=context.bot, task_queue=context.bot_data["task_queue"],
            user_chat_id=user_chat_id, user_id=user_id,
            source_url=source_url, caption=caption, channel_chat_id=channel_chat_id,
            max_resolution=max_resolution,
        ),
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
