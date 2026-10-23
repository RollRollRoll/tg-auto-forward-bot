import pytest
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
