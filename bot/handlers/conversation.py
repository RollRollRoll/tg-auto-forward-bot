from __future__ import annotations
import logging
import secrets
from dataclasses import dataclass, field

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
from bot.services.sources import NoSingleVideo
from bot.services.task_queue import Task
from bot.ui.cards import caption_prompt, confirmation_card, notice
from bot.ui.keyboards import btn as _btn, rows as _rows
from bot.ui.sender import Card, RichSender, SentMessage
from bot.utils.validators import extract_url, sanitize_caption

logger = logging.getLogger(__name__)


@dataclass
class TaskDraft:
    """会话中逐步填写的 Task，确认后由 to_task() 生成 Task。"""
    source_url: str
    caption: str = ""
    channel_chat_id: int | None = None
    # 兜底默认值；正常流程里分辨率选择步骤一定会设置它
    max_resolution: int = 1080
    # 以下只用于会话界面，不进入 Task。draft_id 写进按钮的 callback_data，用来识别过期按钮
    draft_id: str = field(default_factory=lambda: secrets.token_hex(4))
    resolutions: list[int] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)
    # 还带着按钮的消息，丢弃这个 TaskDraft 时要把按钮去掉
    open: dict[int, SentMessage] = field(default_factory=dict)
    prompt: SentMessage | None = None
    card: SentMessage | None = None

    def to_task(self, *, user_id: int, user_chat_id: int) -> Task:
        return Task(
            user_id=user_id, user_chat_id=user_chat_id, source_url=self.source_url,
            caption=self.caption, channel_chat_id=self.channel_chat_id,
            max_resolution=self.max_resolution,
        )


WAITING_CAPTION = 0
WAITING_RESOLUTION = 1
WAITING_CHANNEL = 2
CONFIRMING = 3
EDIT_CAPTION = 4

# 本会话会处理的按钮；不属于当前 TaskDraft 的一律按过期处理
BUTTON_PATTERN = r"^(flow|res|ch|card):"
_EXPIRED = "This action has expired"

_RESOLUTION_LABELS = {
    240: "240p", 360: "360p", 480: "480p",
    720: "720p HD", 1080: "1080p FHD",
    1440: "1440p 2K", 2160: "4K",
}


def _label(height: int) -> str:
    return _RESOLUTION_LABELS.get(height, f"{height}p")




# ---- 消息发送与过期按钮 ----

def _sender(context) -> RichSender:
    return RichSender(context.bot)


def _track(context, sent: SentMessage, card: Card) -> None:
    # 记录还带着按钮的消息，丢弃 TaskDraft 时统一去掉按钮
    draft: TaskDraft | None = context.user_data.get("draft")
    if draft is None:
        return
    if card.keyboard is not None and card.keyboard.inline_keyboard:
        draft.open[sent.message_id] = sent
    else:
        draft.open.pop(sent.message_id, None)


async def _send(update: Update, context, card: Card) -> SentMessage:
    sent = await _sender(context).send(update.effective_chat.id, card)
    _track(context, sent, card)
    return sent


async def _edit(context, sent: SentMessage, card: Card) -> None:
    await _sender(context).edit(sent, card)
    _track(context, sent, card)


async def _discard(context) -> None:
    """丢弃当前 TaskDraft，去掉它留下的所有按钮。"""
    draft: TaskDraft | None = context.user_data.pop("draft", None)
    if draft is None:
        return
    for sent in draft.open.values():
        await _sender(context).remove_keyboard(sent)


def _parse(data: str) -> tuple[str, str]:
    """callback_data 格式为 前缀:draft_id:动作，返回 (draft_id, 动作)。"""
    _, draft_id, action = (data.split(":", 2) + ["", ""])[:3]
    return draft_id, action


def _current(context, draft_id: str) -> TaskDraft | None:
    draft: TaskDraft | None = context.user_data.get("draft")
    return draft if draft is not None and draft.draft_id == draft_id else None


def _clicked(context, update: Update) -> SentMessage:
    # 优先用发送时记下的格式；不在记录里的消息按内容判断
    msg = update.callback_query.message
    draft: TaskDraft | None = context.user_data.get("draft")
    if draft is not None and msg.message_id in draft.open:
        return draft.open[msg.message_id]
    return SentMessage.of(msg)


async def _expire(update: Update, context) -> None:
    query = update.callback_query
    await query.answer(_EXPIRED)
    await _sender(context).remove_keyboard(SentMessage.of(query.message))


async def _draft_for_button(update: Update, context) -> tuple[TaskDraft | None, str]:
    """按钮所属的当前 TaskDraft 和动作。按钮已过期时提示并返回 (None, "")。

    过期时 handler 必须返回 None：ConversationHandler 收到 None 会保持当前状态，
    这样点旧消息上的按钮不会改变正在进行的流程。
    """
    draft_id, action = _parse(update.callback_query.data)
    draft = _current(context, draft_id)
    if draft is None:
        await _expire(update, context)
        return None, ""
    return draft, action


@private_chat_only
@admin_only
async def expired_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """不属于任何进行中会话的流程按钮（已提交、已取消、已被新链接替换，或 Bot 重启过）。"""
    await _expire(update, context)


# ---- 键盘 ----

def _cancel_button(draft: TaskDraft) -> InlineKeyboardButton:
    return _btn("✖ Cancel", f"flow:{draft.draft_id}:cancel", "danger")


def _caption_keyboard(draft: TaskDraft, *, editing: bool) -> InlineKeyboardMarkup:
    second = _btn("Keep current", f"flow:{draft.draft_id}:keep") if editing else _cancel_button(draft)
    return InlineKeyboardMarkup([[_btn("Skip caption", f"flow:{draft.draft_id}:skip"), second]])


def _card_keyboard(draft: TaskDraft) -> InlineKeyboardMarkup:
    d = draft.draft_id
    return InlineKeyboardMarkup([[
        _btn("✅ Confirm", f"card:{d}:confirm", "success"),
        _btn("✏️ Edit", f"card:{d}:edit"),
        _btn("✖ Cancel", f"card:{d}:cancel", "danger"),
    ]])


def _back_row(draft: TaskDraft) -> list[InlineKeyboardButton]:
    return [_btn("« Back", f"card:{draft.draft_id}:back")]


def _edit_keyboard(draft: TaskDraft) -> InlineKeyboardMarkup:
    # 只有一个可选项时不提供对应的修改入口
    d = draft.draft_id
    options = [_btn("Caption", f"card:{d}:e_caption")]
    if len(draft.resolutions) > 1:
        options.append(_btn("Resolution", f"card:{d}:e_res"))
    if len(draft.channels) > 1:
        options.append(_btn("Channel", f"card:{d}:e_ch"))
    return InlineKeyboardMarkup([options, _back_row(draft)])


def _channel_title(draft: TaskDraft) -> str:
    return next((ch["title"] for ch in draft.channels if ch["chat_id"] == draft.channel_chat_id),
                str(draft.channel_chat_id))


def _card(draft: TaskDraft, *, header: str = "📝 Confirm the task", keyboard=None) -> Card:
    return confirmation_card(
        url=draft.source_url, caption_html=draft.caption, resolution_label=_label(draft.max_resolution),
        channel_title=_channel_title(draft), header=header, keyboard=keyboard,
    )


# ---- 流程步骤 ----

@private_chat_only
@admin_only
async def entry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = extract_url(update.message.text)
    if not url:
        return ConversationHandler.END
    # 流程中途发新链接：丢弃当前 TaskDraft，用新链接重新开始
    await _discard(context)
    draft = TaskDraft(source_url=url)
    context.user_data["draft"] = draft
    card = caption_prompt(url, editing=False, keyboard=_caption_keyboard(draft, editing=False))
    draft.prompt = await _send(update, context, card)
    return WAITING_CAPTION


async def _close_prompt(context, text: str) -> None:
    # caption 提示变成选择结果，按钮去掉
    draft: TaskDraft = context.user_data["draft"]
    if draft.prompt is not None:
        await _edit(context, draft.prompt, notice(text))
        draft.prompt = None


async def _after_caption(update: Update, context, state: int) -> int:
    if state == EDIT_CAPTION:
        return await _show_card(update, context)
    return await _fetch_and_ask_resolution(update, context)


async def _caption_state(context) -> int:
    draft: TaskDraft = context.user_data["draft"]
    return EDIT_CAPTION if draft.card is not None else WAITING_CAPTION


@private_chat_only
@admin_only
async def skip_caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = await _caption_state(context)
    context.user_data["draft"].caption = ""
    await _close_prompt(context, "Caption: skipped")
    return await _after_caption(update, context, state)


@private_chat_only
@admin_only
async def caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = await _caption_state(context)
    sanitized, error = sanitize_caption(update.message.text)
    if error:
        await update.message.reply_text(error)
        return state
    context.user_data["draft"].caption = sanitized
    await _close_prompt(context, "Caption: set")
    return await _after_caption(update, context, state)


@private_chat_only
@admin_only
async def caption_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft, action = await _draft_for_button(update, context)
    if draft is None:
        return None
    await update.callback_query.answer()
    state = await _caption_state(context)
    if action == "cancel":
        return await _cancel(update, context)
    if action == "keep":
        await _close_prompt(context, "Caption: unchanged")
        return await _show_card(update, context)
    draft.caption = ""
    await _close_prompt(context, "Caption: skipped")
    return await _after_caption(update, context, state)


async def _fetch_and_ask_resolution(update: Update, context) -> int:
    draft: TaskDraft = context.user_data["draft"]
    status = await _send(update, context, notice("Checking available resolutions..."))
    message = "No video found at this URL, or the site is not supported."
    try:
        heights = await extract_available_resolutions(draft.source_url)
    except NoSingleVideo as e:
        message = str(e)
        heights = []
    except Exception as e:
        logger.warning("Failed to extract formats for %s: %s", draft.source_url, e)
        heights = []
    if not heights:
        await _edit(context, status, notice(message))
        await _discard(context)
        return ConversationHandler.END

    # 只提供不超过 max_resolution 设置的分辨率；若全部超出，则退回到最低的那一档
    db = await get_db()
    max_resolution = int(await get_setting(db, "max_resolution") or "1080")
    draft.resolutions = [h for h in heights if h <= max_resolution] or [min(heights)]

    if len(draft.resolutions) == 1:
        draft.max_resolution = draft.resolutions[0]
        await _edit(context, status, notice(f"Resolution: {_label(draft.max_resolution)}"))
        return await _ask_channel(update, context)

    buttons = [_btn(_label(h), f"res:{draft.draft_id}:{h}") for h in draft.resolutions]
    keyboard = InlineKeyboardMarkup(_rows(buttons, 3) + [[_cancel_button(draft)]])
    await _edit(context, status, notice("Select video resolution:", keyboard))
    return WAITING_RESOLUTION


@private_chat_only
@admin_only
async def resolution_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft, value = await _draft_for_button(update, context)
    if draft is None:
        return None
    await update.callback_query.answer()
    draft.max_resolution = int(value)
    await _edit(context, _clicked(context, update), notice(f"Resolution: {_label(draft.max_resolution)}"))
    return await _ask_channel(update, context)


async def _ask_channel(update: Update, context) -> int:
    draft: TaskDraft = context.user_data["draft"]
    db = await get_db()
    draft.channels = await list_channels(db)
    if not draft.channels:
        await _send(update, context, notice("No channels configured. Use /add_channel first."))
        await _discard(context)
        return ConversationHandler.END
    if len(draft.channels) == 1:
        draft.channel_chat_id = draft.channels[0]["chat_id"]
        return await _show_card(update, context)
    keyboard = InlineKeyboardMarkup(
        [[_btn(ch["title"], f"ch:{draft.draft_id}:{ch['chat_id']}")] for ch in draft.channels]
        + [[_cancel_button(draft)]]
    )
    await _send(update, context, notice("Select target channel:", keyboard))
    return WAITING_CHANNEL


@private_chat_only
@admin_only
async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft, value = await _draft_for_button(update, context)
    if draft is None:
        return None
    await update.callback_query.answer()
    draft.channel_chat_id = int(value)
    await _edit(context, _clicked(context, update), notice(f"Channel: {_channel_title(draft)}"))
    return await _show_card(update, context)


# ---- confirmation card ----

async def _show_card(update: Update, context) -> int:
    """显示 confirmation card。第一次发送新消息；从 Edit 回来时，如果卡片就是当前点击的消息则原地更新。"""
    draft: TaskDraft = context.user_data["draft"]
    card = _card(draft, keyboard=_card_keyboard(draft))
    query = update.callback_query
    if draft.card is not None and query is not None and query.message.message_id == draft.card.message_id:
        await _edit(context, draft.card, card)
        return CONFIRMING
    if draft.card is not None:
        # 旧卡片在新卡片上方，去掉按钮避免误点
        await _sender(context).remove_keyboard(draft.card)
        draft.open.pop(draft.card.message_id, None)
    draft.card = await _send(update, context, card)
    return CONFIRMING


async def _submit(update: Update, context, draft: TaskDraft) -> int:
    # 调用方已经把 TaskDraft 从会话里取出
    task = draft.to_task(user_id=update.effective_user.id, user_chat_id=update.effective_chat.id)
    context.application.create_task(
        download_and_publish(
            bot=context.bot, task_queue=context.bot_data["task_queue"], task=task,
            limiter=context.bot_data["status_limiter"],
        ),
        update=update,
    )
    if not await _sender(context).edit(draft.card, _card(draft, header="✅ Submitted")):
        # 编辑失败时至少去掉按钮；残留的按钮之后点了也只会提示过期
        await _sender(context).remove_keyboard(draft.card)
    for sent in draft.open.values():
        if sent.message_id != draft.card.message_id:
            await _sender(context).remove_keyboard(sent)
    return ConversationHandler.END


@private_chat_only
@admin_only
async def card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft_id, action = _parse(update.callback_query.data)
    draft = _current(context, draft_id)
    if draft is not None and action == "confirm":
        # 检查和取出 TaskDraft 之间不能有 await：同一个 TaskDraft 只会生成一个 Task
        context.user_data.pop("draft", None)
        await update.callback_query.answer()
        return await _submit(update, context, draft)
    if draft is None:
        await _expire(update, context)
        return None
    await update.callback_query.answer()
    clicked = _clicked(context, update)
    match action:
        case "cancel":
            return await _cancel(update, context)
        case "back":
            await _edit(context, clicked, _card(draft, keyboard=_card_keyboard(draft)))
        case "edit":
            # Channel 可能在卡片显示之后增删过，修改菜单按当前配置列出
            draft.channels = await list_channels(await get_db())
            await _edit(context, clicked, _card(draft, keyboard=_edit_keyboard(draft)))
        case "e_res":
            buttons = [_btn(_label(h), f"card:{draft.draft_id}:r{h}") for h in draft.resolutions]
            await _edit(context, clicked, _card(draft, keyboard=InlineKeyboardMarkup(_rows(buttons, 3) + [_back_row(draft)])))
        case "e_ch":
            rows = [[_btn(ch["title"], f"card:{draft.draft_id}:c{ch['chat_id']}")] for ch in draft.channels]
            await _edit(context, clicked, _card(draft, keyboard=InlineKeyboardMarkup(rows + [_back_row(draft)])))
        case "e_caption":
            # 卡片停在只读状态，等新的 caption
            await _edit(context, clicked, _card(draft))
            card = caption_prompt(draft.source_url, editing=True, keyboard=_caption_keyboard(draft, editing=True))
            draft.prompt = await _send(update, context, card)
            return EDIT_CAPTION
        case _ if action.startswith("r") and action[1:].isdigit():
            draft.max_resolution = int(action[1:])
            await _edit(context, clicked, _card(draft, keyboard=_card_keyboard(draft)))
        case _ if action.startswith("c") and action[1:].lstrip("-").isdigit():
            draft.channel_chat_id = int(action[1:])
            await _edit(context, clicked, _card(draft, keyboard=_card_keyboard(draft)))
    return CONFIRMING


# ---- 取消 ----

async def _cancel(update: Update, context) -> int:
    query = update.callback_query
    if query is not None:
        clicked = _clicked(context, update)
        draft: TaskDraft | None = context.user_data.get("draft")
        if draft is not None:
            draft.open.pop(clicked.message_id, None)
        await _sender(context).edit(clicked, notice("Cancelled."))
    await _discard(context)
    return ConversationHandler.END


@private_chat_only
@admin_only
async def cancel_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft, _ = await _draft_for_button(update, context)
    if draft is None:
        return None
    await update.callback_query.answer()
    return await _cancel(update, context)


@private_chat_only
@admin_only
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _discard(context)
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    url_filter = filters.TEXT & filters.Regex(r"https?://\S+")
    new_link = MessageHandler(url_filter & ~filters.COMMAND, entry_handler)
    flow_buttons = [
        CallbackQueryHandler(cancel_button_handler, pattern=r"^flow:[^:]+:cancel$"),
        CallbackQueryHandler(caption_button_handler, pattern=r"^flow:"),
    ]
    return ConversationHandler(
        entry_points=[MessageHandler(url_filter, entry_handler)],
        states={
            WAITING_CAPTION: [
                CommandHandler("skip", skip_caption_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_handler),
                *flow_buttons,
            ],
            WAITING_RESOLUTION: [
                CallbackQueryHandler(resolution_handler, pattern=r"^res:"), *flow_buttons, new_link,
            ],
            WAITING_CHANNEL: [
                CallbackQueryHandler(channel_handler, pattern=r"^ch:"), *flow_buttons, new_link,
            ],
            CONFIRMING: [CallbackQueryHandler(card_handler, pattern=r"^card:"), *flow_buttons, new_link],
            EDIT_CAPTION: [
                CommandHandler("skip", skip_caption_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_handler),
                *flow_buttons,
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            # 当前状态不接受的流程按钮（例如点了旧消息上的按钮）
            CallbackQueryHandler(expired_button_handler, pattern=BUTTON_PATTERN),
        ],
        per_user=True,
        per_chat=True,
    )
