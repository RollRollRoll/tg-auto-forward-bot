"""卡片渲染用的纯函数：Rich Markdown 转义、caption 预览、进度条、大小和时长格式、帖子链接。"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

_MD_SPECIALS = re.compile(r"([\\`*_\[\]()#+\-.!|~<>={}])")


def md(text: str) -> str:
    """把普通文本转义成 Rich Markdown 里的字面文本。"""
    return _MD_SPECIALS.sub(r"\\\1", str(text))


def h(text: str) -> str:
    """把普通文本转义成 HTML 里的字面文本。"""
    return _html.escape(str(text), quote=False)


def md_code(text: str) -> str:
    """行内代码；内容里有反引号时用双反引号包起来。"""
    text = str(text)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def md_join(lines: list[str]) -> str:
    """把多行拼成 Rich Markdown：相邻的非空行之间用硬换行，空行保留成段落分隔。

    Rich Markdown 兼容 GFM，普通换行会被合并成空格，所以行尾要加反斜杠。
    标题行本身就单独成块，加了反斜杠反而会原样显示出来，所以标题行不加。
    """
    out = []
    for line, nxt in zip(lines, lines[1:] + [""]):
        hard_break = line and nxt and not line.startswith("#")
        out.append(line + "\\" if hard_break else line)
    return "\n".join(out)


def progress_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def fmt_size(num_bytes: float) -> str:
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.0f} KB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.1f} MB"
    return f"{num_bytes / 1024 ** 3:.2f} GB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    hrs, rest = divmod(seconds, 3600)
    return f"{hrs}h {rest // 60}m"


def relative_time(seconds_ago: float) -> str:
    """距离现在多久，例如 "12m ago"。结束时间晚于当前时间（时钟偏差）时按 "just now" 处理。"""
    seconds = int(seconds_ago)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


_SHORT_URL_LIMIT = 45


def short_url(url: str) -> str:
    """去掉开头的协议和 www.，超过 45 个字符时保留前 44 个并加 …。"""
    text = re.sub(r"^https?://", "", url)
    text = text.removeprefix("www.")
    if len(text) > _SHORT_URL_LIMIT:
        return text[:_SHORT_URL_LIMIT - 1] + "…"
    return text


def post_link(channel_chat_id: int, username: str | None, message_id: int) -> str:
    """频道帖子的链接：公开频道用 username，私有频道用 t.me/c/<去掉 -100 的 id>。"""
    if username:
        return f"https://t.me/{username}/{message_id}"
    internal_id = str(channel_chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal_id}/{message_id}"


_INLINE = {"b": "**", "i": "_", "s": "~~"}


class _CaptionToMarkdown(HTMLParser):
    """把已清洗过的 caption HTML（只含 b/i/u/s/a/code/pre）转成 Rich Markdown。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: list[str | None] = []
        self._raw: list[str] | None = None  # code / pre 内的原文
        self._raw_tag: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._raw is not None:
            return
        if tag in _INLINE:
            self.out.append(_INLINE[tag])
        elif tag == "u":
            # Markdown 没有下划线语法，Rich Markdown 允许混写 HTML
            self.out.append("<u>")
        elif tag == "a":
            self._href.append(dict(attrs).get("href"))
            self.out.append("[")
        elif tag in ("code", "pre"):
            self._raw, self._raw_tag = [], tag

    def handle_endtag(self, tag):
        if self._raw is not None:
            if tag != self._raw_tag:
                return
            raw = "".join(self._raw)
            self._raw, self._raw_tag = None, None
            self.out.append(f"```\n{raw}\n```" if tag == "pre" else md_code(raw))
            return
        if tag in _INLINE:
            self.out.append(_INLINE[tag])
        elif tag == "u":
            self.out.append("</u>")
        elif tag == "a":
            href = self._href.pop() if self._href else None
            self.out.append(f"]({href})" if href else "]")

    def handle_data(self, data):
        if self._raw is not None:
            self._raw.append(data)
        else:
            # caption 里的换行要保留：普通换行在 Rich Markdown 里会被合并成空格
            self.out.append("\\\n".join(md(part) for part in data.split("\n")))


def caption_markdown(caption_html: str) -> str:
    """caption 按发布到频道后的效果渲染成 Rich Markdown，不显示 HTML 源码。"""
    parser = _CaptionToMarkdown()
    parser.feed(caption_html)
    parser.close()
    return "".join(parser.out)
