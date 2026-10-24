from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import SUPER_ADMIN_ID
from bot.database.connection import get_db
from bot.database.crud import set_setting
from bot.handlers.filters import admin_only, private_chat_only
from bot.ui.pages import (
    SETTING_PRESETS, admins_page, back_keyboard, channels_page, help_page, main_menu,
    setting_presets_page, settings_page, tasks_page,
)
from bot.ui.sender import RichSender, SentMessage


async def _show(update: Update, context, card) -> None:
    # 按菜单消息原来的格式原地编辑
    await RichSender(context.bot).edit(SentMessage.of(update.callback_query.message), card)


@private_chat_only
@admin_only
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.removeprefix("menu:")
    user_id = update.effective_user.id

    if action == "admins" and user_id != SUPER_ADMIN_ID:
        await query.answer()
        return
    if action == "close":
        await query.answer()
        await query.delete_message()
        return

    await query.answer()
    match action:
        case "back":
            card = main_menu(user_id)
        case "tasks":
            card = await tasks_page(context.bot_data["task_queue"], back_keyboard())
        case "channels":
            card = await channels_page(back_keyboard())
        case "settings":
            card = await settings_page(with_buttons=True)
        case "admins":
            card = await admins_page(back_keyboard())
        case "help":
            card = help_page(back_keyboard())
        case _:
            return
    await _show(update, context, card)


@private_chat_only
@admin_only
async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Settings 页的按钮：set:<key> 打开预设页，set:<key>:<value> 写入设置后回到 Settings 页。"""
    query = update.callback_query
    _, key, *rest = query.data.split(":", 2)
    if key not in SETTING_PRESETS:
        await query.answer()
        return
    if not rest:
        await query.answer()
        await _show(update, context, await setting_presets_page(key))
        return
    try:
        # 和 /set 走同一个校验，防止伪造的 callback_data 写入非法值
        await set_setting(await get_db(), key, rest[0])
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return
    await query.answer(f"{key} = {rest[0]}")
    await _show(update, context, await settings_page(with_buttons=True))
