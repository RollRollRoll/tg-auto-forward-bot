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
        self._next_id: int = 0
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    async def try_acquire_slot(self, max_slots: int, *, url: str = "", user_id: int = 0) -> int | None:
        async with self._lock:
            if len(self._tasks) >= max_slots:
                return None
            self._next_id += 1
            self._tasks[self._next_id] = {
                "url": url,
                "user_id": user_id,
                "start_time": time.time(),
                "progress": 0.0,
                "status": "waiting",
            }
            return self._next_id

    def update_progress(self, task_id: int, progress: float, status: str = "downloading") -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["progress"] = progress
            self._tasks[task_id]["status"] = status

    async def release_slot(self, task_id: int | None = None) -> None:
        async with self._lock:
            if task_id is not None:
                self._tasks.pop(task_id, None)
            else:
                # fallback: remove the oldest task
                if self._tasks:
                    oldest = min(self._tasks)
                    del self._tasks[oldest]

    def get_active_tasks(self) -> list[dict]:
        now = time.time()
        return [
            {"task_id": tid, "elapsed": now - t["start_time"], **t}
            for tid, t in self._tasks.items()
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
