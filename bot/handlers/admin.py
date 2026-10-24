from __future__ import annotations
from telegram import Update
from telegram.ext import ContextTypes
from bot.handlers.filters import private_chat_only, admin_only, super_admin_only
from bot.database.connection import get_db
from bot.database.crud import (
    add_admin, remove_admin,
    add_channel, remove_channel,
    get_setting, set_setting,
)
from bot.ui.pages import admins_page, channels_page, settings_page, tasks_page
from bot.ui.sender import reply_card as _reply



@private_chat_only
@super_admin_only
async def add_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /add_admin <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be an integer.")
        return
    db = await get_db()
    try:
        await add_admin(db, user_id=user_id)
        await update.message.reply_text(f"Admin {user_id} added.")
    except Exception:
        await update.message.reply_text(f"Admin {user_id} already exists.")

@private_chat_only
@super_admin_only
async def remove_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /remove_admin <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be an integer.")
        return
    db = await get_db()
    if await remove_admin(db, user_id=user_id):
        await update.message.reply_text(f"Admin {user_id} removed.")
    else:
        await update.message.reply_text(f"Admin {user_id} not found.")

@private_chat_only
@super_admin_only
async def list_admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, context, await admins_page())

@private_chat_only
@admin_only
async def add_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /add_channel <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id must be an integer.")
        return
    bot = context.bot
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        await update.message.reply_text("Invalid chat_id or bot has no access to this channel.")
        return
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        if not getattr(member, "can_post_messages", False):
            await update.message.reply_text("Bot is not an admin of this channel with posting permissions. Please add the bot as a channel admin first.")
            return
    except Exception:
        await update.message.reply_text("Bot is not a member of this channel. Please add the bot as a channel admin first.")
        return
    db = await get_db()
    try:
        await add_channel(db, chat_id=chat_id, title=chat.title or str(chat_id))
        await update.message.reply_text(f"Channel added: <b>{chat.title}</b> ({chat_id})", parse_mode="HTML")
    except Exception:
        await update.message.reply_text(f"Channel {chat_id} already exists.")

@private_chat_only
@admin_only
async def remove_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /remove_channel <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id must be an integer.")
        return
    db = await get_db()
    if await remove_channel(db, chat_id=chat_id):
        await update.message.reply_text(f"Channel {chat_id} removed.")
    else:
        await update.message.reply_text(f"Channel {chat_id} not found.")

@private_chat_only
@admin_only
async def list_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, context, await channels_page())

@private_chat_only
@admin_only
async def set_setting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("Usage: /set <key> <value>")
        return
    key, value = context.args[0], context.args[1]
    db = await get_db()
    try:
        await set_setting(db, key, value)
        await update.message.reply_text(f"Setting <code>{key}</code> = <code>{value}</code>", parse_mode="HTML")
    except ValueError as e:
        await update.message.reply_text(str(e))

@private_chat_only
@admin_only
async def get_setting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /get <key>")
        return
    db = await get_db()
    val = await get_setting(db, context.args[0])
    if val is None:
        await update.message.reply_text(f"Unknown setting: {context.args[0]}")
    else:
        await update.message.reply_text(f"<code>{context.args[0]}</code> = <code>{val}</code>", parse_mode="HTML")

@private_chat_only
@admin_only
async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, context, await settings_page(with_buttons=False))


@private_chat_only
@admin_only
async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, context, await tasks_page(context.bot_data["task_queue"]))
