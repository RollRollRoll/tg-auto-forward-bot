from __future__ import annotations
import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import yt_dlp

from bot.config import COOKIES_FILE, DOWNLOAD_DIR


class DownloadSlotManager:
    def __init__(self):
        self._tasks: dict[int, dict] = {}
        # 排队中的任务，dict 保持插入顺序，即先进先出
        self._queue: dict[int, dict] = {}
        self._waiters: dict[int, asyncio.Future] = {}
        self._max_slots: int = 1
        self._next_id: int = 0
        self._closed: bool = False
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    async def enqueue(self, max_slots: int, *, url: str = "", user_id: int = 0) -> tuple[int, int] | None:
        """登记一个任务，返回 (task_id, 排队位置)。

        有空闲槽位时直接占用，位置为 0；否则进入队列，位置从 1 开始，
        之后需要调用 wait_for_slot() 等待轮到自己。已关闭时返回 None。
        """
        async with self._lock:
            if self._closed:
                return None
            # 每次登记都以最新的并发设置为准；设置调大后可以立刻放行排队任务
            self._max_slots = max_slots
            self._promote()
            self._next_id += 1
            task_id = self._next_id
            info = {
                "url": url,
                "user_id": user_id,
                "start_time": time.time(),
                "progress": 0.0,
                "status": "waiting",
            }
            # 队列里还有人时不能插队，即使恰好有空闲槽位
            if not self._queue and len(self._tasks) < max_slots:
                self._tasks[task_id] = info
                return task_id, 0
            info["status"] = "queued"
            self._queue[task_id] = info
            self._waiters[task_id] = asyncio.get_running_loop().create_future()
            return task_id, len(self._queue)

    async def wait_for_slot(self, task_id: int) -> bool:
        """等待排队任务拿到槽位。拿到返回 True；管理器关闭时返回 False。"""
        waiter = self._waiters.get(task_id)
        if waiter is None:
            return task_id in self._tasks
        return await waiter

    def _promote(self) -> None:
        # 按入队顺序把排队任务移入空闲槽位
        while self._queue and len(self._tasks) < self._max_slots:
            task_id = next(iter(self._queue))
            info = self._queue.pop(task_id)
            waiter = self._waiters.pop(task_id, None)
            if waiter is None or waiter.done():
                # 等待方已被取消，跳过，不占用槽位
                continue
            info["status"] = "waiting"
            info["start_time"] = time.time()
            self._tasks[task_id] = info
            waiter.set_result(True)

    def close(self) -> None:
        """停止接收新任务，并让所有排队任务的 wait_for_slot() 返回 False。

        Application.stop() 会等待所有后台任务结束，所以必须在它之前调用，
        否则重启时要等整个队列下载完才能退出。
        """
        self._closed = True
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_result(False)
        self._waiters.clear()
        self._queue.clear()

    def update_progress(self, task_id: int, progress: float, status: str = "downloading") -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["progress"] = progress
            self._tasks[task_id]["status"] = status

    async def release_slot(self, task_id: int | None = None) -> None:
        async with self._lock:
            if task_id is not None:
                self._tasks.pop(task_id, None)
                # 排队中途结束的任务（如发送排队通知失败）也要移出队列
                self._queue.pop(task_id, None)
                waiter = self._waiters.pop(task_id, None)
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            else:
                # fallback: remove the oldest task
                if self._tasks:
                    oldest = min(self._tasks)
                    del self._tasks[oldest]
            self._promote()

    def get_active_tasks(self) -> list[dict]:
        now = time.time()
        return [
            {"task_id": tid, "elapsed": now - t["start_time"], **t}
            for tid, t in self._tasks.items()
        ]

    def get_queued_tasks(self) -> list[dict]:
        now = time.time()
        return [
            {"task_id": tid, "position": pos, "elapsed": now - t["start_time"], **t}
            for pos, (tid, t) in enumerate(self._queue.items(), start=1)
        ]


async def extract_available_resolutions(url: str) -> list[int]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.isfile(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _do_extract, ydl_opts, url)

    formats = info.get("formats") or []
    sides: set[int] = set()
    for f in formats:
        side = _short_side(f)
        if side:
            sides.add(side)

    return sorted(sides)


def _short_side(f: dict) -> int | None:
    # 分辨率按短边计算：横屏取高度，竖屏取宽度（1080x1920 的竖屏视频算 1080p）
    w, h = f.get("width"), f.get("height")
    if h and h > 0:
        return min(w, h) if w and w > 0 else h
    return None


def _build_format_spec(max_resolution: int) -> str:
    # 短边不超过 max_resolution：横屏（宽高比 >= 1）限制高度，竖屏限制宽度
    limits = (
        f"[aspect_ratio>=1][height<={max_resolution}]",
        f"[aspect_ratio<1][width<={max_resolution}]",
    )
    # 按优先级依次尝试：H.264 + AAC → H.264 + 任意音频 → H.264 单文件 → 任意编码。
    # bestvideo* 会把音视频合一的格式也算进来，避免低分辨率的合一格式抢在高分辨率之前被选中
    templates = (
        "bestvideo*[vcodec^=avc]{f}+bestaudio[acodec^=mp4a]",
        "bestvideo*[vcodec^=avc]{f}+bestaudio",
        "best[vcodec^=avc]{f}",
        "bestvideo*{f}+bestaudio",
        "best{f}",
    )
    selectors = [t.format(f=lim) for t in templates for lim in limits]
    # 格式缺少宽度时只能按高度限制（与 _short_side 的口径一致），再不行就不做限制
    selectors += [
        f"bestvideo*[height<={max_resolution}]+bestaudio", f"best[height<={max_resolution}]",
        "bestvideo*+bestaudio", "best",
    ]
    return "/".join(selectors)


def _do_extract(ydl_opts: dict, url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info or {}


def check_disk_space(max_concurrent: int, max_file_size_mb: int) -> tuple[bool, int]:
    usage = shutil.disk_usage(DOWNLOAD_DIR)
    free_mb = usage.free // (1024 * 1024)
    required_mb = max_concurrent * max_file_size_mb * 2
    return free_mb >= required_mb, free_mb


def cleanup_stale_files(max_age_seconds: int = 3600) -> int:
    download_path = Path(DOWNLOAD_DIR)
    if not download_path.exists():
        return 0
    removed = 0
    now = time.time()
    for item in download_path.iterdir():
        if (now - item.stat().st_mtime) > max_age_seconds:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
    return removed


async def download_video(
    url: str,
    *,
    max_resolution: int = 1080,
    progress_callback: callable | None = None,
) -> dict:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(dir=DOWNLOAD_DIR)
    os.chmod(tmp_dir, 0o755)

    ydl_opts = {
        "format": _build_format_spec(max_resolution),
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
        ],
        "postprocessor_args": {
            "ffmpeg": ["-movflags", "+faststart"],
        },
        "quiet": True,
        "no_warnings": True,
    }

    if progress_callback is not None:
        ydl_opts["progress_hooks"] = [progress_callback]

    if os.path.isfile(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _do_download, ydl_opts, url)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Download failed: {e}") from e

    files = list(Path(tmp_dir).glob("*"))
    if not files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("Download produced no files")

    file_path = files[0]
    return {
        "file_path": str(file_path),
        "tmp_dir": tmp_dir,
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
        "title": info.get("title", ""),
        "file_size_mb": file_path.stat().st_size / (1024 * 1024),
    }


def _do_download(ydl_opts: dict, url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info or {}
