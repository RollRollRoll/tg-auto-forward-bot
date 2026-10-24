# Telegram Video Forward Bot

English | [简体中文](README.zh-CN.md)

A Telegram bot that downloads videos from YouTube, X/Twitter, TikTok, Instagram and 1000+ other sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp), then publishes them to your Telegram channels with a custom caption.

Authorized admins send a link to the bot in a private chat, write a caption, pick a resolution and a target channel. The bot downloads the video in the background and posts it. It runs against a [Local Bot API Server](https://github.com/tdlib/telegram-bot-api), so uploads can be up to 2000 MB instead of the 50 MB limit of the official cloud API.

## Features

- **Multi-platform downloads**: anything yt-dlp supports, with H.264/AAC MP4 preferred for in-app streaming
- **Per-link resolution picker**: available resolutions are detected from each link, capped by the `max_resolution` setting. Resolution means the short side, so a 1080×1920 portrait video counts as 1080p
- **HTML captions**: `<b>` `<i>` `<u>` `<s>` `<a href>` `<code>` `<pre>`, sanitized, up to 1024 visible characters; `/skip` to post without a caption
- **Multiple channels**: choose the target channel from inline buttons
- **Task tracking**: `/tasks` shows active downloads with progress bars and elapsed time, the queue, and the 5 most recent tasks
- **Inline dashboard**: `/start` opens a button menu (Tasks, Channels, Settings, Admins, Help)
- **Two-level permissions**: a super admin set by env var, plus admins managed from the chat
- **Concurrency and disk guards**: configurable download slots and file size limit; when all slots are busy, new tasks wait in a first-in-first-out queue; disk space is checked before each download starts
- **Graceful fallback**: if Telegram rejects the file as a video, it is sent as a document instead
- **Cookies support**: use a `cookies.txt` for sites that need a login (e.g. X/Twitter)
- **Self-maintaining**: yt-dlp is upgraded on every container start; stale downloads older than 1 hour are removed on startup
- **SQLite storage**: admins, channels, settings, post logs and the download queue in a single file; queued tasks survive a restart

## How It Works

```text
Admin (private chat)
  │  1. send link  2. caption or /skip  3. pick resolution  4. pick channel
  ▼
Bot (python-telegram-bot, long polling)
  │  background task: acquire slot (or wait in queue) → disk check → yt-dlp + ffmpeg → send_video
  ▼
Local Bot API Server ──► Telegram channel
```

The bot writes downloads to a directory shared with the Local Bot API Server, which reads the file from disk directly. Both processes must see the file at **the same absolute path**. In Docker Compose this is the `shared-data` volume mounted at `/var/lib/telegram-bot-api`.

## Requirements

- A bot token from [@BotFather](https://t.me/BotFather)
- `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) (required by the Local Bot API Server)
- Your numeric Telegram user ID, e.g. from [@userinfobot](https://t.me/userinfobot)
- Docker with the Compose plugin, **or** Python 3.11+, ffmpeg and a separately running Local Bot API Server
- The Local Bot API Server must support Bot API 10.3 or later. The private chat cards use Telegram Rich Messages; Docker Compose pins `aiogram/telegram-bot-api:10.3`. On an older server the bot falls back to plain HTML messages.
- Enough free disk space. Before each task the bot requires `max_concurrent_downloads × max_file_size_mb × 2` MB free in the download directory. With the defaults that is **8000 MB**; lower the settings on small disks (see [Settings](#settings)).

## Quick Start (Docker)

1. Log the bot out of the official cloud API. This is required once before using a Local Bot API Server; wait about 10 minutes afterwards.
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/logOut
   ```
2. Configure:
   ```bash
   cp .env.example .env
   # fill in BOT_TOKEN, SUPER_ADMIN_ID, API_ID, API_HASH
   ```
3. Start:
   ```bash
   docker compose up -d
   docker compose logs -f bot   # wait for "Bot started: @your_bot"
   ```
4. Add the bot to your channel as an **admin with "Post Messages" permission**.
5. In a private chat with the bot, run `/add_channel <chat_id>` (numeric ID, channels usually start with `-100`), then send a video link.

## Quick Start (Direct)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m bot.main
```

Notes for running without Docker:

- ffmpeg must be installed and on `PATH`.
- A Local Bot API Server must be running in `--local` mode and reachable at `API_BASE_URL`.
- The bot reads the downloaded file from `DOWNLOAD_DIR` and passes its path to the API server, so **the server must be able to read that same path**. Running both on one host with the default `/var/lib/telegram-bot-api/downloads` works; create the directory and make it writable first.
- The SQLite database is created at `data/bot.db` relative to the current working directory.

## Configuration

Environment variables are read from the process environment or a `.env` file (`bot/config.py`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | Yes | – | Bot token from @BotFather |
| `SUPER_ADMIN_ID` | Yes | – | Telegram user ID of the super admin |
| `API_ID` / `API_HASH` | Yes (Docker) | – | Used by the `telegram-bot-api` container |
| `API_BASE_URL` | No | `http://localhost:8081` | Local Bot API Server URL. Docker Compose sets it to `http://telegram-bot-api:8081` |
| `DOWNLOAD_DIR` | No | `/var/lib/telegram-bot-api/downloads` | Temp download directory, must be readable by the API server at the same path |
| `COOKIES_FILE` | No | `/app/data/cookies.txt` | cookies.txt for yt-dlp; used only if the file exists |

> In Docker Compose, the `bot` service only receives the variables listed in `docker-compose.yml`. To override `COOKIES_FILE` or `DOWNLOAD_DIR`, add them under `bot.environment` there.

## Usage

All interaction happens in a **private chat** with the bot. Messages in groups and channels are ignored, and so are messages from users who are not admins.

1. Send a message containing a video link.
2. Reply with a caption (HTML allowed), or tap **Skip caption** (or send `/skip`) to post without one.
3. Pick a resolution. Only resolutions up to `max_resolution` are offered; if just one remains, this step is skipped.
4. Pick the target channel. If only one channel is configured, it is used automatically.
5. Check the summary card and tap **Confirm**. **Edit** lets you change the caption, resolution or channel first; **Cancel** drops the task.
6. The bot posts one status message for the task and keeps it up to date: queue position, download progress (speed and ETA), converting, uploading. When the task ends the message shows the result, with an **Open post** button on success, and the bot sends a short message so your phone gets a notification. `/tasks` still shows all active and recent tasks.

Tap **Cancel** or send `/cancel` at any step to abort. Sending a new link while choosing a resolution or channel, or on the summary card, starts over with the new link. Buttons on old messages (after the task was submitted or cancelled, or after a bot restart) only show "This action has expired".

## Bot Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Admins get the button menu; others get the help text |
| `/help` | Everyone | Show help text |
| `/tasks` | Admin | Active downloads, queued tasks and the 5 most recent tasks |
| `/add_channel <chat_id>` | Admin | Add a target channel (the bot must already be an admin there with posting rights) |
| `/remove_channel <chat_id>` | Admin | Remove a channel |
| `/list_channels` | Admin | List channels |
| `/settings` | Admin | Show all settings |
| `/get <key>` | Admin | Show one setting |
| `/set <key> <value>` | Admin | Update a setting |
| `/add_admin <user_id>` | Super admin | Add an admin |
| `/remove_admin <user_id>` | Super admin | Remove an admin |
| `/list_admins` | Super admin | List admins |
| `/skip` | Admin | Skip the caption (during the link flow) |
| `/cancel` | Admin | Cancel the current link flow |

Most commands are also registered in the Telegram **Menu** button on startup.

## Settings

Settings are stored in SQLite and changed with `/set <key> <value>`.

| Key | Default | Allowed values | Description |
|---|---|---|---|
| `max_concurrent_downloads` | `2` | 1–5 | Number of simultaneous downloads. When all slots are busy, new tasks are queued. A higher value takes effect when the next task is submitted; lowering it does not stop tasks that are already running |
| `max_file_size_mb` | `2000` | 1–2000 | Downloads larger than this are discarded instead of posted |
| `max_resolution` | `1080` | 360, 480, 720, 1080, 1440, 2160 | Upper limit for the resolution picker, measured on the short side (height for landscape, width for portrait). Higher resolutions are hidden; if a video has none at or below the limit, its lowest resolution is used |

## Cookies (for sites that need a login)

Some sites, such as X/Twitter, may fail with `Bad guest token` or HTTP 403 without a logged-in session. Export your browser cookies to fix this.

1. Install a browser extension:
   - Chrome: [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. Log in to the site (e.g. [x.com](https://x.com)) and export its cookies as `cookies.txt`.
3. Copy it into the bot's data volume and restart (no rebuild needed):
   ```bash
   docker compose cp cookies.txt bot:/app/data/cookies.txt
   docker compose restart bot
   ```

Cookies expire. If downloads start failing again, export and upload a fresh file. `cookies.txt` is in `.gitignore`; never commit it.

## yt-dlp Auto-Upgrade

`entrypoint.sh` runs `pip install -U yt-dlp` every time the container starts, so site extractors stay current. Check the result with:

```bash
docker compose logs bot | head -5
```

The upgrade needs network access to PyPI. If it fails, the bot still starts with the version already installed in the image.

All dependencies, including transitive ones, are locked in `requirements.txt` (see [Dependencies](#dependencies)). The locked yt-dlp version is only the baseline baked into the image; at runtime the container uses the latest release. To keep yt-dlp fixed as well, remove the `pip install` line from `entrypoint.sh`.

## Deploying to a Remote Server

### Option A: build locally, ship the image

```bash
# On your machine (linux/amd64 fits most cloud servers)
docker build --platform linux/amd64 -t tg-auto-forward-bot:latest .
docker save tg-auto-forward-bot:latest -o tg-auto-forward-bot.tar

scp tg-auto-forward-bot.tar docker-compose.yml user@your-server-ip:~/tg-bot/
scp .env.example user@your-server-ip:~/tg-bot/.env
```

On the server:

```bash
# Install Docker if needed (Ubuntu/Debian)
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

cd ~/tg-bot
docker load -i tg-auto-forward-bot.tar
nano .env   # fill in real credentials
```

In `docker-compose.yml`, replace `build: .` under the `bot` service with the image:

```yaml
bot:
  image: tg-auto-forward-bot:latest   # instead of: build: .
```

Then start it with `docker compose up -d`.

### Option B: build on the server

```bash
rsync -avz --exclude='venv' --exclude='.git' --exclude='__pycache__' \
  ./ user@your-server-ip:~/tg-bot/

# On the server
cd ~/tg-bot
cp .env.example .env && nano .env
docker compose up -d --build
```

### Operations

```bash
docker compose ps              # status
docker compose logs -f bot     # live logs
docker compose restart         # restart
docker compose down            # stop (volumes are kept)

# Update to a new image
docker load -i tg-auto-forward-bot-new.tar
docker compose up -d
```

Data lives in two named volumes: `bot-data` (SQLite database and `cookies.txt`) and `shared-data` (Local Bot API Server state and temporary downloads). Back up `bot-data` before removing volumes.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Bot does not respond at all | You are not an admin, or you wrote in a group. Use a private chat with the account set as `SUPER_ADMIN_ID` |
| `Insufficient disk space` | Free space is below `max_concurrent_downloads × max_file_size_mb × 2` MB. Free up space or lower these settings |
| `Your task is queued at position n` | All slots are taken. No action needed, the task starts automatically. To run more tasks at once, raise `max_concurrent_downloads` |
| `Bot is restarting. Your task is saved...` | The bot was stopped while the task was still queued. No action needed: queued tasks are stored in SQLite and resume in the same order after the restart |
| `Your task was interrupted by a bot restart...` | The bot was stopped while the task was downloading or uploading. It is not retried automatically, because the bot cannot tell whether the video already reached the channel. Resend the link from the message |
| `No video found at this URL` | The site is unsupported, the post has no video, or a login is needed. Try [cookies](#cookies-for-sites-that-need-a-login) |
| `Bad guest token` / 403 | Configure or refresh `cookies.txt` |
| `Bot is not an admin of this channel...` | Add the bot as a channel admin with "Post Messages" permission, then retry `/add_channel` |
| Video arrives as a file | Telegram rejected it as a video; the bot fell back to `send_document`. The caption is sent as plain text in this case |
| Startup errors right after switching to the local server | You skipped `logOut`, or did not wait ~10 minutes after it |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

### Dependencies

Direct dependencies are declared in `requirements.in` (runtime) and `requirements-dev.in` (tests). `requirements.txt` and `requirements-dev.txt` are lock files generated by [uv](https://github.com/astral-sh/uv) and pin every package, including transitive ones. Do not edit the lock files by hand. After changing a `.in` file, regenerate both:

```bash
uv pip compile requirements.in -o requirements.txt --universal --python-version 3.11
uv pip compile requirements-dev.in -o requirements-dev.txt --universal --python-version 3.11
```

The dev lock is constrained by `requirements.txt`, so shared packages always have the same version in tests and in production.

Project layout:

```text
bot/
├── main.py          # entry point: builds the Application, registers handlers
├── config.py        # environment variables
├── handlers/        # Telegram handlers: link conversation, admin commands, menu, permission decorators
├── services/        # downloader (yt-dlp, slots, disk), publisher (send_video), pipeline
├── database/        # aiosqlite connection, tables, CRUD
└── utils/           # URL extraction, caption HTML sanitizer
tests/               # pytest + pytest-asyncio
```

## License

[GPL-3.0](LICENSE)
