# 领域术语

- **Task**：一次"把某个链接下载并发布到某个 Channel"的请求，包含 source_url、caption、max_resolution、channel_chat_id。
- **pending task**：已持久化到 `pending_tasks` 表、尚未完成的 Task。状态为 `queued`（排队中）或 `running`（已拿到 slot 开始执行）。重启后 `queued` 的会恢复执行，`running` 的因无法确认是否已发布而丢弃并通知用户重发。
- **slot**：并发下载名额，数量由 `max_concurrent_downloads` 设置决定。
- **TaskQueue**：负责 Task 排队、持久化与 slot 生命周期的模块，不负责下载/发布，不依赖 Telegram。保证"重启不丢任务、不重复发布"。
- **ticket**：`TaskQueue.submit()` 返回的凭据，带 task_id 与排队位置（0 表示直接拿到 slot）。
- **post log**：`post_logs` 表中一次发布尝试的记录，状态 pending/publishing/done/failed。
- **Channel**：已配置的发布目标频道。
