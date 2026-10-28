import pytest
import yt_dlp

from bot.services import downloader
from bot.services.downloader import _build_format_spec, _short_side
from bot.services.sources import NoSingleVideo


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


# mp4 直链（如 video.twimg.com）：yt-dlp 只给一个格式，宽高和编码都未知
_DIRECT = {"format_id": "mp4", "url": "http://example.invalid/v.mp4", "ext": "mp4", "protocol": "https"}


def test_format_spec_original_picks_direct_file():
    assert _select([_DIRECT], downloader.ORIGINAL_RESOLUTION) == "mp4"


@pytest.mark.parametrize("formats, expected", [
    ([_DIRECT], [downloader.ORIGINAL_RESOLUTION]),
    ([_AUDIO], []),
    ([], []),
])
async def test_extract_without_sizes(monkeypatch, formats, expected):
    monkeypatch.setattr(downloader, "_do_extract", lambda opts, url: {"id": "v", "formats": formats})
    assert await downloader.extract_available_resolutions("https://video.example/v.mp4") == expected


# ---- 合集识别 ----

_PLAYLIST = {"_type": "playlist", "extractor": "generic", "title": "list",
             "entries": [{"_type": "url", "url": "http://example.invalid/a"},
                         {"_type": "url", "url": "http://example.invalid/b"}]}


@pytest.mark.parametrize("info, expected", [
    (_PLAYLIST, True),
    ({"_type": "multi_video", "entries": []}, True),
    ({"entries": []}, True),
    ({"id": "v", "formats": [_fmt("v720", 1280, 720)]}, False),
])
def test_is_playlist(info, expected):
    assert downloader._is_playlist(info) is expected


async def test_extract_rejects_playlist(monkeypatch):
    monkeypatch.setattr(downloader, "_do_extract", lambda opts, url: dict(_PLAYLIST))
    with pytest.raises(NoSingleVideo, match="playlist"):
        await downloader.extract_available_resolutions("https://example.com/list")


async def test_extract_uses_resolved_url_and_skips_playlists(monkeypatch):
    seen = {}

    async def _resolve(url):
        return "https://video.example/main.m3u8"

    def _extract(opts, url):
        seen.update(opts=opts, url=url)
        return {"id": "v", "formats": [_fmt("v720", 1280, 720), _fmt("v360", 640, 360)]}

    monkeypatch.setattr(downloader, "resolve_source_url", _resolve)
    monkeypatch.setattr(downloader, "_do_extract", _extract)
    assert await downloader.extract_available_resolutions("https://bad.news/t/1") == [360, 720]
    assert seen["url"] == "https://video.example/main.m3u8"
    assert seen["opts"]["noplaylist"] is True


async def test_download_stops_before_downloading_playlist(monkeypatch, tmp_path):
    processed = []
    monkeypatch.setattr(downloader, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", lambda self, url, download=True: dict(_PLAYLIST))
    monkeypatch.setattr(yt_dlp.YoutubeDL, "process_ie_result",
                        lambda self, info, download=True: processed.append(info))
    with pytest.raises(RuntimeError, match="playlist"):
        await downloader.download_video("https://example.com/list")
    assert processed == []
    # 临时目录要清理掉
    assert list(tmp_path.iterdir()) == []
