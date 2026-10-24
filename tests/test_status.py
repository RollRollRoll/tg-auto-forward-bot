import asyncio

import pytest
from telegram.error import RetryAfter, TimedOut

from bot.ui.sender import RichSender
from bot.ui.status import ChatEditLimiter, StatusReporter
from tests.fakebot import FakeBot, FakeClock, plain

CHAT = 111
MB = 1024 * 1024


def _setup():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    limiter = ChatEditLimiter(clock=clock, sleep=clock.sleep)
    return clock, bot, limiter


def _reporter(bot, limiter, url="https://e.com/v/1", restored=False):
    return StatusReporter(
        RichSender(bot), limiter, chat_id=CHAT, url=url,
        channel_title="Demo", resolution=720, restored=restored,
    )


def _edits(bot, message_id=None):
    return [(t, kw) for t, m, kw in bot.calls
            if m == "editMessageText" and (message_id is None or kw["message_id"] == message_id)]


@pytest.mark.asyncio
async def test_one_status_message_walks_through_stages_and_ends_with_open_post():
    clock, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    rep.downloading()
    await rep.wait_idle()
    clock.advance(5)
    rep.progress(downloaded=62 * MB, total=100 * MB, speed=4.1 * MB, eta=31)
    await rep.wait_idle()
    content = bot.last(CHAT).text
    assert "Downloading" in content and "62.0%" in content and "ETA 31s" in content
    rep.converting()
    await rep.wait_idle()
    assert "Converting" in bot.last(CHAT).text
    rep.uploading()
    await rep.wait_idle()
    assert "Uploading" in bot.last(CHAT).text
    await rep.finished(success=True, post_url="https://t.me/demo/79")

    status, push = bot.messages(CHAT)
    assert status.format == "rich" and "Done" in status.text
    assert "https://e.com/v/1" in status.text and "Demo" in status.text and "720p" in status.text
    assert [(b["text"], b["url"]) for b in status.buttons] == [("🔗 Open post", "https://t.me/demo/79")]
    assert push.text == "✅ Published to Demo"


@pytest.mark.asyncio
async def test_queued_position_updates_in_place():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(3)
    await rep.wait_idle()
    assert "Queued (#3)" in bot.last(CHAT).text
    rep.position_changed(2)
    await rep.wait_idle()
    assert len(bot.messages(CHAT)) == 1
    assert "Queued (#2)" in bot.last(CHAT).text
    assert bot.last(CHAT).buttons == []


@pytest.mark.asyncio
async def test_progress_without_total_shows_size_and_speed_only():
    clock, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    rep.downloading()
    await rep.wait_idle()
    clock.advance(5)
    rep.progress(downloaded=5 * MB, total=None, speed=1 * MB, eta=None)
    await rep.wait_idle()
    content = bot.last(CHAT).text
    assert "5.0 MB" in content and "1.0 MB/s" in content
    assert "%" not in content and "ETA" not in content


@pytest.mark.asyncio
async def test_failure_shows_reason_without_buttons_and_pushes_it():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    await rep.finished(success=False, detail="Video too large (2100.0 MB), exceeds limit (2000 MB).")
    status, push = bot.messages(CHAT)
    assert "Failed" in status.text and "too large" in status.text and status.buttons == []
    assert push.text.startswith("❌") and "too large" in push.text


@pytest.mark.asyncio
async def test_restored_task_says_so_at_the_top():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter, restored=True)
    rep.start(1)
    await rep.wait_idle()
    assert bot.last(CHAT).text.splitlines()[0].endswith("Bot restarted, resuming")


@pytest.mark.asyncio
async def test_stages_never_go_backwards():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    rep.uploading()
    # yt-dlp 合并音视频时会再报一次 finished，不能把 Uploading 改回 Converting
    rep.converting()
    await rep.wait_idle()
    assert "Uploading" in bot.last(CHAT).text


@pytest.mark.asyncio
async def test_progress_edits_of_one_message_are_at_least_3s_apart():
    clock, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    rep.downloading()
    for i in range(60):
        clock.advance(0.25)
        rep.progress(downloaded=i * MB, total=60 * MB, speed=MB, eta=60 - i)
        await asyncio.sleep(0)
    await rep.wait_idle()
    times = [t for t, _ in _edits(bot)]
    assert len(times) >= 3
    assert all(b - a >= 3 for a, b in zip(times, times[1:]))


@pytest.mark.asyncio
async def test_many_tasks_in_one_chat_stay_within_the_chat_rate_limit():
    clock, bot, limiter = _setup()
    reps = [_reporter(bot, limiter, url=f"https://e.com/v/{i}") for i in range(5)]
    for r in reps:
        r.start(0)
        r.downloading()
    for step in range(80):
        clock.advance(0.25)
        for r in reps:
            r.progress(downloaded=step * MB, total=80 * MB, speed=MB, eta=80 - step)
        if step == 40:
            for r in reps:
                r.converting()
        await asyncio.sleep(0)
    await asyncio.gather(*(r.wait_idle() for r in reps))
    await asyncio.gather(*(r.finished(success=True, post_url="https://t.me/demo/1") for r in reps))

    times = sorted(t for t, m, _ in bot.calls if m in ("editMessageText", "sendRichMessage", "sendMessage"))
    assert all(b - a >= 1.5 for a, b in zip(times, times[1:]))
    statuses = [m for m in bot.messages(CHAT) if m.format == "rich"]
    assert len(statuses) == 5 and all("Done" in m.text for m in statuses)
    # 阶段变化不会被丢弃：每条 status message 都显示过 Converting
    for m in statuses:
        assert any("Converting" in kw["content"] for _, kw in _edits(bot, m.message_id))


@pytest.mark.asyncio
async def test_every_stage_is_shown_even_when_they_arrive_while_waiting_for_the_limit():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(2)
    # 下面几个阶段在限速等待期间一起到达，不能只显示最后一个
    rep.position_changed(1)
    rep.running()
    rep.downloading()
    rep.converting()
    rep.uploading()
    await rep.finished(success=True, post_url="https://t.me/demo/1")
    shown = [plain(kw["content"] if m == "editMessageText" else kw["rich_message"]["markdown"])
             for _, m, kw in bot.calls if m in ("sendRichMessage", "editMessageText")]
    for label in ("Queued (#2)", "Queued (#1)", "Starting", "Downloading", "Converting", "Uploading", "Done"):
        assert any(label in c for c in shown), label


@pytest.mark.asyncio
async def test_rate_limited_edits_never_raise_and_final_state_is_resent_as_html():
    clock, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    await rep.wait_idle()
    bot.fail("editMessageText", RetryAfter(5))
    rep.downloading()
    clock.advance(5)
    rep.progress(downloaded=MB, total=2 * MB, speed=MB, eta=1)
    rep.converting()
    rep.uploading()
    await rep.finished(success=True, post_url="https://t.me/demo/79")

    stale, final, push = bot.messages(CHAT)
    assert "Starting" in stale.text
    assert final.format == "html" and "Done" in final.text
    assert [b["url"] for b in final.buttons] == ["https://t.me/demo/79"]
    assert push.text == "✅ Published to Demo"


@pytest.mark.asyncio
async def test_status_message_that_could_not_be_sent_is_sent_on_the_next_update():
    _, bot, limiter = _setup()
    bot.fail("sendRichMessage", TimedOut(), times=1)
    rep = _reporter(bot, limiter)
    rep.start(0)
    await rep.wait_idle()
    assert bot.messages(CHAT) == []
    rep.downloading()
    await rep.finished(success=False, detail="Download failed: boom")
    status, push = bot.messages(CHAT)
    assert status.format == "rich" and "Failed" in status.text and "boom" in status.text
    assert "boom" in push.text


@pytest.mark.asyncio
async def test_calls_after_finished_are_ignored():
    _, bot, limiter = _setup()
    rep = _reporter(bot, limiter)
    rep.start(0)
    await rep.finished(success=False, detail="x")
    calls = len(bot.calls)
    rep.downloading()
    rep.progress(downloaded=1, total=2, speed=1, eta=1)
    await rep.finished(success=True)
    await rep.wait_idle()
    assert len(bot.calls) == calls
