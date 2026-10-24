import asyncio
import pytest

from bot.services.task_queue import InMemoryTaskStore, Task, TaskQueue

# 使用内存 store，行为完全确定，不依赖 SQLite 和轮询


def _task(url="https://a"):
    return Task(user_id=1, user_chat_id=1, source_url=url, caption="c",
                channel_chat_id=-100, max_resolution=720)


@pytest.mark.asyncio
async def test_submit_with_free_slot_takes_it_and_persists_queued():
    store = InMemoryTaskStore()
    queue = TaskQueue(store)
    ticket = await queue.submit(_task(), max_slots=1)
    assert (ticket.position, ticket.closed) == (0, False)
    assert [r["status"] for r in await store.list()] == ["queued"]


@pytest.mark.asyncio
async def test_submit_without_free_slot_queues_in_fifo_order():
    queue = TaskQueue(InMemoryTaskStore())
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)
    assert (first.position, second.position, third.position) == (0, 1, 2)
    assert [t["url"] for t in queue.get_queued_tasks()] == ["https://b", "https://c"]
    assert [t["position"] for t in queue.get_queued_tasks()] == [1, 2]
    assert [t["url"] for t in queue.get_active_tasks()] == ["https://a"]


@pytest.mark.asyncio
async def test_slot_marks_running_then_deletes_and_promotes_next():
    store = InMemoryTaskStore()
    queue = TaskQueue(store)
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)

    async with queue.slot(first) as ok:
        assert ok
        assert [(r["source_url"], r["status"]) for r in await store.list()] == [
            ("https://a", "running"), ("https://b", "queued"),
        ]

    assert [r["source_url"] for r in await store.list()] == ["https://b"]
    async with queue.slot(second) as ok:
        assert ok
        assert [t["url"] for t in queue.get_active_tasks()] == ["https://b"]
    assert await store.list() == []
    assert queue.get_active_tasks() == [] and queue.get_queued_tasks() == []


@pytest.mark.asyncio
async def test_raising_max_slots_promotes_queued_tasks():
    queue = TaskQueue(InMemoryTaskStore())
    await queue.submit(_task("https://a"), max_slots=1)
    await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=3)
    # b 被立刻放行；c 前面已无排队者，也直接拿到 slot
    assert third.position == 0
    assert [t["url"] for t in queue.get_active_tasks()] == ["https://a", "https://b", "https://c"]
    assert queue.get_queued_tasks() == []


@pytest.mark.asyncio
async def test_close_releases_queued_waiters_and_keeps_their_records():
    store = InMemoryTaskStore()
    queue = TaskQueue(store)
    await queue.submit(_task("https://a"), max_slots=1)
    queued = await queue.submit(_task("https://b"), max_slots=1)
    queue.close()
    async with queue.slot(queued) as ok:
        assert not ok
    assert [(r["source_url"], r["status"]) for r in await store.list()] == [
        ("https://a", "queued"), ("https://b", "queued"),
    ]


@pytest.mark.asyncio
async def test_submit_after_close_keeps_record_and_slot_is_refused():
    store = InMemoryTaskStore()
    queue = TaskQueue(store)
    queue.close()
    ticket = await queue.submit(_task(), max_slots=1)
    # 从 ticket 本身就能看出队列已关闭，不必等 slot() 的返回值
    assert ticket.closed
    async with queue.slot(ticket) as ok:
        assert not ok
    assert [r["status"] for r in await store.list()] == ["queued"]
    assert queue.get_active_tasks() == [] and queue.get_queued_tasks() == []


@pytest.mark.asyncio
async def test_restore_requeues_queued_in_order_and_drops_interrupted_running():
    store = InMemoryTaskStore()
    for url in ("https://a", "https://was-running", "https://b", "https://c"):
        await store.create(_task(url))
    await store.mark_running(2)

    queue = TaskQueue(store)
    resumed, interrupted = await queue.restore(max_slots=1)

    assert [(task.source_url, t.position) for task, t in resumed] == [
        ("https://a", 0), ("https://b", 1), ("https://c", 2),
    ]
    assert resumed[0][0] == _task("https://a")
    assert interrupted == [_task("https://was-running")]
    assert [r["source_url"] for r in await store.list()] == ["https://a", "https://b", "https://c"]


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_queue_without_taking_a_slot():
    store = InMemoryTaskStore()
    queue = TaskQueue(store)
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)

    async def wait(ticket):
        async with queue.slot(ticket):
            pass

    waiting = asyncio.create_task(wait(second))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert [t["url"] for t in queue.get_queued_tasks()] == ["https://c"]
    async with queue.slot(first):
        pass
    assert [t["url"] for t in queue.get_active_tasks()] == ["https://c"]
    assert [r["source_url"] for r in await store.list()] == ["https://c"]


@pytest.mark.asyncio
async def test_update_progress_is_visible_in_active_tasks():
    queue = TaskQueue(InMemoryTaskStore())
    ticket = await queue.submit(_task(), max_slots=1)
    queue.update_progress(ticket.task_id, 42.0, "downloading")
    active = queue.get_active_tasks()[0]
    assert (active["progress"], active["status"]) == (42.0, "downloading")


@pytest.mark.asyncio
async def test_sqlite_store_round_trip(db):
    from bot.services.task_queue import SqliteTaskStore
    store = SqliteTaskStore(db)
    a = await store.create(_task("https://a"))
    b = await store.create(_task("https://b"))
    await store.mark_running(a)
    rows = await store.list()
    assert [(r["id"], r["source_url"], r["status"], r["max_resolution"]) for r in rows] == [
        (a, "https://a", "running", 720), (b, "https://b", "queued", 720),
    ]
    await store.delete(a)
    assert [r["id"] for r in await store.list()] == [b]


@pytest.mark.asyncio
async def test_snapshots_keep_the_shape_used_by_the_tasks_view():
    queue = TaskQueue(InMemoryTaskStore())
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)
    active, queued = queue.get_active_tasks()[0], queue.get_queued_tasks()[0]
    assert set(active) == {"task_id", "elapsed", "url", "user_id", "channel_chat_id", "start_time", "progress", "status"}
    assert active["channel_chat_id"] == -100
    assert (active["task_id"], active["status"]) == (first.task_id, "starting")
    assert set(queued) == set(active) | {"position"}
    assert (queued["task_id"], queued["status"], queued["position"]) == (second.task_id, "queued", 1)


@pytest.mark.asyncio
async def test_queued_tasks_are_told_when_their_position_moves_up():
    queue = TaskQueue(InMemoryTaskStore())
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)
    moves = {"b": [], "c": []}

    async def wait(ticket, key):
        async with queue.slot(ticket, on_position_changed=moves[key].append):
            pass

    waiting = [asyncio.create_task(wait(second, "b")), asyncio.create_task(wait(third, "c"))]
    await asyncio.sleep(0)
    async with queue.slot(first):
        pass
    await asyncio.gather(*waiting)
    # a 完成后 b 拿到 slot（不再排队，不通知），c 从第 2 位升到第 1 位
    assert moves == {"b": [], "c": [1]}


@pytest.mark.asyncio
async def test_failing_position_callback_does_not_break_the_queue():
    queue = TaskQueue(InMemoryTaskStore())
    first = await queue.submit(_task("https://a"), max_slots=1)
    second = await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)

    def boom(position):
        raise RuntimeError("edit failed")

    async def wait(ticket):
        async with queue.slot(ticket, on_position_changed=boom) as ok:
            assert ok

    waiting = [asyncio.create_task(wait(second)), asyncio.create_task(wait(third))]
    await asyncio.sleep(0)
    async with queue.slot(first):
        pass
    await asyncio.wait_for(asyncio.gather(*waiting), 1)
    assert queue.get_active_tasks() == [] and queue.get_queued_tasks() == []


@pytest.mark.asyncio
async def test_position_change_before_waiting_is_reported_when_the_listener_attaches():
    queue = TaskQueue(InMemoryTaskStore())
    first = await queue.submit(_task("https://a"), max_slots=1)
    await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)
    assert third.position == 2
    # c 提交后、开始等待前，前面的 b 已经出队（例如期间有网络调用）
    async with queue.slot(first):
        pass
    moves = []

    async def wait():
        async with queue.slot(third, on_position_changed=moves.append):
            pass

    waiting = asyncio.create_task(wait())
    await asyncio.sleep(0)
    assert moves == [1]
    waiting.cancel()


@pytest.mark.asyncio
async def test_raising_max_slots_on_submit_reports_new_positions():
    queue = TaskQueue(InMemoryTaskStore())
    await queue.submit(_task("https://a"), max_slots=1)
    await queue.submit(_task("https://b"), max_slots=1)
    third = await queue.submit(_task("https://c"), max_slots=1)
    moves = []

    async def wait():
        async with queue.slot(third, on_position_changed=moves.append):
            pass

    waiting = asyncio.create_task(wait())
    await asyncio.sleep(0)
    # 调大并发后 b 被放行，c 升到第 1 位
    await queue.submit(_task("https://d"), max_slots=2)
    assert moves == [1]
    waiting.cancel()
