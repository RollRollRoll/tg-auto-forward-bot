import asyncio

import pytest
from bot.services.downloader import DownloadSlotManager


@pytest.mark.asyncio
async def test_enqueue_takes_free_slots_then_queues():
    mgr = DownloadSlotManager()
    t1, p1 = await mgr.enqueue(2)
    t2, p2 = await mgr.enqueue(2)
    t3, p3 = await mgr.enqueue(2)
    assert (p1, p2, p3) == (0, 0, 1)
    assert mgr.active_count == 2
    assert mgr.queued_count == 1
    assert [t["task_id"] for t in mgr.get_queued_tasks()] == [t3]
    assert mgr.get_queued_tasks()[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_release_promotes_queued_task_in_fifo_order():
    mgr = DownloadSlotManager()
    t1, _ = await mgr.enqueue(1)
    t2, p2 = await mgr.enqueue(1)
    t3, p3 = await mgr.enqueue(1)
    assert (p2, p3) == (1, 2)

    await mgr.release_slot(t1)
    assert await asyncio.wait_for(mgr.wait_for_slot(t2), 1) is True
    assert [t["task_id"] for t in mgr.get_active_tasks()] == [t2]
    assert [t["position"] for t in mgr.get_queued_tasks()] == [1]

    await mgr.release_slot(t2)
    assert await asyncio.wait_for(mgr.wait_for_slot(t3), 1) is True
    assert mgr.queued_count == 0


@pytest.mark.asyncio
async def test_new_task_does_not_jump_the_queue():
    mgr = DownloadSlotManager()
    t1, _ = await mgr.enqueue(1)
    t2, _ = await mgr.enqueue(1)
    await mgr.release_slot(t1)
    # t2 已被移入槽位，新任务只能排队
    t3, p3 = await mgr.enqueue(1)
    assert p3 == 1
    assert await mgr.wait_for_slot(t2) is True


@pytest.mark.asyncio
async def test_raising_max_slots_releases_queued_tasks():
    mgr = DownloadSlotManager()
    await mgr.enqueue(1)
    t2, p2 = await mgr.enqueue(1)
    assert p2 == 1
    # 并发设置调大后，下一次登记会先放行排队任务
    t3, p3 = await mgr.enqueue(3)
    assert await asyncio.wait_for(mgr.wait_for_slot(t2), 1) is True
    assert p3 == 0
    assert mgr.active_count == 3


@pytest.mark.asyncio
async def test_releasing_queued_task_does_not_take_a_slot():
    mgr = DownloadSlotManager()
    t1, _ = await mgr.enqueue(1)
    t2, _ = await mgr.enqueue(1)
    t3, _ = await mgr.enqueue(1)
    await mgr.release_slot(t2)  # 排队中途放弃
    assert mgr.queued_count == 1
    await mgr.release_slot(t1)
    assert await asyncio.wait_for(mgr.wait_for_slot(t3), 1) is True
    assert [t["task_id"] for t in mgr.get_active_tasks()] == [t3]


@pytest.mark.asyncio
async def test_close_cancels_queued_and_rejects_new_tasks():
    mgr = DownloadSlotManager()
    t1, _ = await mgr.enqueue(1)
    t2, _ = await mgr.enqueue(1)
    waiting = asyncio.create_task(mgr.wait_for_slot(t2))
    await asyncio.sleep(0)
    mgr.close()
    assert await asyncio.wait_for(waiting, 1) is False
    assert mgr.queued_count == 0
    assert await mgr.enqueue(1) is None
    # 已在运行的任务不受影响，仍可正常释放
    assert mgr.active_count == 1
    await mgr.release_slot(t1)
    assert mgr.active_count == 0


@pytest.mark.asyncio
async def test_release_does_not_go_negative():
    mgr = DownloadSlotManager()
    await mgr.release_slot()
    assert mgr.active_count == 0

import yt_dlp

from bot.services.downloader import _build_format_spec, _short_side


def _fmt(fid, w, h, vcodec="avc1.64001F", acodec="none", ext="mp4"):
    return {"format_id": fid, "url": f"http://example.invalid/{fid}", "ext": ext,
            "width": w, "height": h, "vcodec": vcodec, "acodec": acodec, "protocol": "https"}


_AUDIO = {"format_id": "aud", "url": "http://example.invalid/aud", "ext": "m4a",
          "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https"}


def _select(formats, max_resolution):
    # 离线让 yt-dlp 按格式串选格式，不联网、不下载
    info = {"id": "t", "title": "t", "extractor": "generic", "extractor_key": "Generic",
            "webpage_url": "http://example.invalid", "formats": [dict(f) for f in formats]}
    opts = {"quiet": True, "no_warnings": True, "format": _build_format_spec(max_resolution)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.process_ie_result(info, download=False)["format_id"]


def test_short_side():
    assert _short_side({"width": 1920, "height": 1080}) == 1080
    assert _short_side({"width": 1080, "height": 1920}) == 1080
    assert _short_side({"height": 720}) == 720
    assert _short_side({"width": 1280}) is None
    assert _short_side({"vcodec": "none"}) is None


@pytest.mark.parametrize("limit, expected", [(720, "p720+aud"), (1080, "p1080+aud")])
def test_format_spec_limits_portrait_by_width(limit, expected):
    formats = [_fmt("p540", 540, 960), _fmt("p720", 720, 1280), _fmt("p1080", 1080, 1920), _AUDIO]
    assert _select(formats, limit) == expected


@pytest.mark.parametrize("limit, expected", [(360, "m360"), (720, "v720+aud"), (1080, "v1080+aud")])
def test_format_spec_prefers_higher_resolution_over_low_merged_format(limit, expected):
    # 360p 的音视频合一格式（类似 YouTube 18 号）不能挡住更高分辨率的纯视频格式
    formats = [_fmt("m360", 640, 360, acodec="mp4a.40.2"), _fmt("v720", 1280, 720),
               _fmt("v1080", 1920, 1080), _fmt("vp9", 3840, 2160, vcodec="vp9", ext="webm"), _AUDIO]
    assert _select(formats, limit) == expected


def test_format_spec_portrait_merged_only():
    formats = [_fmt("pm720", 720, 1280, acodec="mp4a.40.2"), _fmt("pm1080", 1080, 1920, acodec="mp4a.40.2")]
    assert _select(formats, 720) == "pm720"
