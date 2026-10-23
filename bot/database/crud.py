from __future__ import annotations
import aiosqlite

_SETTINGS_VALIDATORS = {
    "max_resolution": {"type": "enum", "values": ["360", "480", "720", "1080", "1440", "2160"]},
    "max_file_size_mb": {"type": "int_range", "min": 1, "max": 2000},
    "max_concurrent_downloads": {"type": "int_range", "min": 1, "max": 5},
}

def _validate_setting(key: str, value: str) -> None:
    if key not in _SETTINGS_VALIDATORS:
        raise ValueError(f"Unknown setting: '{key}'")
    spec = _SETTINGS_VALIDATORS[key]
    if spec["type"] == "enum":
        if value not in spec["values"]:
            raise ValueError(f"'{key}' must be one of: {', '.join(spec['values'])}")
    elif spec["type"] == "int_range":
        try:
            v = int(value)
        except ValueError:
            raise ValueError(f"'{key}' must be an integer") from None
        if not (spec["min"] <= v <= spec["max"]):
            raise ValueError(f"'{key}' must be between {spec['min']} and {spec['max']}")

async def add_admin(db: aiosqlite.Connection, *, user_id: int, username: str | None = None) -> None:
    await db.execute("INSERT INTO admins (user_id, username) VALUES (?, ?)", (user_id, username))
    await db.commit()

async def remove_admin(db: aiosqlite.Connection, *, user_id: int) -> bool:
    cursor = await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    await db.commit()
    return cursor.rowcount > 0

async def list_admins(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT user_id, username, created_at FROM admins ORDER BY created_at")
    return [dict(row) for row in await cursor.fetchall()]

async def is_admin(db: aiosqlite.Connection, *, user_id: int) -> bool:
    cursor = await db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return await cursor.fetchone() is not None

async def add_channel(db: aiosqlite.Connection, *, chat_id: int, title: str) -> None:
    await db.execute("INSERT INTO channels (chat_id, title) VALUES (?, ?)", (chat_id, title))
    await db.commit()

async def remove_channel(db: aiosqlite.Connection, *, chat_id: int) -> bool:
    cursor = await db.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
    await db.commit()
    return cursor.rowcount > 0

async def list_channels(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT chat_id, title, created_at FROM channels ORDER BY created_at")
    return [dict(row) for row in await cursor.fetchall()]

async def get_setting(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None

async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    _validate_setting(key, value)
    await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    await db.commit()

async def get_all_settings(db: aiosqlite.Connection) -> dict[str, str]:
    cursor = await db.execute("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in await cursor.fetchall()}

async def create_post_log(db: aiosqlite.Connection, *, admin_user_id: int, source_url: str, channel_chat_id: int, caption: str) -> int:
    cursor = await db.execute(
        "INSERT INTO post_logs (admin_user_id, source_url, channel_chat_id, caption, status) VALUES (?, ?, ?, ?, 'downloading')",
        (admin_user_id, source_url, channel_chat_id, caption),
    )
    await db.commit()
    return cursor.lastrowid

async def update_post_log_status(db: aiosqlite.Connection, log_id: int, *, status: str, message_id: int | None = None, error_message: str | None = None) -> None:
    fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list = [status]
    if message_id is not None:
        fields.append("message_id = ?")
        params.append(message_id)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    params.append(log_id)
    await db.execute(f"UPDATE post_logs SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()


async def list_recent_post_logs(db: aiosqlite.Connection, *, limit: int = 5) -> list[dict]:
    cursor = await db.execute(
        "SELECT source_url, status, error_message, created_at FROM post_logs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def fail_interrupted_post_logs(db: aiosqlite.Connection) -> int:
    # 启动时调用：上次退出时还停在下载或发布阶段的记录，标记为失败
    cursor = await db.execute(
        "UPDATE post_logs SET status = 'failed', error_message = 'Interrupted by restart', updated_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('downloading', 'publishing')"
    )
    await db.commit()
    return cursor.rowcount


async def create_pending_task(db: aiosqlite.Connection, *, user_id: int, user_chat_id: int, source_url: str, caption: str, channel_chat_id: int, max_resolution: int) -> int:
    cursor = await db.execute(
        "INSERT INTO pending_tasks (user_id, user_chat_id, source_url, caption, channel_chat_id, max_resolution) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, user_chat_id, source_url, caption, channel_chat_id, max_resolution),
    )
    await db.commit()
    return cursor.lastrowid


async def list_pending_tasks(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT id, user_id, user_chat_id, source_url, caption, channel_chat_id, max_resolution, status "
        "FROM pending_tasks ORDER BY id"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def mark_pending_task_running(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute("UPDATE pending_tasks SET status = 'running' WHERE id = ?", (task_id,))
    await db.commit()


async def delete_pending_task(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute("DELETE FROM pending_tasks WHERE id = ?", (task_id,))
    await db.commit()
