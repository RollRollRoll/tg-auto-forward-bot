from __future__ import annotations
from telegram import Update
from telegram.ext import ContextTypes
from bot.handlers.filters import private_chat_only, admin_only, super_admin_only
from bot.database.connection import get_db
from bot.database.crud import (
    add_admin, remove_admin, list_admins,
    add_channel, remove_channel, list_channels,
    get_setting, set_setting, get_all_settings,
    list_recent_post_logs,
)

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

async def format_admins_text() -> str:
    db = await get_db()
    admins = await list_admins(db)
    if not admins:
        return "No admins configured."
    lines = [f"<b>Admins ({len(admins)}):</b>"]
    for a in admins:
        lines.append(f"  {a['user_id']} — {a['username'] or 'N/A'}")
    return "\n".join(lines)


@private_chat_only
@super_admin_only
async def list_admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await format_admins_text()
    await update.message.reply_text(text, parse_mode="HTML")

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

async def format_channels_text() -> str:
    db = await get_db()
    channels = await list_channels(db)
    if not channels:
        return "No channels configured."
    lines = [f"<b>Channels ({len(channels)}):</b>"]
    for ch in channels:
        lines.append(f"  {ch['title']} ({ch['chat_id']})")
    return "\n".join(lines)


@private_chat_only
@admin_only
async def list_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await format_channels_text()
    await update.message.reply_text(text, parse_mode="HTML")

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

async def format_settings_text() -> str:
    db = await get_db()
    settings = await get_all_settings(db)
    lines = ["<b>Settings:</b>"]
    for k, v in sorted(settings.items()):
        lines.append(f"  <code>{k}</code> = <code>{v}</code>")
    return "\n".join(lines)


@private_chat_only
@admin_only
async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await format_settings_text()
    await update.message.reply_text(text, parse_mode="HTML")


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


_STATUS_LABELS = {
    "waiting": "Waiting",
    "downloading": "Downloading",
    "processing": "Processing",
    "publishing": "Publishing",
}


async def format_tasks_text(task_queue) -> str:
    db = await get_db()
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")

    active_tasks = task_queue.get_active_tasks()

    lines: list[str] = []

    if active_tasks:
        lines.append(f"<b>Active Downloads ({len(active_tasks)}/{max_concurrent}):</b>\n")
        for t in active_tasks:
            url = t["url"]
            if len(url) > 50:
                url = url[:50] + "..."
            status_label = _STATUS_LABELS.get(t["status"], t["status"])
            elapsed = _format_elapsed(t["elapsed"])
            progress = t["progress"]
            lines.append(f"#{t['task_id']} — {status_label}")
            lines.append(f"  URL: <code>{url}</code>")
            if t["status"] == "downloading":
                bar_len = 10
                filled = int(progress / 100 * bar_len)
                bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                lines.append(f"  Progress: [{bar}] {progress:.1f}%")
            elif t["status"] == "processing":
                lines.append("  Progress: Converting format...")
            elif t["status"] == "publishing":
                lines.append("  Progress: Uploading to channel...")
            lines.append(f"  Elapsed: {elapsed}")
            lines.append("")
    else:
        lines.append(f"<b>Active Downloads (0/{max_concurrent}):</b>")
        lines.append("No active tasks.\n")

    queued_tasks = task_queue.get_queued_tasks()
    if queued_tasks:
        lines.append(f"<b>Queued ({len(queued_tasks)}):</b>")
        for t in queued_tasks:
            url = t["url"]
            if len(url) > 45:
                url = url[:45] + "..."
            lines.append(f"  {t['position']}. <code>{url}</code> (waiting {_format_elapsed(t['elapsed'])})")
        lines.append("")

    recent = await list_recent_post_logs(db, limit=5)
    if recent:
        lines.append("<b>Recent Tasks:</b>")
        for r in recent:
            status = r["status"]
            emoji = {"done": "OK", "failed": "FAIL", "downloading": "...", "publishing": "..."}
            label = emoji.get(status, status)
            url = r["source_url"]
            if len(url) > 45:
                url = url[:45] + "..."
            lines.append(f"  [{label}] <code>{url}</code>")
            if r["error_message"]:
                lines.append(f"       Error: {r['error_message'][:60]}")

    return "\n".join(lines)


@private_chat_only
@admin_only
async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await format_tasks_text(context.bot_data["task_queue"])
    await update.message.reply_text(text, parse_mode="HTML")
