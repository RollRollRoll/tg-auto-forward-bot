"""菜单各页和列表命令的内容。菜单页和对应命令（/tasks、/settings、/list_*）共用同一份渲染。"""
from __future__ import annotations

from telegram import InlineKeyboardMarkup

from bot.config import SUPER_ADMIN_ID
from bot.database.connection import get_db
from bot.database.crud import get_all_settings, get_setting, list_admins, list_channels, list_recent_post_logs
from bot.ui.keyboards import btn as _btn, rows as _rows
from bot.ui.markup import fmt_duration, h, md, md_code, md_join, progress_bar
from bot.ui.sender import Card

# Settings 页可以点选的预设值：(值, 按钮文字)
SETTING_PRESETS: dict[str, list[tuple[str, str]]] = {
    "max_concurrent_downloads": [(v, v) for v in ("1", "2", "3", "4", "5")],
    "max_file_size_mb": [(v, f"{v} MB") for v in ("500", "1000", "1500", "2000")],
    "max_resolution": [(v, f"{v}p") for v in ("360", "480", "720", "1080", "1440", "2160")],
}


class _Lines:
    """同时拼 Rich Markdown 和 HTML 两份文本。"""

    def __init__(self):
        self.md: list[str] = []
        self.html: list[str] = []

    def add(self, md_line: str, html_line: str) -> None:
        self.md.append(md_line)
        self.html.append(html_line)

    def text(self, plain: str) -> None:
        self.add(md(plain), h(plain))

    def bold(self, plain: str) -> None:
        self.add(f"**{md(plain)}**", f"<b>{h(plain)}</b>")

    def blank(self) -> None:
        self.add("", "")

    def card(self, keyboard: InlineKeyboardMarkup | None = None) -> Card:
        return Card(markdown=md_join(self.md), html="\n".join(self.html), keyboard=keyboard)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("« Back", "menu:back")]])


def main_menu(user_id: int) -> Card:
    rows = [[_btn("📋 Tasks", "menu:tasks"), _btn("📢 Channels", "menu:channels")]]
    if user_id == SUPER_ADMIN_ID:
        rows.append([_btn("⚙️ Settings", "menu:settings"), _btn("👤 Admins", "menu:admins")])
    else:
        rows.append([_btn("⚙️ Settings", "menu:settings")])
    rows.append([_btn("❓ Help", "menu:help"), _btn("✖ Close", "menu:close")])
    lines = _Lines()
    lines.add("# Video Forward Bot", "<b>Video Forward Bot</b>")
    lines.blank()
    lines.text("Select an option:")
    return lines.card(InlineKeyboardMarkup(rows))


def _short(url: str, limit: int) -> str:
    return url[:limit] + "..." if len(url) > limit else url


_STATUS_LABELS = {
    "starting": "Starting",
    "downloading": "Downloading",
    "processing": "Processing",
    "publishing": "Publishing",
}
_STATUS_NOTES = {
    "processing": "Progress: Converting format...",
    "publishing": "Progress: Uploading to channel...",
}
_LOG_LABELS = {"done": "OK", "failed": "FAIL", "downloading": "...", "publishing": "..."}


async def tasks_page(task_queue, keyboard: InlineKeyboardMarkup | None = None) -> Card:
    db = await get_db()
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    active = task_queue.get_active_tasks()
    lines = _Lines()
    lines.add(f"## {md(f'Active Downloads ({len(active)}/{max_concurrent})')}",
              f"<b>{h(f'Active Downloads ({len(active)}/{max_concurrent})')}:</b>")
    if not active:
        lines.text("No active tasks.")
    for t in active:
        lines.blank()
        lines.bold(f"#{t['task_id']} — {_STATUS_LABELS.get(t['status'], t['status'])}")
        url = _short(t["url"], 50)
        lines.add(f"URL: {md_code(url)}", f"URL: <code>{h(url)}</code>")
        if t["status"] == "downloading":
            bar = f"[{progress_bar(t['progress'])}] {t['progress']:.1f}%"
            lines.add(f"Progress: {md_code(bar)}", f"Progress: <code>{h(bar)}</code>")
        elif t["status"] in _STATUS_NOTES:
            lines.text(_STATUS_NOTES[t["status"]])
        lines.text(f"Elapsed: {fmt_duration(t['elapsed'])}")

    queued = task_queue.get_queued_tasks()
    if queued:
        lines.blank()
        lines.add(f"## {md(f'Queued ({len(queued)})')}", f"<b>{h(f'Queued ({len(queued)})')}:</b>")
        for t in queued:
            url = _short(t["url"], 45)
            wait = f"(waiting {fmt_duration(t['elapsed'])})"
            lines.add(f"{t['position']}\\. {md_code(url)} {md(wait)}", f"{t['position']}. <code>{h(url)}</code> {h(wait)}")

    recent = await list_recent_post_logs(db, limit=5)
    if recent:
        lines.blank()
        lines.add("## Recent Tasks", "<b>Recent Tasks:</b>")
        for r in recent:
            label = _LOG_LABELS.get(r["status"], r["status"])
            url = _short(r["source_url"], 45)
            lines.add(f"{md(f'[{label}]')} {md_code(url)}", f"[{h(label)}] <code>{h(url)}</code>")
            if r["error_message"]:
                lines.text(f"Error: {r['error_message'][:60]}")
    return lines.card(keyboard)


async def channels_page(keyboard: InlineKeyboardMarkup | None = None) -> Card:
    channels = await list_channels(await get_db())
    lines = _Lines()
    if not channels:
        lines.text("No channels configured.")
        return lines.card(keyboard)
    lines.add(f"## {md(f'Channels ({len(channels)})')}", f"<b>{h(f'Channels ({len(channels)})')}:</b>")
    for ch in channels:
        lines.add(f"{md(ch['title'])} {md_code(ch['chat_id'])}", f"{h(ch['title'])} (<code>{ch['chat_id']}</code>)")
    return lines.card(keyboard)


async def admins_page(keyboard: InlineKeyboardMarkup | None = None) -> Card:
    admins = await list_admins(await get_db())
    lines = _Lines()
    if not admins:
        lines.text("No admins configured.")
        return lines.card(keyboard)
    lines.add(f"## {md(f'Admins ({len(admins)})')}", f"<b>{h(f'Admins ({len(admins)})')}:</b>")
    for a in admins:
        name = a["username"] or "N/A"
        lines.add(f"{md_code(a['user_id'])} — {md(name)}", f"<code>{a['user_id']}</code> — {h(name)}")
    return lines.card(keyboard)


async def settings_page(*, with_buttons: bool) -> Card:
    """所有设置的当前值；with_buttons 时每个可点选的设置一个入口按钮，再加 Back。"""
    settings = await get_all_settings(await get_db())
    lines = _Lines()
    lines.add("## Settings", "<b>Settings:</b>")
    for k, v in sorted(settings.items()):
        lines.add(f"{md_code(k)} \\= {md_code(v)}", f"<code>{h(k)}</code> = <code>{h(v)}</code>")
    if not with_buttons:
        return lines.card()
    keyboard = [[_btn(f"{k}: {settings.get(k, '?')}", f"set:{k}")] for k in sorted(SETTING_PRESETS)]
    return lines.card(InlineKeyboardMarkup(keyboard + [[_btn("« Back", "menu:back")]]))


async def setting_presets_page(key: str) -> Card:
    """某个设置的预设值按钮，当前值用 primary 高亮；当前值不在预设里时不高亮任何按钮。"""
    current = await get_setting(await get_db(), key)
    lines = _Lines()
    lines.add(f"## {md_code(key)}", f"<b><code>{h(key)}</code></b>")
    lines.add(f"Current value: {md_code(current)}", f"Current value: <code>{h(current)}</code>")
    lines.text("Pick a preset, or use /set for other values.")
    buttons = [_btn(label, f"set:{key}:{value}", "primary" if value == current else None)
               for value, label in SETTING_PRESETS[key]]
    return lines.card(InlineKeyboardMarkup(_rows(buttons, 3) + [[_btn("« Back", "menu:settings")]]))


def help_page(keyboard: InlineKeyboardMarkup | None = None) -> Card:
    lines = _Lines()
    lines.add("# Video Forward Bot", "<b>Video Forward Bot</b>")
    lines.blank()
    lines.text("Send me a video link and I'll publish it to a Telegram channel.")
    lines.text("Supports YouTube, X/Twitter, TikTok, Instagram, and 1000+ sites.")
    lines.blank()
    lines.add("## How to use", "<b>How to use:</b>")
    steps = [
        "Send a video link",
        "Send a caption, or tap Skip caption",
        "Choose resolution and target channel",
        "Check the summary card and tap Confirm",
        "Follow the status message until the post is published",
    ]
    for i, step in enumerate(steps, start=1):
        lines.add(f"{i}\\. {md(step)}", f"{i}. {h(step)}")
    lines.blank()
    lines.add("## Caption format (HTML)", "<b>Caption format (HTML):</b>")
    tags = ["<b>", "<i>", "<u>", "<s>", '<a href="...">', "<code>", "<pre>"]
    lines.add("Supported tags: " + " ".join(md_code(t) for t in tags),
              "Supported tags: " + " ".join(f"<code>{h(t)}</code>" for t in tags))
    lines.text("Max length: 1024 characters (text only, tags don't count)")
    lines.blank()
    lines.add("## Admin commands", "<b>Admin commands:</b>")
    for cmd, desc in [
        ("/add_channel <chat_id>", "Add channel"), ("/remove_channel <chat_id>", "Remove channel"),
        ("/list_channels", "List channels"), ("/set <key> <value>", "Update setting"),
        ("/settings", "View all settings"), ("/tasks", "View active downloads & recent tasks"),
    ]:
        lines.text(f"{cmd} — {desc}")
    lines.blank()
    lines.add("## Super admin commands", "<b>Super admin commands:</b>")
    for cmd, desc in [
        ("/add_admin <user_id>", "Add admin"), ("/remove_admin <user_id>", "Remove admin"),
        ("/list_admins", "List admins"),
    ]:
        lines.text(f"{cmd} — {desc}")
    return lines.card(keyboard)
