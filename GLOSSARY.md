# 领域术语

- **Task**：一次"把某个链接下载并发布到某个 Channel"的请求，包含 source_url、caption、max_resolution、channel_chat_id。
- **pending task**：已持久化到 `pending_tasks` 表、尚未完成的 Task。状态为 `queued`（排队中）或 `running`（已拿到 slot 开始执行）。重启后 `queued` 的会恢复执行，`running` 的因无法确认是否已发布而丢弃并通知用户重发。
- **slot**：并发下载名额，数量由 `max_concurrent_downloads` 设置决定。
- **TaskQueue**：负责 Task 排队、持久化与 slot 生命周期的模块，不负责下载/发布，不依赖 Telegram。保证"重启不丢任务、不重复发布"。
- **TaskDraft**：会话中逐步填写、尚未提交的 Task（链接、caption、分辨率、目标 Channel），填完后生成 Task 交给 TaskQueue。
- **ticket**：`TaskQueue.submit()` 返回的凭据，带 task_id 与排队位置（0 表示直接拿到 slot）。
- **post log**：`post_logs` 表中一次发布尝试的记录，状态 pending/publishing/done/failed。
- **Outcome**：一次 Task 执行的最终结果（Published / TooLarge / NoDiskSpace / SendFailed / Failed），post log 的最终状态和用户通知都由它决定。
- **Channel**：已配置的发布目标频道。
- **status message**：每个 Task 在管理员私聊里对应的一条消息，从提交起随 Task 进度原地更新（排队位置、下载进度、转换、上传），直到最终结果。重启恢复的 Task 会另起一条新的 status message。
- **confirmation card**：TaskDraft 填完后、提交成 Task 之前展示给管理员的摘要（链接、caption、分辨率、Channel），管理员确认后才提交，也可以从这里回去修改某一项或取消。
- **Tasks 页**：菜单里的 Tasks 和 `/tasks` 命令显示的任务总览，内容相同：正在占用 slot 的 Task、排队中的 Task，以及最近结束（Done 或 Failed）的几条 post log。所有管理员看到的是同一份。
