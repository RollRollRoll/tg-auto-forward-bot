# Telegram 视频转发 Bot

[English](README.md) | 简体中文

一个 Telegram Bot：从 YouTube、X/Twitter、TikTok、Instagram 以及 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 支持的 1000 多个站点下载视频，配上自定义文案发布到你的 Telegram 频道。

授权管理员在私聊里把链接发给 Bot，填写文案，选择分辨率和目标频道，Bot 在后台下载并发布。Bot 连接的是 [Local Bot API Server](https://github.com/tdlib/telegram-bot-api)，上传上限是 2000 MB，而官方云端 API 只有 50 MB。

## 功能

- **多平台下载**：yt-dlp 支持的站点都能用，优先选 H.264/AAC 的 MP4，方便在 Telegram 内直接播放
- **按链接选分辨率**：自动探测每个链接实际可用的分辨率
- **HTML 文案**：支持 `<b>` `<i>` `<u>` `<s>` `<a href>` `<code>` `<pre>`，自动清洗，可见字符最多 1024 个；发送 `/skip` 可不带文案
- **多频道**：通过内联按钮选择目标频道
- **任务追踪**：`/tasks` 显示进行中的下载（进度条、已用时间）和最近 5 条任务
- **按钮菜单**：`/start` 打开按钮面板（任务、频道、设置、管理员、帮助）
- **两级权限**：超级管理员由环境变量指定，普通管理员在聊天中增删
- **并发与磁盘保护**：可配置下载槽位数和文件大小上限，每个任务开始前检查磁盘空间
- **失败降级**：Telegram 不接受视频格式时，自动改为以文件形式发送
- **Cookies 支持**：需要登录的站点（如 X/Twitter）可配置 `cookies.txt`
- **自动维护**：容器每次启动时升级 yt-dlp；启动时清理超过 1 小时的残留下载文件
- **SQLite 存储**：管理员、频道、设置和发布记录都放在一个文件里

## 工作原理

```text
管理员（私聊）
  │  1. 发链接  2. 写文案或 /skip  3. 选分辨率  4. 选频道
  ▼
Bot（python-telegram-bot，long polling）
  │  后台任务：检查磁盘 → 占用槽位 → yt-dlp + ffmpeg 下载 → send_video
  ▼
Local Bot API Server ──► Telegram 频道
```

Bot 把视频下载到和 Local Bot API Server 共享的目录，API Server 直接从磁盘读取文件。两个进程必须能在**同一个绝对路径**下看到这个文件。Docker Compose 部署时，这个目录是挂载在 `/var/lib/telegram-bot-api` 的 `shared-data` 卷。

## 准备工作

- 从 [@BotFather](https://t.me/BotFather) 获取 Bot Token
- 从 [my.telegram.org](https://my.telegram.org) 获取 `api_id` 和 `api_hash`（Local Bot API Server 需要）
- 你的 Telegram 数字用户 ID，可通过 [@userinfobot](https://t.me/userinfobot) 查询
- 安装了 Compose 插件的 Docker；**或者** Python 3.11+、ffmpeg，外加一个单独运行的 Local Bot API Server
- 足够的磁盘空间。每个任务开始前，Bot 要求下载目录剩余空间不少于 `max_concurrent_downloads × max_file_size_mb × 2` MB。默认配置下是 **8000 MB**，磁盘小的话请调低这两项（见[设置项](#设置项)）

## 快速开始（Docker）

1. 先让 Bot 从官方云端 API 登出。第一次使用 Local Bot API Server 前必须做这一步，做完后等大约 10 分钟。
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/logOut
   ```
2. 配置：
   ```bash
   cp .env.example .env
   # 填写 BOT_TOKEN、SUPER_ADMIN_ID、API_ID、API_HASH
   ```
3. 启动：
   ```bash
   docker compose up -d
   docker compose logs -f bot   # 看到 "Bot started: @your_bot" 即启动成功
   ```
4. 把 Bot 加进你的频道，设为**管理员并开启"发布消息"权限**。
5. 在与 Bot 的私聊中执行 `/add_channel <chat_id>`（数字 ID，频道一般以 `-100` 开头），然后发送一个视频链接。

## 快速开始（直接运行）

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m bot.main
```

不用 Docker 运行时要注意：

- 需要安装 ffmpeg，并且在 `PATH` 中。
- 需要单独运行一个 `--local` 模式的 Local Bot API Server，地址与 `API_BASE_URL` 一致。
- Bot 从 `DOWNLOAD_DIR` 读取下载好的文件，把路径交给 API Server，所以 **API Server 必须能读到同一个路径**。两者跑在同一台机器上、使用默认的 `/var/lib/telegram-bot-api/downloads` 即可，记得先创建这个目录并确保可写。
- SQLite 数据库会创建在当前工作目录下的 `data/bot.db`。

## 配置

环境变量从进程环境或 `.env` 文件读取（见 `bot/config.py`）。

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `BOT_TOKEN` | 是 | – | 从 @BotFather 获取的 Bot Token |
| `SUPER_ADMIN_ID` | 是 | – | 超级管理员的 Telegram 用户 ID |
| `API_ID` / `API_HASH` | 是（Docker） | – | 供 `telegram-bot-api` 容器使用 |
| `API_BASE_URL` | 否 | `http://localhost:8081` | Local Bot API Server 地址。Docker Compose 中固定为 `http://telegram-bot-api:8081` |
| `DOWNLOAD_DIR` | 否 | `/var/lib/telegram-bot-api/downloads` | 临时下载目录，API Server 必须能在同一路径读到 |
| `COOKIES_FILE` | 否 | `/app/data/cookies.txt` | yt-dlp 使用的 cookies.txt，文件存在时才会启用 |

> Docker Compose 中，`bot` 服务只会收到 `docker-compose.yml` 里列出的变量。要修改 `COOKIES_FILE` 或 `DOWNLOAD_DIR`，请把它们加到 `bot.environment` 下。

## 使用方法

所有操作都在与 Bot 的**私聊**中进行。群组和频道里的消息、非管理员发来的消息都会被忽略。

1. 发送一条包含视频链接的消息。
2. 回复文案（支持 HTML），或发送 `/skip` 不带文案。
3. 选择分辨率。如果链接只有一种分辨率，这一步会自动跳过。
4. 选择目标频道。如果只配置了一个频道，会自动使用它。
5. Bot 回复 "Task accepted" 后在后台下载，完成后告诉你成功或失败。随时可以用 `/tasks` 查看进度。

任意步骤发送 `/cancel` 可以取消。

## Bot 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/start` | 所有人 | 管理员打开按钮菜单，其他人显示帮助 |
| `/help` | 所有人 | 显示帮助 |
| `/tasks` | 管理员 | 进行中的下载和最近 5 条任务 |
| `/add_channel <chat_id>` | 管理员 | 添加目标频道（Bot 需已是该频道有发布权限的管理员） |
| `/remove_channel <chat_id>` | 管理员 | 移除频道 |
| `/list_channels` | 管理员 | 列出频道 |
| `/settings` | 管理员 | 查看所有设置 |
| `/get <key>` | 管理员 | 查看单个设置 |
| `/set <key> <value>` | 管理员 | 修改设置 |
| `/add_admin <user_id>` | 超级管理员 | 添加管理员 |
| `/remove_admin <user_id>` | 超级管理员 | 移除管理员 |
| `/list_admins` | 超级管理员 | 列出管理员 |
| `/skip` | 管理员 | 跳过文案（发链接流程中） |
| `/cancel` | 管理员 | 取消当前的发链接流程 |

启动时大部分命令会注册到 Telegram 的 **Menu** 按钮里。

## 设置项

设置保存在 SQLite 中，用 `/set <key> <value>` 修改。

| 键 | 默认值 | 允许值 | 说明 |
|---|---|---|---|
| `max_concurrent_downloads` | `2` | 1–5 | 同时下载的任务数。槽位全满时新任务会被拒绝，需要重新发送链接 |
| `max_file_size_mb` | `2000` | 1–2000 | 下载结果超过这个大小会被丢弃，不发布 |
| `max_resolution` | `1080` | 360、480、720、1080、1440、2160 | 可以存储和修改，但**当前下载流程不会读取它**，分辨率以每个链接里选的为准 |

## Cookies 配置（需要登录的站点）

部分站点（如 X/Twitter）在未登录时可能报 `Bad guest token` 或 HTTP 403，导出浏览器 cookies 即可解决。

1. 安装浏览器扩展：
   - Chrome：[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox：[cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. 在浏览器中登录该站点（如 [x.com](https://x.com)），把 cookies 导出为 `cookies.txt`。
3. 复制到 Bot 的数据卷并重启（不需要重新构建）：
   ```bash
   docker compose cp cookies.txt bot:/app/data/cookies.txt
   docker compose restart bot
   ```

Cookies 会过期，再次下载失败时需要重新导出上传。`cookies.txt` 已加入 `.gitignore`，不要提交到仓库。

## yt-dlp 自动升级

容器每次启动时，`entrypoint.sh` 都会执行 `pip install -U yt-dlp`，让站点解析器保持最新。可以这样确认：

```bash
docker compose logs bot | head -5
```

升级需要能访问 PyPI。升级失败时，Bot 仍会用镜像里已有的版本启动。

## 远程服务器部署

### 方案 A：本地构建，上传镜像

```bash
# 在本地执行（linux/amd64 适配大多数云服务器）
docker build --platform linux/amd64 -t tg-auto-forward-bot:latest .
docker save tg-auto-forward-bot:latest -o tg-auto-forward-bot.tar

scp tg-auto-forward-bot.tar docker-compose.yml user@your-server-ip:~/tg-bot/
scp .env.example user@your-server-ip:~/tg-bot/.env
```

在服务器上：

```bash
# 如未安装 Docker（Ubuntu/Debian）
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

cd ~/tg-bot
docker load -i tg-auto-forward-bot.tar
nano .env   # 填入真实凭据
```

把 `docker-compose.yml` 中 `bot` 服务的 `build: .` 改成使用镜像：

```yaml
bot:
  image: tg-auto-forward-bot:latest   # 原来是 build: .
```

然后执行 `docker compose up -d` 启动。

### 方案 B：在服务器上构建

```bash
rsync -avz --exclude='venv' --exclude='.git' --exclude='__pycache__' \
  ./ user@your-server-ip:~/tg-bot/

# 在服务器上
cd ~/tg-bot
cp .env.example .env && nano .env
docker compose up -d --build
```

### 日常运维

```bash
docker compose ps              # 查看状态
docker compose logs -f bot     # 实时日志
docker compose restart         # 重启
docker compose down            # 停止（数据卷保留）

# 更新到新镜像
docker load -i tg-auto-forward-bot-new.tar
docker compose up -d
```

数据保存在两个命名卷里：`bot-data`（SQLite 数据库和 `cookies.txt`）和 `shared-data`（Local Bot API Server 的状态和临时下载文件）。删除数据卷前请先备份 `bot-data`。

## 常见问题

| 现象 | 原因与解决 |
|---|---|
| Bot 完全没有回应 | 你不是管理员，或者是在群里发的。请用 `SUPER_ADMIN_ID` 对应的账号私聊 Bot |
| `Insufficient disk space` | 剩余空间不足 `max_concurrent_downloads × max_file_size_mb × 2` MB。清理磁盘或调低这两项 |
| `Server busy (n/n download slots in use)` | 槽位已满。等任务完成后重新发送链接，或调大 `max_concurrent_downloads` |
| `No video found at this URL` | 站点不支持、帖子里没有视频，或需要登录。可以试试配置 [Cookies](#cookies-配置需要登录的站点) |
| `Bad guest token` / 403 | 配置或更新 `cookies.txt` |
| `Bot is not an admin of this channel...` | 把 Bot 设为频道管理员并开启"发布消息"权限，再执行 `/add_channel` |
| 视频以文件形式发出 | Telegram 不接受这个视频格式，Bot 改用了 `send_document`。这种情况下文案会以纯文本发送 |
| 切换到本地服务器后启动报错 | 没有执行 `logOut`，或执行后没等够 10 分钟 |

## 开发

```bash
pip install -r requirements-dev.txt
pytest
```

目录结构：

```text
bot/
├── main.py          # 入口：构建 Application，注册 handler
├── config.py        # 环境变量
├── handlers/        # Telegram handler：发链接会话、管理命令、菜单、权限装饰器
├── services/        # downloader（yt-dlp、槽位、磁盘）、publisher（send_video）、pipeline
├── database/        # aiosqlite 连接、建表、CRUD
└── utils/           # URL 提取、文案 HTML 清洗
tests/               # pytest + pytest-asyncio
```

## 许可证

[GPL-3.0](LICENSE)
