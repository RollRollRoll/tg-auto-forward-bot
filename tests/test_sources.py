import pytest

from bot.services import sources
from bot.services.sources import NoSingleVideo, bad_news_topic_id, parse_bad_news_video, resolve_source_url

# 话题页结构：主视频和推荐列表里的视频都是 <video class="my-videos">，靠 data-id 区分
_TOPIC_HTML = """
<div class="coverimg">
  <video class="my-videos" width="550" height="330" poster="https://pbs.twimg.com/a.jpg"
         data-id="6532348" controls autoplay preload="auto"
         data-source="https://video.twimg.com/amplify_video/1/pl/main.m3u8?tag=29" data-type="m3u8">
  </video>
</div>
<video class="my-videos" controls preload="none" onplay="clickCounter(3405509)"
       data-id="3405509" data-source="https://video.twimg.com/ext_tw_video/2/vid/other.mp4?tag=12"></video>
"""


@pytest.mark.parametrize("url, expected", [
    ("https://bad.news/ajax/topic/6532348/download", "6532348"),
    ("https://bad.news/t/6532348", "6532348"),
    ("http://www.bad.news/t/6532348?from=share", "6532348"),
    ("https://bad.news/t/6532348/", "6532348"),
    ("https://bad.news/", None),
    ("https://bad.news/t/abc", None),
    ("https://bad.news/t/6532348x", None),
    ("https://notbad.news/t/6532348", None),
    ("https://example.com/t/6532348", None),
])
def test_bad_news_topic_id(url, expected):
    assert bad_news_topic_id(url) == expected


def test_parse_picks_the_topic_video_not_recommendations():
    assert parse_bad_news_video(_TOPIC_HTML, "6532348", "https://bad.news/t/6532348") == \
        "https://video.twimg.com/amplify_video/1/pl/main.m3u8?tag=29"
    assert parse_bad_news_video(_TOPIC_HTML, "3405509", "https://bad.news/t/3405509") == \
        "https://video.twimg.com/ext_tw_video/2/vid/other.mp4?tag=12"


def test_parse_returns_none_without_matching_video():
    assert parse_bad_news_video(_TOPIC_HTML, "1", "https://bad.news/t/1") is None
    assert parse_bad_news_video('<video data-id="1"></video>', "1", "https://bad.news/t/1") is None


def test_parse_resolves_relative_source():
    html = '<video data-id="7" data-source="/media/7.mp4"></video>'
    assert parse_bad_news_video(html, "7", "https://bad.news/t/7") == "https://bad.news/media/7.mp4"


async def test_resolve_leaves_other_urls_alone(monkeypatch):
    async def _fail(url):
        raise AssertionError("should not fetch")
    monkeypatch.setattr(sources, "_fetch_text", _fail)
    assert await resolve_source_url("https://x.com/a/status/1") == "https://x.com/a/status/1"


async def test_resolve_download_link_via_topic_page(monkeypatch):
    fetched = []

    async def _fetch(url):
        fetched.append(url)
        return _TOPIC_HTML
    monkeypatch.setattr(sources, "_fetch_text", _fetch)
    url = await resolve_source_url("https://bad.news/ajax/topic/6532348/download")
    assert url == "https://video.twimg.com/amplify_video/1/pl/main.m3u8?tag=29"
    assert fetched == ["https://bad.news/t/6532348"]


async def test_resolve_raises_when_topic_has_no_video(monkeypatch):
    async def _fetch(url):
        return "<html>login required</html>"
    monkeypatch.setattr(sources, "_fetch_text", _fetch)
    with pytest.raises(NoSingleVideo):
        await resolve_source_url("https://bad.news/t/42")
