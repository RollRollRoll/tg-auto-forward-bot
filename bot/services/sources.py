from __future__ import annotations
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx


class NoSingleVideo(Exception):
    """链接解析不出单个视频；异常信息直接展示给管理员。"""


# bad.news 的话题下载接口和话题页：下载接口未登录时只返回登录提示页，统一改用话题页
_BAD_NEWS_TOPIC = re.compile(
    r"^https?://(?:www\.)?bad\.news/(?:ajax/topic/(\d+)/download|t/(\d+))(?:[/?#]|$)",
    re.IGNORECASE,
)


def bad_news_topic_id(url: str) -> str | None:
    m = _BAD_NEWS_TOPIC.match(url)
    return (m.group(1) or m.group(2)) if m else None


class _VideoSourceParser(HTMLParser):
    # 话题页里除了主视频还有推荐列表，按 data-id 找到主视频那个 <video>
    def __init__(self, topic_id: str):
        super().__init__()
        self.topic_id = topic_id
        self.source: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "video" or self.source is not None:
            return
        attrs = dict(attrs)
        if attrs.get("data-id") == self.topic_id and attrs.get("data-source"):
            self.source = attrs["data-source"]


def parse_bad_news_video(html: str, topic_id: str, page_url: str) -> str | None:
    parser = _VideoSourceParser(topic_id)
    parser.feed(html)
    return urljoin(page_url, parser.source) if parser.source else None


async def _fetch_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def resolve_source_url(url: str) -> str:
    """把 yt-dlp 无法直接处理的链接换成视频本身的地址，其他链接原样返回。"""
    topic_id = bad_news_topic_id(url)
    if topic_id is None:
        return url
    page_url = f"https://bad.news/t/{topic_id}"
    video_url = parse_bad_news_video(await _fetch_text(page_url), topic_id, page_url)
    if video_url is None:
        raise NoSingleVideo("No video found on this bad.news topic page.")
    return video_url
