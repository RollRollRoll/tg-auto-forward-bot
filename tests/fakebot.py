"""测试用的假 Telegram Bot：记录 bot 发出的请求，并在内存里维护每个聊天里每条消息的当前样子。

测试只看结果：某条消息现在显示什么、是 rich 还是 html、带哪些按钮。
fail(method, exc) 可以让指定方法抛错，用来测 HTML 兜底和限流。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest


def plain(markdown: str) -> str:
    """去掉 Rich Markdown 的标记、转义和硬换行，得到大致能看到的文字。不追求完全还原排版。"""
    text = markdown.replace("\\\n", "\n")
    text = re.sub(r"^#{1,6} ", "", text, flags=re.M)
    # 先处理未转义的标记，再去掉转义：转义过的 \[ \* \` 是字面字符
    text = re.sub(r"(?<!\\)\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?<!\\)\*\*", "", text)
    text = re.sub(r"(?<!\\)`", "", text)
    return re.sub(r"\\(.)", r"\1", text)


@dataclass
class FakeMessage:
    chat_id: int
    message_id: int
    format: str  # "rich" / "html" / "text"
    content: str
    keyboard: list[list[dict]] = field(default_factory=list)

    @property
    def api_kwargs(self) -> dict:
        # 真实 PTB 解析 rich 消息时，rich 内容落在 api_kwargs["rich_message"]
        return {"rich_message": {"markdown": self.content}} if self.format == "rich" else {}

    @property
    def text(self) -> str:
        """用户大致能看到的文字：去掉 Markdown 转义和硬换行，或去掉 HTML 标签。不追求完全还原排版。"""
        if self.format == "rich":
            return plain(self.content)
        if self.format == "html":
            return html.unescape(re.sub(r"<[^>]+>", "", self.content))
        return self.content

    @property
    def buttons(self) -> list[dict]:
        return [b for row in self.keyboard for b in row]

    def button(self, text: str) -> dict:
        return next(b for b in self.buttons if b["text"] == text)


def _rows(markup) -> list[list[dict]]:
    if markup is None:
        return []
    data = markup.to_dict() if isinstance(markup, InlineKeyboardMarkup) else markup
    return [[dict(b) for b in row] for row in data.get("inline_keyboard", [])]
class FakeBot:
    def __init__(self, clock=None):
        self._clock = clock
        # (时间, 方法名, 参数)；没有传 clock 时时间为 None
        self.calls: list[tuple[float | None, str, dict]] = []
        self.chats: dict[int, dict[int, FakeMessage]] = {}
        self.answers: list[dict] = []
        # chat_id -> SimpleNamespace(username=..., title=...)，供 get_chat 返回
        self.chat_info: dict[int, SimpleNamespace] = {}
        self._next_id = 100
        self._failures: dict[str, list] = {}

    # ---- 测试用的控制和查询 ----

    def fail(self, method: str, exc: Exception, times: int | None = None) -> None:
        """让 method 抛 exc；times 为 None 表示一直失败。"""
        self._failures[method] = [exc, times]

    def messages(self, chat_id: int) -> list[FakeMessage]:
        return list(self.chats.get(chat_id, {}).values())

    def last(self, chat_id: int) -> FakeMessage:
        return self.messages(chat_id)[-1]

    def methods(self) -> list[str]:
        return [m for _, m, _ in self.calls]

    def _record(self, method: str, **kwargs) -> None:
        self.calls.append((self._clock() if self._clock else None, method, kwargs))
        failure = self._failures.get(method)
        if failure is None:
            return
        exc, times = failure
        if times is not None:
            if times <= 0:
                return
            failure[1] = times - 1
        raise exc

    def _store(self, chat_id: int, fmt: str, content: str, markup) -> FakeMessage:
        self._next_id += 1
        msg = FakeMessage(chat_id, self._next_id, fmt, content, _rows(markup))
        self.chats.setdefault(chat_id, {})[msg.message_id] = msg
        return msg

    def _get(self, chat_id: int, message_id: int) -> FakeMessage:
        try:
            return self.chats[chat_id][message_id]
        except KeyError:
            raise BadRequest("Message to edit not found") from None
    # ---- 被测代码会调用的 Bot 方法 ----

    async def do_api_request(self, endpoint: str, api_kwargs: dict | None = None, return_type=None, **_):
        api_kwargs = api_kwargs or {}
        self._record(endpoint, **api_kwargs)
        if endpoint != "sendRichMessage":
            raise AssertionError(f"FakeBot does not support {endpoint}")
        return self._store(
            api_kwargs["chat_id"], "rich", api_kwargs["rich_message"]["markdown"], api_kwargs.get("reply_markup"),
        )

    async def send_message(self, chat_id: int, text: str, parse_mode=None, reply_markup=None, **_):
        self._record("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return self._store(chat_id, "html" if parse_mode == "HTML" else "text", text, reply_markup)

    async def edit_message_text(self, text, chat_id: int, message_id: int, parse_mode=None,
                                reply_markup=None, api_kwargs: dict | None = None, **_):
        rich = (api_kwargs or {}).get("rich_message")
        self._record("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                     parse_mode=parse_mode, reply_markup=reply_markup, rich=rich is not None,
                     content=rich["markdown"] if rich is not None else text)
        msg = self._get(chat_id, message_id)
        if rich is not None:
            msg.format, msg.content = "rich", rich["markdown"]
        else:
            msg.format, msg.content = ("html" if parse_mode == "HTML" else "text"), text
        # 和真实服务器一样：编辑时不传 reply_markup，键盘会被清掉（实测）
        msg.keyboard = _rows(reply_markup)
        return msg

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None, **_):
        self._record("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        msg = self._get(chat_id, message_id)
        msg.keyboard = _rows(reply_markup)
        return msg

    async def get_chat(self, chat_id: int, **_):
        self._record("getChat", chat_id=chat_id)
        if chat_id not in self.chat_info:
            raise BadRequest("Chat not found")
        return self.chat_info[chat_id]


class FakeClock:
    """可以手动拨快的时钟；sleep 直接把时间往前拨，不真正等待。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        import asyncio
        self.now += max(seconds, 0)
        await asyncio.sleep(0)
