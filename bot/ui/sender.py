"""私聊卡片类消息的发送与编辑：优先用原生 Rich Messages，被拒绝时退回 HTML。

python-telegram-bot 22.8 只原生支持到 Bot API 10.0，rich 消息通过原始方法调用（见 ADR 0001）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, EndPointNotFound, TelegramError

logger = logging.getLogger(__name__)

_EMPTY_KEYBOARD = InlineKeyboardMarkup([])


@dataclass(frozen=True)
class Card:
    """同一份内容的两种渲染，加上消息下方的按钮。"""
    markdown: str
    html: str
    keyboard: InlineKeyboardMarkup | None = None


@dataclass(frozen=True)
class SentMessage:
    """已发出的卡片消息；format 记录它用的是哪种格式，之后的编辑沿用同一种格式。"""
    chat_id: int
    message_id: int
    format: str  # "rich" 或 "html"

    @classmethod
    def of(cls, message: Message) -> SentMessage:
        # 按钮回调里拿到的消息：PTB 22.8 把 rich 内容放在 api_kwargs 里（实测）
        fmt = "rich" if "rich_message" in (message.api_kwargs or {}) else "html"
        return cls(chat_id=message.chat_id, message_id=message.message_id, format=fmt)


def _rejected(e: TelegramError) -> bool:
    # 内容不合法时返回 BadRequest；服务器版本太旧没有这个方法时返回 404
    return isinstance(e, (BadRequest, EndPointNotFound))


def _not_modified(e: TelegramError) -> bool:
    return isinstance(e, BadRequest) and "not modified" in str(e).lower()


class RichSender:
    def __init__(self, bot):
        self._bot = bot

    async def send(self, chat_id: int, card: Card) -> SentMessage:
        """发送卡片。rich 被拒绝时改用 HTML 发送；其他错误照常抛出。"""
        api_kwargs = {"chat_id": chat_id, "rich_message": {"markdown": card.markdown}}
        if card.keyboard is not None:
            api_kwargs["reply_markup"] = card.keyboard.to_dict()
        try:
            msg = await self._bot.do_api_request("sendRichMessage", api_kwargs=api_kwargs, return_type=Message)
            return SentMessage(chat_id=chat_id, message_id=msg.message_id, format="rich")
        except TelegramError as e:
            if not _rejected(e):
                raise
            logger.warning("sendRichMessage rejected in chat %d, falling back to HTML: %s", chat_id, e)
        return await self.send_html(chat_id, card)

    async def send_html(self, chat_id: int, card: Card) -> SentMessage:
        """直接用 HTML 发送卡片，例如 rich 编辑失败后补发最终状态。"""
        msg = await self._bot.send_message(chat_id, card.html, parse_mode="HTML", reply_markup=card.keyboard)
        return SentMessage(chat_id=chat_id, message_id=msg.message_id, format="html")

    async def send_text(self, chat_id: int, text: str) -> None:
        """发一条纯文本短消息，例如触发推送的结果提醒。"""
        await self._bot.send_message(chat_id, text)


    async def edit(self, sent: SentMessage, card: Card) -> bool:
        """按消息原来的格式编辑，返回是否成功；失败只记日志，不抛异常。"""
        # 编辑时不传 reply_markup 会清掉键盘（实测），所以总是显式传
        keyboard = card.keyboard or _EMPTY_KEYBOARD
        try:
            if sent.format == "rich":
                await self._bot.edit_message_text(
                    text=None, chat_id=sent.chat_id, message_id=sent.message_id,
                    reply_markup=keyboard, api_kwargs={"rich_message": {"markdown": card.markdown}},
                )
            else:
                await self._bot.edit_message_text(
                    card.html, chat_id=sent.chat_id, message_id=sent.message_id,
                    parse_mode="HTML", reply_markup=keyboard,
                )
            return True
        except TelegramError as e:
            if _not_modified(e):
                return True
            logger.warning("Failed to edit message %d in chat %d: %s", sent.message_id, sent.chat_id, e)
            return False

    async def remove_keyboard(self, sent: SentMessage) -> None:
        """只去掉按钮，不动内容；失败只记日志。"""
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=sent.chat_id, message_id=sent.message_id, reply_markup=_EMPTY_KEYBOARD,
            )
        except TelegramError as e:
            if not _not_modified(e):
                logger.warning("Failed to remove keyboard of message %d: %s", sent.message_id, e)


async def reply_card(update, context, card: Card) -> None:
    """在命令所在的私聊里发一张卡片。"""
    await RichSender(context.bot).send(update.effective_chat.id, card)
