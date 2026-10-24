import pytest

from bot.ui.markup import caption_markdown, fmt_duration, fmt_size, md, post_link, progress_bar


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
