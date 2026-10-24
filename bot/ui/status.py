"""status message：每个 Task 在管理员私聊里的一条状态消息，随进度原地更新。

pipeline 只报告事件（start / position_changed / downloading / progress / converting / uploading / finished），
渲染、发送、编辑、限速和 HTML 兜底都在这里完成。所有方法都不抛异常，失败只记日志，不影响 Task 执行。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.markup import fmt_duration, fmt_size, h, md, md_code, md_join, progress_bar
from bot.ui.sender import Card, RichSender, SentMessage

logger = logging.getLogger(__name__)

# 同一个聊天里相邻两次发送或编辑的最小间隔（Telegram 对单个聊天约每秒 1 条）
CHAT_EDIT_INTERVAL = 1.5
# 同一条 status message 的进度刷新最小间隔
PROGRESS_INTERVAL = 3.0


class ChatEditLimiter:
    """按聊天限速，全进程共用一个实例。阶段变化等到可用时间再发；进度刷新来不及就直接丢弃。"""

    def __init__(self, *, interval: float = CHAT_EDIT_INTERVAL, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock = clock
        self._interval = interval
        self._sleep = sleep
        self._last: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _ready_at(self, chat_id: int) -> float:
        last = self._last.get(chat_id)
        return float("-inf") if last is None else last + self._interval

    @asynccontextmanager
    async def turn(self, chat_id: int, *, wait: bool = True):
        """占用这个聊天的一次发送机会，yield 是否拿到；wait=False 时拿不到立刻得到 False。

        拿到后在退出前一直持有锁，保证发送本身也按顺序、按间隔进行。
        """
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if not wait and (lock.locked() or self.clock() < self._ready_at(chat_id)):
            yield False
            return
        async with lock:
            delay = self._ready_at(chat_id) - self.clock()
            if delay > 0:
                await self._sleep(delay)
            self._last[chat_id] = self.clock()
            yield True


# 阶段只能前进不能后退；done / failed 是终态
_RANK = {"queued": 0, "starting": 1, "downloading": 2, "converting": 3, "uploading": 4, "done": 5, "failed": 5}
_ICONS = {
    "queued": "⏳", "starting": "⏳", "downloading": "⬇️", "converting": "⚙️",
    "uploading": "⬆️", "done": "✅", "failed": "❌",
}


class StatusReporter:
    """一个 Task 的 status message。由 pipeline 创建并持有，每个 Task 一个实例。"""

    def __init__(self, sender: RichSender, limiter: ChatEditLimiter, *, chat_id: int, url: str,
                 channel_title: str, resolution: int, restored: bool = False):
        self._sender = sender
        self._limiter = limiter
        self._clock = limiter.clock
        self._chat_id = chat_id
        self._url = url
        self._channel_title = channel_title
        self._resolution = resolution
        self._restored = restored

        self._stage = "queued"
        self._position = 0
        self._progress: tuple[float, float | None, float | None, float | None] | None = None
        self._detail = ""
        self._post_url: str | None = None
        self._started_at: float | None = None

        self._sent: SentMessage | None = None
        self._last_edit = float("-inf")
        # 还没显示的阶段变化，按发生顺序逐个显示，一个都不能丢
        self._pending_stages: list[tuple[str, int]] = []
        self._progress_dirty = False
        self._worker: asyncio.Task | None = None
        # 收到 finished 后不再接受新事件；_finished 表示最终状态已经处理完
        self._closed = False
        self._finished = False

    # ---- pipeline 报告的事件（除 finished 外都是同步方法，可以在回调里直接调用） ----

    def start(self, position: int) -> None:
        """Task 已提交：position > 0 表示在排队，0 表示已拿到 slot。"""
        self._started_at = self._clock()
        self._position = position
        self._stage = "queued" if position > 0 else "starting"
        self._kick(stage=True)

    def position_changed(self, position: int) -> None:
        if self._closed or self._stage != "queued" or position == self._position:
            return
        self._position = position
        self._kick(stage=True)

    def running(self) -> None:
        """排队的 Task 拿到了 slot，还没开始下载。"""
        self._advance("starting")

    def downloading(self) -> None:
        self._advance("downloading")

    def converting(self) -> None:
        self._advance("converting")

    def uploading(self) -> None:
        self._advance("uploading")

    def progress(self, *, downloaded: float, total: float | None, speed: float | None, eta: float | None) -> None:
        if self._closed or self._stage != "downloading":
            return
        self._progress = (downloaded, total, speed, eta)
        self._kick(stage=False)

    async def finished(self, *, success: bool, detail: str = "", post_url: str | None = None) -> None:
        """编辑成最终状态，再另发一条短消息触发推送。只生效一次。"""
        if self._closed:
            return
        self._closed = True
        # 先把排着的阶段变化显示完，再显示最终状态
        self._progress_dirty = False
        await self.wait_idle()
        self._finished = True
        self._stage = "done" if success else "failed"
        self._detail, self._post_url = detail, post_url
        card = self._card(self._stage, self._position)
        try:
            async with self._limiter.turn(self._chat_id):
                if self._sent is None:
                    self._sent = await self._sender.send(self._chat_id, card)
                elif not await self._sender.edit(self._sent, card):
                    # 最终状态不能丢：编辑失败时另发一条 HTML 新消息
                    self._sent = await self._sender.send_html(self._chat_id, card)
        except Exception:
            logger.warning("Failed to deliver final status in chat %d", self._chat_id, exc_info=True)
        push = f"✅ Published to {self._channel_title}" if success else (
            f"❌ Task failed: {detail}" if detail else "❌ Task failed")
        try:
            async with self._limiter.turn(self._chat_id):
                await self._sender.send_text(self._chat_id, push)
        except Exception:
            logger.warning("Failed to send result notification in chat %d", self._chat_id, exc_info=True)

    async def wait_idle(self) -> None:
        """等待已排上的刷新发完。"""
        while self._worker is not None and not self._worker.done():
            await self._worker

    # ---- 内部：刷新调度 ----

    def _advance(self, stage: str) -> None:
        if self._closed or _RANK[stage] <= _RANK[self._stage]:
            return
        self._stage = stage
        self._progress = None
        self._kick(stage=True)

    def _kick(self, *, stage: bool) -> None:
        if self._closed:
            return
        if stage:
            self._pending_stages.append((self._stage, self._position))
        else:
            self._progress_dirty = True
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._flush())

    async def _flush(self) -> None:
        try:
            while self._pending_stages or (self._progress_dirty and not self._closed):
                if self._pending_stages:
                    # 阶段变化不能丢：等到这个聊天可以发送为止，按顺序逐个显示
                    stage, position = self._pending_stages.pop(0)
                    async with self._limiter.turn(self._chat_id):
                        await self._deliver(stage, position)
                    continue
                self._progress_dirty = False
                if self._clock() - self._last_edit < PROGRESS_INTERVAL:
                    # 太频繁的进度刷新直接丢弃，下一次刷新会带上最新进度
                    continue
                async with self._limiter.turn(self._chat_id, wait=False) as ok:
                    if ok and not self._closed:
                        await self._deliver(self._stage, self._position)
        except Exception:
            logger.warning("Status update failed in chat %d", self._chat_id, exc_info=True)

    async def _deliver(self, stage: str, position: int) -> None:
        card = self._card(stage, position)
        self._last_edit = self._clock()
        if self._sent is None:
            try:
                self._sent = await self._sender.send(self._chat_id, card)
            except Exception:
                # 这次没发出去，下一次刷新时再发
                logger.warning("Failed to send status message in chat %d", self._chat_id, exc_info=True)
            return
        await self._sender.edit(self._sent, card)

    # ---- 内部：渲染 ----

    @staticmethod
    def _stage_label(stage: str, position: int) -> str:
        match stage:
            case "queued":
                return f"Queued (#{position})"
            case "starting":
                return "Starting"
            case "downloading":
                return "Downloading"
            case "converting":
                return "Converting"
            case "uploading":
                return "Uploading"
            case "done":
                return "Done"
        return "Failed"

    def _progress_lines(self, stage: str) -> tuple[str, str] | None:
        # 只有正在下载时才显示进度；已经进入下一阶段的旧快照不显示
        if stage != "downloading" or self._stage != "downloading" or self._progress is None:
            return None
        downloaded, total, speed, eta = self._progress
        speed_text = f"{fmt_size(speed)}/s" if speed else None
        if total:
            pct = min(downloaded / total * 100, 100.0)
            parts = [f"{fmt_size(downloaded)} / {fmt_size(total)}"]
            if speed_text:
                parts.append(speed_text)
            if eta is not None:
                parts.append(f"ETA {fmt_duration(eta)}")
            bar = f"[{progress_bar(pct)}] {pct:.1f}%"
            tail = " · ".join(parts)
            return f"{md_code(bar)} {md(tail)}", f"<code>{h(bar)}</code> {h(tail)}"
        parts = [fmt_size(downloaded)] + ([speed_text] if speed_text else [])
        tail = " · ".join(parts)
        return md(tail), h(tail)

    def _card(self, stage: str, position: int) -> Card:
        icon, label = _ICONS[stage], self._stage_label(stage, position)
        elapsed = fmt_duration(self._clock() - self._started_at) if self._started_at is not None else "0s"
        md_lines: list[str] = []
        html_lines: list[str] = []
        if self._restored:
            md_lines.append("🔄 Bot restarted, resuming")
            html_lines.append("🔄 Bot restarted, resuming")
        # 头部：这个 Task 的链接、Channel、分辨率，同时运行多个 Task 时用来区分
        md_lines += [f"Link: {md_code(self._url)}",
                     f"Channel: {md(self._channel_title)}", f"Resolution: {self._resolution}p", ""]
        html_lines += [f"Link: <code>{h(self._url)}</code>",
                       f"Channel: {h(self._channel_title)}", f"Resolution: {self._resolution}p", ""]
        md_lines.append(f"**{icon} {md(label)}**")
        html_lines.append(f"<b>{icon} {h(label)}</b>")
        progress = self._progress_lines(stage)
        if progress:
            md_lines.append(progress[0])
            html_lines.append(progress[1])
        if stage == "failed" and self._detail:
            md_lines.append(md(self._detail))
            html_lines.append(h(self._detail))
        md_lines.append(f"Elapsed: {md(elapsed)}")
        html_lines.append(f"Elapsed: {h(elapsed)}")

        keyboard = None
        if stage == "done" and self._post_url:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open post", url=self._post_url)]])
        return Card(markdown=md_join(md_lines), html="\n".join(html_lines), keyboard=keyboard)
