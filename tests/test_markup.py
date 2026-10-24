import pytest

from bot.ui.markup import caption_markdown, fmt_duration, fmt_size, md, post_link, progress_bar, relative_time, short_url


@pytest.mark.parametrize("chat_id,username,expected", [
    (-1001234567890, "demo", "https://t.me/demo/79"),
    (-1001234567890, None, "https://t.me/c/1234567890/79"),
])
def test_post_link_for_public_and_private_channels(chat_id, username, expected):
    assert post_link(chat_id, username, 79) == expected


@pytest.mark.parametrize("pct,expected", [(0, "░" * 10), (62.0, "██████░░░░"), (100, "█" * 10), (130, "█" * 10)])
def test_progress_bar(pct, expected):
    assert progress_bar(pct) == expected


def test_sizes_and_durations_are_human_readable():
    assert fmt_size(205.3 * 1024 * 1024) == "205.3 MB"
    assert fmt_size(2.5 * 1024 ** 3) == "2.50 GB"
    assert fmt_size(512 * 1024) == "512 KB"
    assert (fmt_duration(31), fmt_duration(63), fmt_duration(3725)) == ("31s", "1m 3s", "1h 2m")


def test_md_escapes_markdown_specials():
    assert md("a_b*c [x](y) #1") == r"a\_b\*c \[x\]\(y\) \#1"


def test_caption_markdown_renders_the_html_tags_bot_allows():
    html = '<b>Title</b> &amp; <i>more</i> <s>old</s> <code>x_y</code> <a href="https://e.com">link</a>'
    assert caption_markdown(html) == r"**Title** & _more_ ~~old~~ `x_y` [link](https://e.com)"


def test_caption_markdown_escapes_plain_text():
    assert caption_markdown("1. not a list * star") == r"1\. not a list \* star"


def test_caption_markdown_keeps_line_breaks():
    # Rich Markdown 会把普通换行合并成空格，caption 里的换行要变成硬换行
    assert caption_markdown("line one\nline two") == "line one\\\nline two"


def test_caption_markdown_keeps_pre_blocks_verbatim():
    assert caption_markdown("<pre>a_b\n*c</pre>") == "```\na_b\n*c\n```"


@pytest.mark.parametrize("seconds,expected", [
    (0, "just now"), (59, "just now"), (60, "1m ago"), (3599, "59m ago"),
    (3600, "1h ago"), (86399, "23h ago"), (86400, "1d ago"), (3 * 86400 + 5, "3d ago"),
    # 结束时间晚于当前时间（时钟偏差）
    (-30, "just now"),
])
def test_relative_time(seconds, expected):
    assert relative_time(seconds) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=5DslLmsx9s0", "youtube.com/watch?v=5DslLmsx9s0"),
    ("http://archive.org/details/BigBuckBunny_124", "archive.org/details/BigBuckBunny_124"),
    # 只去掉开头的 www.，路径里的不动
    ("https://example.com/www.page", "example.com/www.page"),
    # 正好 45 个字符不截断，超过时保留 44 个加 …
    ("https://" + "a" * 45, "a" * 45),
    ("https://" + "a" * 46, "a" * 44 + "…"),
])
def test_short_url(url, expected):
    assert short_url(url) == expected
