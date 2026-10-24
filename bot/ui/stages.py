"""Task 阶段的名称和图标，status message 和 Tasks 页共用，保证两处说法一致。"""
from __future__ import annotations

STAGE_ICONS = {
    "queued": "⏳", "starting": "⏳", "downloading": "⬇️", "converting": "⚙️",
    "uploading": "⬆️", "done": "✅", "failed": "❌",
}

STAGE_NAMES = {
    "starting": "Starting", "downloading": "Downloading", "converting": "Converting",
    "uploading": "Uploading", "done": "Done", "failed": "Failed",
}

# TaskQueue 记录的状态 → 阶段（下载完成后的 ffmpeg 后处理叫 processing，上传叫 publishing）
QUEUE_STATUS_STAGES = {
    "starting": "starting", "downloading": "downloading",
    "processing": "converting", "publishing": "uploading",
}
