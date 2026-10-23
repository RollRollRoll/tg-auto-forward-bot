# Telegram X Video Forward Bot - Design Spec

> **Revision 2026-10-22:** download tasks are no longer rejected when all slots are busy. They wait in a FIFO queue persisted in the `pending_tasks` table and resume after a restart. Affected sections: background task steps, `pending_tasks`, Error Handling, Concurrency and Disk Safety. Other sections still describe the original design and may be outdated in details (e.g. multi-platform support, resolution picker).

## Overview

A Telegram Bot that receives Twitter (X) video links from authorized users via private chat, downloads the video using yt-dlp, and publishes it with user-provided rich text caption to a designated Telegram Channel. Uses a Local Bot API Server to support uploads up to 2000MB.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| Bot Framework | python-telegram-bot v20+ (async) |
| Video Download | yt-dlp (Python API) |
| Database | SQLite via aiosqlite |
| Bot API | Telegram Local Bot API Server (2000MB upload limit) |
| Deployment | Docker Compose / Direct run |

## Architecture

```
User (Private Chat)
    │
    ▼
┌─────────────────────────┐
│   Telegram Bot           │
│  (python-telegram-bot)   │
│                          │
│  ConversationHandler     │
│  链接 → 文案 → Channel   │
│         │                │
│    Downloader (yt-dlp)   │
│         │                │
│    Publisher (send_video) │
│         │                │
│    Database (SQLite)     │
└─────────────────────────┘
          │
          ▼
┌─────────────────────────┐
│ Local Bot API Server     │
│ (telegram-bot-api)       │
│ Upload limit: 2000MB     │
└─────────────────────────┘
          │
          ▼
    Telegram Channel(s)
```

### Core Modules

| Module | Responsibility |
|--------|---------------|
| **Bot Handler** | User interaction, conversation state management (link → caption → channel selection → download → publish) |
| **Downloader** | yt-dlp video download with quality control |
| **Publisher** | Send video + caption to target Channel |
| **Database** | Store config (admin whitelist, channel list, video quality settings, post logs) |
| **Admin Commands** | Bot commands for runtime config management |

## Interaction Flow

```
User:  sends X video link
Bot:   "Parsing link..." → validates link
Bot:   "Please enter the caption (HTML format supported)"
User:  sends caption
Bot:   [multiple channels] → Inline Keyboard to select target Channel
       [single channel] → skip selection
Bot:   "Task accepted. Downloading video..."  ← conversation ENDS here
       (user is free to submit another link immediately)
Bot:   [async, file too large] "Video too large (XXX MB), cannot send"
Bot:   [async, success] "Published to [Channel Name]" + message link
```

### ConversationHandler States and Execution Model

The conversation and the download/publish are **separate phases** with different execution models:

**Phase 1 — Conversation (synchronous, blocking per-user):**

```python
# State constants
WAITING_CAPTION = 0     # Link received, waiting for caption
WAITING_CHANNEL = 1     # Caption received, waiting for channel selection (multi-channel only)

# entry_points: MessageHandler with URL regex filter (admin-only)
# states:
#   WAITING_CAPTION: MessageHandler for text input
#   WAITING_CHANNEL: CallbackQueryHandler for inline keyboard selection
# fallbacks: CommandHandler for /cancel
```

The conversation auto-advances past WAITING_CHANNEL when only one channel is configured. Once the channel is determined, the handler:
1. Validates caption (HTML normalization + length check)
2. Replies "Task accepted. Downloading video..."
3. Returns `ConversationHandler.END` — **the conversation is over**
4. Spawns a background `asyncio.Task` for the download/publish phase

**Phase 2 — Download & Publish (background `asyncio.Task`, concurrent):**

```python
# Spawned from the final conversation handler, AFTER ConversationHandler.END
task = asyncio.create_task(
    download_and_publish(bot, user_id, chat_id, source_url, caption, channel_chat_id)
)
# Fire-and-forget; task reports results back to user via bot.send_message()
```

The background task:
1. Saves the task to `pending_tasks`, then registers it with `DownloadSlotManager.enqueue()`
   - If no slot: tells the user their queue position and waits in `wait_for_slot()`
2. Checks disk space (after the slot is acquired, since the queue wait can be long)
3. Downloads video with yt-dlp
4. Post-processes (faststart, codec check)
5. Publishes to channel via `bot.send_video()`
6. Sends success/failure notification to user via `bot.send_message()`
7. Releases download slot and deletes the `pending_tasks` row in `finally`

**Why this split matters:** `python-telegram-bot` processes updates sequentially through handlers. If download (which can take minutes) ran inside a handler via `await`, it would block all other updates — no other admin could even start a conversation. By ending the conversation first and spawning a background task, the handler returns immediately, unblocking the update queue. Multiple background tasks run concurrently via the event loop, achieving the parallel download goal.

**Consequence:** Since the conversation ends before download starts, `/cancel` cannot cancel an in-progress download. This is acceptable — downloads are typically short (< 1 minute for most X videos), and adding cancellation would require task tracking infrastructure that isn't justified at this scale.

### Private Chat Enforcement

**All handlers** (conversation entry, admin commands, user commands) must check `update.effective_chat.type == ChatType.PRIVATE` as the first guard. This is implemented as a shared decorator/filter applied uniformly to every handler registration — not left to individual handler functions.

If a message arrives from a group or channel, the bot silently ignores it (no response, no error). This prevents admins from accidentally triggering commands or conversations in shared groups.

## Database Design

### admins

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER PK | Auto-increment |
| user_id | BIGINT UNIQUE | Telegram User ID |
| username | TEXT | Username for display |
| created_at | TIMESTAMP | Time added |

### channels

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER PK | Auto-increment |
| chat_id | BIGINT UNIQUE | Channel Chat ID |
| title | TEXT | Channel display name (for button labels) |
| created_at | TIMESTAMP | Time added |

### settings

| Field | Type | Description |
|-------|------|-------------|
| key | TEXT PK | Config key |
| value | TEXT | Config value (JSON serialized) |

Default settings:
- `max_resolution`: `"1080"` — max video resolution
- `max_file_size_mb`: `"2000"` — max file size in MB
- `max_concurrent_downloads`: `"2"` — max parallel downloads

Allowed settings keys and valid ranges:
- `max_resolution`: one of `"360"`, `"480"`, `"720"`, `"1080"`, `"1440"`, `"2160"`
- `max_file_size_mb`: integer between `1` and `2000`
- `max_concurrent_downloads`: integer between `1` and `5` (default: `"2"`)

The `/set` command validates keys against this whitelist and values against their allowed ranges.

### post_logs

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER PK | Auto-increment |
| admin_user_id | BIGINT | Operator |
| source_url | TEXT | Original X link |
| channel_chat_id | BIGINT | Target Channel |
| message_id | INTEGER | Published message ID (null until published) |
| caption | TEXT | Sent caption |
| status | TEXT | `downloading` / `publishing` / `done` / `failed` |
| error_message | TEXT | Error details if status is `failed` (nullable) |
| created_at | TIMESTAMP | Record creation time |
| updated_at | TIMESTAMP | Last status update time |

On startup, rows still in `downloading` / `publishing` are marked `failed` with `Interrupted by restart`.

### pending_tasks

Tasks that were submitted but have not finished. Used to restore the queue after a restart.

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER PK | Auto-increment; also used as the task ID in `DownloadSlotManager` |
| user_id | BIGINT | Submitting admin |
| user_chat_id | BIGINT | Private chat to notify |
| source_url | TEXT | Video link |
| caption | TEXT | Sanitized caption |
| channel_chat_id | BIGINT | Target Channel |
| max_resolution | INTEGER | Resolution chosen in the conversation |
| status | TEXT | `queued` / `running` |
| created_at | TIMESTAMP | Submission time |

## Bot Commands

### User Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage instructions |
| `/help` | Help information |
| `/cancel` | Cancel current operation |

### Admin Commands

| Command | Description |
|---------|-------------|
| `/add_admin <user_id>` | Add admin (super admin only) |
| `/remove_admin <user_id>` | Remove admin (super admin only) |
| `/list_admins` | List all admins |
| `/add_channel <chat_id>` | Add target Channel (validates access, fetches title automatically) |
| `/remove_channel <chat_id>` | Remove Channel |
| `/list_channels` | List all Channels |
| `/set <key> <value>` | Update config |
| `/get <key>` | View config value |
| `/settings` | View all settings |

### Permission Levels

- **Super Admin** (`SUPER_ADMIN_ID` env var): Can manage other admins + all admin functions
- **Admin**: Can post videos, manage Channels and settings, but cannot add/remove admins

### Channel Validation on Add

When `/add_channel <chat_id>` is called, the bot performs upfront validation **before** writing to the database:

1. **`get_chat(chat_id)`** — verifies the chat_id exists and is a channel. Extracts `chat.title` for display. If it fails: "Invalid chat_id or bot has no access to this channel."
2. **`get_chat_member(chat_id, bot.id)`** — verifies the bot is a member with posting permissions (administrator with `can_post_messages`). If it fails: "Bot is not an admin of this channel. Please add the bot as a channel admin first."
3. Only after both checks pass, write to `channels` table with `title` from `get_chat` result.

The command takes only `chat_id` — title is always fetched from Telegram to ensure accuracy. No user-provided title parameter.

This catches mistyped chat_ids and missing permissions at config time, not at first publish.

## Video Download Logic

### Link Validation

Supported URL patterns:
- `twitter.com/*/status/*`
- `x.com/*/status/*`
- `t.co/*` short links — Bot resolves via HTTP HEAD redirect before passing to yt-dlp

Regex match first, then yt-dlp `extract_info` to verify the link contains downloadable video.

### Download Parameters

```python
ydl_opts = {
    'format': (
        # Priority 1: H.264+AAC in MP4, resolution-limited — ideal for inline playback
        f'best[vcodec^=avc][acodec^=mp4a][height<={max_resolution}]'
        # Priority 2: H.264 video + any audio, merge into MP4
        f'/bestvideo[vcodec^=avc][height<={max_resolution}]+bestaudio[acodec^=mp4a]'
        f'/bestvideo[vcodec^=avc][height<={max_resolution}]+bestaudio'
        # Priority 3: any codec, resolution-limited (will need remux/transcode)
        f'/best[height<={max_resolution}]'
        f'/bestvideo[height<={max_resolution}]+bestaudio'
        # Priority 4: absolute fallback
        f'/best'
    ),
    'outtmpl': os.path.join(tmp_dir, '%(id)s.%(ext)s'),
    'merge_output_format': 'mp4',
    'postprocessors': [{
        'key': 'FFmpegVideoConvertor',
        'preferedformat': 'mp4',
    }],
    'postprocessor_args': {
        'ffmpeg': ['-movflags', '+faststart'],  # moov atom at front for streaming
    },
}
```

**Format selection rationale:**
- The format string **prefers H.264 (`avc`) + AAC (`mp4a`)** variants first, matching the inline playback requirements defined in the Publishing section. This avoids downloading VP9/AV1/Opus when the source has an H.264 variant available, preventing unnecessary fallback to `send_document()`.
- The `filesize` filter is intentionally omitted because most X/Twitter CDNs do not return `Content-Length` metadata. File size is checked **after download** (step 4 in Download Flow).
- If only Priority 3/4 matches (non-H.264 source), the post-download processing will attempt remux. If that's not possible, `send_document()` fallback applies.

### Download Flow

1. Call yt-dlp Python API (not subprocess)
2. Build format selection from `max_resolution` and codec preference (H.264+AAC preferred, any codec as fallback); file size is checked post-download
3. Download to temp directory (`tempfile.mkdtemp()`)
4. Check file size after download; notify user if over limit
5. Clean up temp files on both success and failure

## Publishing Logic

### Inline Video Playback Constraints

Telegram renders inline video players based on the **video file itself**, not metadata parameters. The key requirements:

1. **Container**: MP4 (with moov atom at the front for streaming)
2. **Video codec**: H.264
3. **Audio codec**: AAC
4. **`supports_streaming=True`** in `send_video()` — signals the client to attempt streaming playback

`thumbnail`, `duration`, `width`, `height` are cosmetic hints only — they do NOT determine whether Telegram shows an inline player or a generic file icon.

### Post-Download Processing

After yt-dlp download, verify the file meets streaming requirements:
1. Check container is MP4 with H.264+AAC (via `yt-dlp` merge or `ffprobe` metadata inspection)
2. Ensure moov atom is at the front of the file. If not (e.g. after `yt-dlp` merge), run `ffmpeg -movflags +faststart` to relocate it. This is a fast metadata-only operation, not a re-encode.
3. If the video cannot be made streaming-compatible (rare edge case, e.g. non-H.264 source with no ffmpeg available), fall back to `send_document()` and notify the user: "Video sent as file (non-streamable format). Viewers will need to download before playing."

### Send Flow

1. Use `bot.send_video()` with `supports_streaming=True` to send to target Channel
2. Pass `caption` with user text, `parse_mode='HTML'` for rich text
3. Optionally pass `duration`, `width`, `height` from yt-dlp `extract_info` as display hints
4. On success, get `message_id` and log to `post_logs`
5. Reply to user with success message + Channel message link

### Caption Validation and Normalization

Caption processing happens **before** download starts (fail fast):

**Step 1 — HTML normalization:**
- Parse input with a whitelist-based sanitizer (e.g. `bleach` or manual parser)
- Allowed tags: `<b>`, `<i>`, `<u>`, `<s>`, `<a href="...">`, `<code>`, `<pre>`
- Strip all other tags (keep inner text)
- Fix unclosed/mismatched tags
- Escape bare `<`, `>`, `&` characters outside tags (Telegram requires `&lt;` `&gt;` `&amp;`)

**Step 2 — Length validation:**
- Telegram `send_video` caption limit: **1024 characters** (after entity parsing)
- After normalization, check the plain-text length (tags stripped) against 1024
- If over limit, reject immediately: "Caption too long (X/1024 chars). Please shorten and resend."
- User stays in WAITING_CAPTION state to retry

**Step 3 — Preview (optional):**
- After validation passes, show the user a preview of the normalized caption before proceeding

Documented in `/start` help message with supported tag list and character limit.

## Error Handling

| Scenario | Response |
|----------|----------|
| Invalid / non-X link | "Please send a valid X video link" |
| yt-dlp download failure | "Download failed, please check if the link contains a video" |
| File exceeds size limit | "Video too large (XXX MB), exceeds limit (YYY MB)" |
| Channel send failure | "Send failed, please check Bot permissions." (Should be rare — channel validated on add) |
| Caption too long | "Caption too long (X/1024 chars). Please shorten and resend." (stays in WAITING_CAPTION) |
| Insufficient disk space | "Insufficient disk space, please try again later" |
| Concurrency limit reached | "Your task is queued at position N and will start automatically." |
| Bot stopped while task is queued | "Bot is restarting. Your task is saved..." (resumed after restart) |
| Bot stopped while task is downloading | After restart: "Your task was interrupted by a bot restart..." (not retried, see below) |
| Non-streamable video | Falls back to `send_document()` with notice to user |
| Network timeout | "Network timeout, please try again later" |
| Unsupported HTML tags in caption | Strip unsupported tags silently, send with clean HTML |

### Concurrency and Disk Safety

**Per-user conversation:** ConversationHandler enforces one active conversation per user. But since the conversation ends before download starts, a user can start a new conversation while their previous download is still running in the background.

**Global concurrency limit and queue:** A `DownloadSlotManager` (`bot/services/downloader.py`) holds the running tasks and a FIFO queue. The slot is acquired inside the background `asyncio.Task`, not inside the conversation handler.

- `enqueue(max_slots, task_id=...)` takes a free slot (position 0) or appends the task to the queue (position 1, 2, ...). A new task never jumps ahead of queued ones, even if a slot happens to be free.
- `wait_for_slot(task_id)` returns `True` when the task gets a slot, `False` if the manager was closed.
- `release_slot(task_id)` frees the slot and promotes the next queued task. Every `enqueue` re-reads `max_concurrent_downloads`, so raising it releases queued tasks on the next submission; lowering it does not stop running tasks.

**Persistence and restart:** every task is written to `pending_tasks` before it is enqueued, marked `running` when it gets a slot, and deleted when it finishes.

- On stop, `BotApplication.stop()` calls `DownloadSlotManager.close()` before `Application.stop()`. `Application.stop()` awaits all `create_task` tasks, so without this a restart would wait for the whole queue to download. Queued tasks exit early and keep their `pending_tasks` row; running downloads finish normally.
- On start, `BotApplication.start()` calls `restore_pending_tasks()` after `Application.start()` (only then are `create_task` tasks awaited on stop). `queued` rows are re-enqueued in their original order. `running` rows are not retried, because the bot cannot tell whether the video already reached the channel; the user is asked to resend the link instead.

**Pre-download disk check:** After acquiring a slot, check available disk space on the download volume (`shutil.disk_usage`). Required free space = `max_concurrent_downloads * max_file_size_mb * 2` MB. The formula reserves space for all possible concurrent tasks at full capacity. The `*2` factor accounts for yt-dlp temp files + faststart remux producing a second copy during processing. If below threshold, reject with: "Insufficient disk space, please try again later" and log a warning.

**Cleanup:** Temp files are cleaned in a `finally` block per download task. On bot startup, any stale files in the download directory older than 1 hour are purged.

## Project Structure

```
tg_auto_forward_bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entry point, init Bot and start
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── start.py          # /start, /help, /cancel
│   │   ├── conversation.py   # Core two-step conversation flow
│   │   └── admin.py          # Admin command handlers
│   ├── services/
│   │   ├── __init__.py
│   │   ├── downloader.py     # yt-dlp download logic
│   │   └── publisher.py      # Send video to Channel
│   ├── database/
│   │   ├── __init__.py
│   │   ├── connection.py     # Database connection management
│   │   ├── models.py         # Table definitions and init
│   │   └── crud.py           # CRUD operations
│   ├── utils/
│   │   ├── __init__.py
│   │   └── validators.py     # URL validation utilities
│   └── config.py             # Environment variable loading
├── .env.example              # Environment variable template
├── requirements.txt
├── Dockerfile
├── docker-compose.yml        # Bot + Local Bot API Server
├── .gitignore
└── README.md
```

## Deployment

### Prerequisite: Switch Bot to Local API Server

Before the bot can receive updates from a Local Bot API Server, you **must** call `logOut` on the official Telegram API. This is a one-time operation per bot token migration:

```bash
# Call logOut via the OFFICIAL Telegram API (not your local server)
curl https://api.telegram.org/bot<BOT_TOKEN>/logOut
```

**Rules:**
- After `logOut`, the bot **cannot** use the official API for 10 minutes
- Only then start the bot against the Local API Server
- If migrating between two Local API Servers, call `close` on the old server first
- The bot startup script (`main.py`) should document this requirement and verify connectivity to the Local API Server on startup

The bot's `main.py` will attempt a `getMe` call on startup. If it fails, it logs a clear error message mentioning the `logOut` prerequisite.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram Bot Token |
| `API_BASE_URL` | Local Bot API Server URL (e.g., `http://localhost:8081`) |
| `SUPER_ADMIN_ID` | Super admin Telegram User ID |
| `API_ID` | Telegram API ID (for Local Bot API Server) |
| `API_HASH` | Telegram API Hash (for Local Bot API Server) |

### Docker Compose

```yaml
services:
  telegram-bot-api:
    image: aiogram/telegram-bot-api
    environment:
      TELEGRAM_API_ID: ${API_ID}
      TELEGRAM_API_HASH: ${API_HASH}
      TELEGRAM_LOCAL: "true"          # Enable local mode for 2000MB upload limit
    ports:
      - "8081:8081"
    volumes:
      - shared-data:/var/lib/telegram-bot-api

  bot:
    build: .
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      API_BASE_URL: http://telegram-bot-api:8081
      SUPER_ADMIN_ID: ${SUPER_ADMIN_ID}
    volumes:
      - bot-data:/app/data
      - shared-data:/var/lib/telegram-bot-api  # Shared volume for local file path uploads
    depends_on:
      - telegram-bot-api

volumes:
  shared-data:    # Shared between bot and API server for local file path passing
  bot-data:       # SQLite database persistence
```

### Local Mode Configuration (Critical)

Using local file paths requires **both** server-side and client-side local mode:

**Server side:** `TELEGRAM_LOCAL: "true"` in the `telegram-bot-api` container (already configured above).

**Client side (`config.py` / `main.py`):** The `python-telegram-bot` `Application` must be built with `local_mode=True`. Without this, PTB reads the file into bytes and uploads via HTTP multipart — defeating the shared volume design entirely.

```python
from telegram.ext import ApplicationBuilder

app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .base_url(f"{API_BASE_URL}/bot")
    .base_file_url(f"{API_BASE_URL}/file/bot")
    .local_mode(True)   # REQUIRED: tells PTB to pass file paths directly
    .build()
)
```

When `local_mode=True` is set and a `pathlib.Path` or string path is passed to `send_video()`, PTB sends the **file URI** (`file:///path/to/video.mp4`) to the Local API Server, which reads the file directly from the shared filesystem. This is what makes the shared volume (`shared-data`) work.

**Download path:** The bot must download videos to a path inside the shared volume — specifically `/var/lib/telegram-bot-api/downloads/` — so the API Server can access them. In direct-run mode (no Docker), both the bot and the API Server must share the same filesystem path.

### Direct Run

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your configuration
# Ensure Local Bot API Server is running separately
python -m bot.main
```

### Dependencies

```
python-telegram-bot[ext]>=20.0
yt-dlp
aiosqlite
python-dotenv
```

**System dependency:** `ffmpeg` must be installed (used by yt-dlp for merging and by the bot for `-movflags +faststart`). The Dockerfile should install it via `apt-get install ffmpeg`.
