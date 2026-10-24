"""菜单各页和列表命令的内容。菜单页和对应命令（/tasks、/settings、/list_*）共用同一份渲染。"""
from __future__ import annotations

from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup

from bot.config import SUPER_ADMIN_ID
from bot.database.connection import get_db
from bot.database.crud import get_all_settings, get_setting, list_admins, list_channels, list_finished_post_logs
from bot.ui.keyboards import btn as _btn, rows as _rows
from bot.ui.channels import channel_info
from bot.ui.markup import fmt_duration, h, md, md_code, md_join, post_link, progress_bar, relative_time, short_url
from bot.ui.sender import Card
from bot.ui.stages import QUEUE_STATUS_STAGES, STAGE_ICONS, STAGE_NAMES

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


# 菜单里的 Tasks 页沿用 menu:tasks 刷新；/tasks 的回复用 tasks:refresh，刷新后仍然只有 Refresh
TASKS_MENU_KEYBOARD = InlineKeyboardMarkup([[_btn("🔄 Refresh", "menu:tasks"), _btn("« Back", "menu:back")]])
TASKS_COMMAND_KEYBOARD = InlineKeyboardMarkup([[_btn("🔄 Refresh", "tasks:refresh")]])


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


_REASON_LIMIT = 100
_RECENT_LIMIT = 5


def utc_now() -> datetime:
    """当前 UTC 时间；测试里替换成固定值，让相对时间稳定复现。"""
    return datetime.now(timezone.utc)


def _finished_at(value: str) -> datetime:
    # post_logs 的时间由 SQLite CURRENT_TIMESTAMP 写入，是 UTC 的 "YYYY-MM-DD HH:MM:SS"
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _reason(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[:_REASON_LIMIT] + "…"


async def tasks_page(bot, task_queue, keyboard: InlineKeyboardMarkup | None = None) -> Card:
    """Tasks 页：摘要行，In progress / Queued / Recent 三节。"""
    db = await get_db()
    max_concurrent = int(await get_setting(db, "max_concurrent_downloads") or "2")
    channels = _Channels(bot, await list_channels(db))
    active = task_queue.get_active_tasks()
    queued = task_queue.get_queued_tasks()

    lines = _Lines()
    lines.add("# 📋 Tasks", "<b>📋 Tasks</b>")
    lines.text(f"Running {len(active)}/{max_concurrent} · Queued {len(queued)}")

    lines.blank()
    lines.add("## In progress", "<b>In progress</b>")
    if not active:
        lines.text("No active tasks.")
    for t in active:
        stage = QUEUE_STATUS_STAGES.get(t["status"], "starting")
        channel = await channels.title(t["channel_chat_id"])
        lines.text(f"{STAGE_ICONS[stage]} {STAGE_NAMES[stage]} · {channel} · {fmt_duration(t['elapsed'])}")
        _link_line(lines, t["url"])
        if stage == "downloading":
            bar = f"[{progress_bar(t['progress'])}] {t['progress']:.1f}%"
            lines.add(md_code(bar), f"<code>{h(bar)}</code>")

    if queued:
        lines.blank()
        lines.add("## Queued", "<b>Queued</b>")
        for t in queued:
            channel = await channels.title(t["channel_chat_id"])
            head = f"{t['position']}. {channel} · waiting {fmt_duration(t['elapsed'])}"
            lines.text(head)
            _link_line(lines, t["url"])

    lines.blank()
    lines.add("## Recent", "<b>Recent</b>")
    recent = await list_finished_post_logs(db, limit=_RECENT_LIMIT)
    if not recent:
        lines.text("No tasks yet.")
    now = utc_now()
    for r in recent:
        title, username = await channels.info(r["channel_chat_id"])
        done = r["status"] == "done"
        ago = relative_time((now - _finished_at(r["updated_at"])).total_seconds())
        head = f"{STAGE_ICONS['done' if done else 'failed']} {title} · {ago}"
        if done and r["message_id"]:
            url = post_link(r["channel_chat_id"], username, r["message_id"])
            lines.add(f"{md(head)} · [Open post]({url})", f'{h(head)} · <a href="{h(url)}">Open post</a>')
        else:
            lines.text(head)
        _link_line(lines, r["source_url"])
        if not done and r["error_message"]:
            lines.text(_reason(r["error_message"]))
    return lines.card(keyboard)


class _Channels:
    """Tasks 页里的 Channel 名称和 username，每个 Channel 最多向 Bot 查一次。"""

    def __init__(self, bot, configured: list[dict]):
        self._bot = bot
        self._configured = configured
        self._titles = {ch["chat_id"]: ch["title"] for ch in configured}
        self._cache: dict[int, tuple[str, str | None]] = {}

    async def info(self, chat_id: int) -> tuple[str, str | None]:
        # Recent 要拼帖子链接，需要 username，所以已配置的 Channel 也要查
        if chat_id not in self._cache:
            self._cache[chat_id] = await channel_info(self._bot, self._configured, chat_id)
        return self._cache[chat_id]

    async def title(self, chat_id: int) -> str:
        # 只要名称：已配置的直接用标题，不在配置里（例如提交后被删掉）才向 Bot 查
        if chat_id in self._titles:
            return self._titles[chat_id]
        return (await self.info(chat_id))[0]


def _link_line(lines: _Lines, url: str) -> None:
    short = short_url(url)
    lines.add(md_code(short), f"<code>{h(short)}</code>")


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
