from __future__ import annotations
import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields

from bot.database.crud import (
    create_pending_task, delete_pending_task, list_pending_tasks, mark_pending_task_running,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Task:
    """一次"把某个链接下载并发布到某个 Channel"的请求。"""
    user_id: int
    user_chat_id: int
    source_url: str
    caption: str
    channel_chat_id: int
    max_resolution: int

    @classmethod
    def from_row(cls, row: dict) -> Task:
        # pending_tasks 行里还有 id、status，只取 Task 自己的字段
        return cls(**{f.name: row[f.name] for f in fields(cls)})


class InMemoryTaskStore:
    """pending task 的内存实现，用于测试。"""

    def __init__(self):
        self._rows: dict[int, dict] = {}
        self._next_id = 0

    async def create(self, task: Task) -> int:
        self._next_id += 1
        self._rows[self._next_id] = {"id": self._next_id, **asdict(task), "status": "queued"}
        return self._next_id

    async def list(self) -> list[dict]:
        return [dict(r) for r in self._rows.values()]

    async def mark_running(self, task_id: int) -> None:
        self._rows[task_id]["status"] = "running"

    async def delete(self, task_id: int) -> None:
        self._rows.pop(task_id, None)


class SqliteTaskStore:
    """pending task 的 SQLite 实现，读写 pending_tasks 表。"""

    def __init__(self, db):
        self._db = db

    async def create(self, task: Task) -> int:
        return await create_pending_task(self._db, **asdict(task))

    async def list(self) -> list[dict]:
        return await list_pending_tasks(self._db)

    async def mark_running(self, task_id: int) -> None:
        await mark_pending_task_running(self._db, task_id)

    async def delete(self, task_id: int) -> None:
        await delete_pending_task(self._db, task_id)


@dataclass(frozen=True)
class Ticket:
    task_id: int
    # 0 表示已拿到 slot，>0 表示排队位置；closed 时无意义
    position: int
    # 提交时队列已关闭：记录已落库，等重启后由 restore() 恢复
    closed: bool = False


class TaskQueue:
    """Task 的排队、持久化与 slot 生命周期，不依赖 Telegram。"""

    def __init__(self, store):
        self._store = store
        self._tasks: dict[int, dict] = {}
        # 排队中的任务，dict 保持插入顺序，即先进先出
        self._queue: dict[int, dict] = {}
        self._waiters: dict[int, asyncio.Future] = {}
        # 排队任务的位置变化回调，以及上次通知的位置
        self._position_listeners: dict[int, Callable[[int], None]] = {}
        self._notified_positions: dict[int, int] = {}
        self._max_slots = 1
        self._closed = False
        self._lock = asyncio.Lock()

    async def submit(self, task: Task, *, max_slots: int) -> Ticket:
        task_id = await self._store.create(task)
        return await self._register(task_id, task, max_slots)

    async def restore(self, *, max_slots: int) -> tuple[list[tuple[Task, Ticket]], list[Task]]:
        """启动后恢复上次退出时留下的 pending task。

        返回 (待恢复的 (Task, ticket) 列表, 被丢弃的中断 Task)。
        status=running 的任务无法确认是否已发到频道，为避免重复发布直接删除，由调用方通知用户重发。
        """
        resumed: list[tuple[Task, Ticket]] = []
        interrupted: list[Task] = []
        for row in await self._store.list():
            task = Task.from_row(row)
            if row["status"] == "running":
                await self._store.delete(row["id"])
                interrupted.append(task)
                continue
            # 按入队顺序依次登记，保证恢复后的排队顺序与重启前一致
            resumed.append((task, await self._register(row["id"], task, max_slots)))
        return resumed, interrupted

    async def _register(self, task_id: int, task: Task, max_slots: int) -> Ticket:
        async with self._lock:
            if self._closed:
                # 已关闭：记录已落库，留到重启后恢复
                return Ticket(task_id=task_id, position=0, closed=True)
            # 每次登记都以最新的并发设置为准；设置调大后可以立刻放行排队任务
            self._max_slots = max_slots
            self._promote()
            self._notify_positions()
            info = {
                "url": task.source_url, "user_id": task.user_id,
                "start_time": time.time(), "progress": 0.0,
                # 已拿到 slot、还没开始下载
                "status": "starting",
            }
            # 队列里还有人时不能插队，即使恰好有空闲槽位
            if not self._queue and len(self._tasks) < max_slots:
                self._tasks[task_id] = info
                return Ticket(task_id=task_id, position=0)
            info["status"] = "queued"
            self._queue[task_id] = info
            self._waiters[task_id] = asyncio.get_running_loop().create_future()
            return Ticket(task_id=task_id, position=len(self._queue))

    @asynccontextmanager
    async def slot(self, ticket: Ticket, *, on_position_changed: Callable[[int], None] | None = None):
        """等待并占用 slot。

        拿到时 yield True，退出后释放 slot 并删除 pending 记录；
        队列已关闭时 yield False，pending 记录保留，重启后由 restore() 恢复。
        on_position_changed(position) 在排队期间位置变化时同步调用；它抛出的异常只记日志，不影响排队。
        """
        task_id = ticket.task_id
        keep_pending = False
        try:
            if ticket.closed:
                keep_pending = True
                yield False
                return
            if on_position_changed is not None and task_id in self._queue:
                self._position_listeners[task_id] = on_position_changed
                # 调用方已经按 ticket.position 显示过；之后位置不同就通知
                self._notified_positions[task_id] = ticket.position
                self._notify_positions()
            waiter = self._waiters.get(task_id)
            got = await waiter if waiter is not None else task_id in self._tasks
            if not got:
                keep_pending = True
                yield False
                return
            await self._store.mark_running(task_id)
            yield True
        finally:
            await self._release(task_id)
            if not keep_pending:
                await self._store.delete(task_id)

    def close(self) -> None:
        """停止接收新任务，并让所有排队任务的 slot() 得到 False。

        Application.stop() 会等待所有后台任务结束，所以必须在它之前调用。
        """
        self._closed = True
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_result(False)
        self._waiters.clear()
        self._queue.clear()
        self._position_listeners.clear()
        self._notified_positions.clear()

    async def _release(self, task_id: int) -> None:
        async with self._lock:
            self._tasks.pop(task_id, None)
            self._queue.pop(task_id, None)
            waiter = self._waiters.pop(task_id, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()
            self._promote()
            self._notify_positions()

    def _notify_positions(self) -> None:
        # 只通知仍在排队、且位置和上次通知不同的任务；已出队的任务清掉回调
        for task_id in [t for t in self._position_listeners if t not in self._queue]:
            self._position_listeners.pop(task_id, None)
            self._notified_positions.pop(task_id, None)
        for position, task_id in enumerate(self._queue, start=1):
            listener = self._position_listeners.get(task_id)
            if listener is None or self._notified_positions.get(task_id) == position:
                continue
            self._notified_positions[task_id] = position
            try:
                listener(position)
            except Exception:
                logger.warning("Position callback failed for task %d", task_id, exc_info=True)

    def _promote(self) -> None:
        # 按入队顺序把排队任务移入空闲槽位
        while self._queue and len(self._tasks) < self._max_slots:
            task_id = next(iter(self._queue))
            info = self._queue.pop(task_id)
            waiter = self._waiters.pop(task_id, None)
            if waiter is None or waiter.done():
                # 等待方已被取消，跳过，不占用槽位
                continue
            info["status"] = "starting"
            info["start_time"] = time.time()
            self._tasks[task_id] = info
            waiter.set_result(True)

    def update_progress(self, task_id: int, progress: float, status: str = "downloading") -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["progress"] = progress
            self._tasks[task_id]["status"] = status

    def get_active_tasks(self) -> list[dict]:
        return self._snapshot(self._tasks)

    def get_queued_tasks(self) -> list[dict]:
        return [{**t, "position": pos} for pos, t in enumerate(self._snapshot(self._queue), start=1)]

    @staticmethod
    def _snapshot(tasks: dict[int, dict]) -> list[dict]:
        now = time.time()
        return [{"task_id": tid, "elapsed": now - t["start_time"], **t} for tid, t in tasks.items()]
