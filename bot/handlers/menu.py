from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.config import SUPER_ADMIN_ID
from bot.handlers.admin import (
    format_admins_text,
    format_channels_text,
    format_settings_text,
    format_tasks_text,
)
from bot.handlers.filters import admin_only, private_chat_only
from bot.handlers.start import HELP_TEXT

MENU_GREETING = "<b>Video Forward Bot</b>\n\nSelect an option:"


def build_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("\U0001f4cb Tasks", callback_data="menu:tasks"),
            InlineKeyboardButton("\U0001f4e2 Channels", callback_data="menu:channels"),
        ],
    ]
    if user_id == SUPER_ADMIN_ID:
        rows.append([
            InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="menu:settings"),
            InlineKeyboardButton("\U0001f464 Admins", callback_data="menu:admins"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="menu:settings"),
        ])
    rows.append([
        InlineKeyboardButton("\u2753 Help", callback_data="menu:help"),
        InlineKeyboardButton("\u2716 Close", callback_data="menu:close"),
    ])
    return InlineKeyboardMarkup(rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u00ab Back", callback_data="menu:back")]
    ])


@private_chat_only
@admin_only
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.removeprefix("menu:")
    user_id = update.effective_user.id

    if action == "back":
        await query.edit_message_text(
            MENU_GREETING,
            reply_markup=build_main_keyboard(user_id),
            parse_mode="HTML",
        )

    elif action == "tasks":
        text = await format_tasks_text(context.bot_data["task_queue"])
        await query.edit_message_text(
            text, reply_markup=_back_keyboard(), parse_mode="HTML",
        )

    elif action == "channels":
        text = await format_channels_text()
        await query.edit_message_text(
            text, reply_markup=_back_keyboard(), parse_mode="HTML",
        )

    elif action == "settings":
        text = await format_settings_text()
        await query.edit_message_text(
            text, reply_markup=_back_keyboard(), parse_mode="HTML",
        )

    elif action == "admins":
        if user_id != SUPER_ADMIN_ID:
            return
        text = await format_admins_text()
        await query.edit_message_text(
            text, reply_markup=_back_keyboard(), parse_mode="HTML",
        )

    elif action == "help":
        await query.edit_message_text(
            HELP_TEXT, reply_markup=_back_keyboard(), parse_mode="HTML",
        )

    elif action == "close":
        await query.delete_message()
