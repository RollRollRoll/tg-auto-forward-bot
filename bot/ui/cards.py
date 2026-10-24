"""发链接流程里的卡片：简单提示、caption 提示、confirmation card。纯函数，只负责渲染。"""
from __future__ import annotations

from telegram import InlineKeyboardMarkup

from bot.ui.markup import caption_markdown, h, md, md_code, md_join
from bot.ui.sender import Card


def notice(text: str, keyboard: InlineKeyboardMarkup | None = None) -> Card:
    """一行提示文字，例如选择结果 "Caption: skipped"、"Cancelled."。"""
    return Card(markdown=md(text), html=h(text), keyboard=keyboard)


def caption_prompt(url: str, *, editing: bool, keyboard: InlineKeyboardMarkup) -> Card:
    if editing:
        title, hint = "✏️ Edit caption", "Send the new caption (HTML supported, max 1024 chars)."
        md_lines, html_lines = [f"**{md(title)}**", md(hint)], [f"<b>{h(title)}</b>", h(hint)]
    else:
        title = "🔗 Link received"
        hint = "Send the caption (HTML supported, max 1024 chars), or tap Skip caption."
        md_lines = [f"**{md(title)}**", md_code(url), md(hint)]
        html_lines = [f"<b>{h(title)}</b>", f"<code>{h(url)}</code>", h(hint)]
    return Card(markdown=md_join(md_lines), html="\n".join(html_lines), keyboard=keyboard)


def confirmation_card(*, url: str, caption_html: str, resolution_label: str, channel_title: str,
                      header: str = "📝 Confirm the task",
                      keyboard: InlineKeyboardMarkup | None = None) -> Card:
    """TaskDraft 的摘要。caption 按发布到 Channel 后的效果渲染，不显示 HTML 源码。"""
    md_lines = [
        f"**{md(header)}**", f"Link: {md_code(url)}",
        f"Resolution: {md(resolution_label)}", f"Channel: {md(channel_title)}",
    ]
    caption_md = caption_markdown(caption_html) if caption_html else "_\\(no caption\\)_"
    # caption 单独成段，避免和上面的硬换行连在一起
    markdown = md_join(md_lines) + "\n\n**Caption**\n\n" + caption_md
    html = "\n".join([
        f"<b>{h(header)}</b>", f"Link: <code>{h(url)}</code>",
        f"Resolution: {h(resolution_label)}", f"Channel: {h(channel_title)}",
        "", "<b>Caption</b>", caption_html or "<i>(no caption)</i>",
    ])
    return Card(markdown=markdown, html=html, keyboard=keyboard)
