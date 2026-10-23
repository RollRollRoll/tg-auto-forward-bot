import aiosqlite

_DEFAULT_SETTINGS = {
    "max_resolution": "1080",
    "max_file_size_mb": "2000",
    "max_concurrent_downloads": "2",
}


async def create_tables(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id BIGINT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS post_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id BIGINT NOT NULL,
            source_url TEXT NOT NULL,
            channel_chat_id BIGINT NOT NULL,
            message_id INTEGER,
            caption TEXT,
            status TEXT NOT NULL DEFAULT 'downloading',
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 已提交但尚未完成的任务，重启后据此恢复队列。status: queued / running
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            user_chat_id BIGINT NOT NULL,
            source_url TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            channel_chat_id BIGINT NOT NULL,
            max_resolution INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Insert default settings (skip if already exist)
    for key, value in _DEFAULT_SETTINGS.items():
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    await db.commit()
