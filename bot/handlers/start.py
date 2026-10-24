from telegram import Update
from telegram.ext import ContextTypes

from bot.config import SUPER_ADMIN_ID
from bot.database.connection import get_db
from bot.database.crud import is_admin
from bot.handlers.filters import private_chat_only
from bot.ui.pages import help_page, main_menu
from bot.ui.sender import reply_card as _reply



@private_chat_only
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = await get_db()
    if user_id == SUPER_ADMIN_ID or await is_admin(db, user_id=user_id):
        await _reply(update, context, main_menu(user_id))
    else:
        await _reply(update, context, help_page())


@private_chat_only
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update, context, help_page())
