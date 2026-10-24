"""Channel 的显示名称和 username（用来拼帖子链接）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def channel_info(bot, configured: list[dict], chat_id: int) -> tuple[str, str | None]:
    """返回 (显示名称, username)。

    名称依次取已配置 Channel 的标题、Bot 取到的 chat 标题、chat_id 数字；
    取不到 username 时帖子链接按私有 Channel 的格式拼。
    """
    title = next((ch["title"] for ch in configured if ch["chat_id"] == chat_id), None)
    username = None
    try:
        chat = await bot.get_chat(chat_id)
        username = getattr(chat, "username", None)
        title = title or getattr(chat, "title", None)
    except Exception as e:
        logger.warning("Failed to get chat info for channel %d: %s", chat_id, e)
    return title or str(chat_id), username
