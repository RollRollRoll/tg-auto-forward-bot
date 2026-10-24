import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, EndPointNotFound, TimedOut

from bot.ui.sender import Card, RichSender, SentMessage
from tests.fakebot import FakeBot

CHAT = 111


def _card(md="**hi**", html="<b>hi</b>", buttons=("OK",)):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(b, callback_data=b) for b in buttons]]) if buttons else None
    return Card(markdown=md, html=html, keyboard=kb)


@pytest.mark.asyncio
async def test_send_uses_rich_message_with_keyboard():
    bot = FakeBot()
    sent = await RichSender(bot).send(CHAT, _card())
    msg = bot.last(CHAT)
    assert (msg.format, msg.content) == ("rich", "**hi**")
    assert [b["text"] for b in msg.buttons] == ["OK"]
    assert sent == SentMessage(chat_id=CHAT, message_id=msg.message_id, format="rich")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BadRequest("Rich_message_markdown_invalid"), EndPointNotFound("Not Found")])
async def test_send_falls_back_to_html_when_rich_is_rejected(error):
    bot = FakeBot()
    bot.fail("sendRichMessage", error)
    sent = await RichSender(bot).send(CHAT, _card())
    msg = bot.last(CHAT)
    assert (msg.format, msg.content) == ("html", "<b>hi</b>")
    assert [b["text"] for b in msg.buttons] == ["OK"]
    assert sent.format == "html"


@pytest.mark.asyncio
async def test_send_does_not_fall_back_on_timeout():
    bot = FakeBot()
    bot.fail("sendRichMessage", TimedOut())
    with pytest.raises(TimedOut):
        await RichSender(bot).send(CHAT, _card())
    assert bot.messages(CHAT) == []


@pytest.mark.asyncio
async def test_edit_keeps_the_format_the_message_was_sent_with():
    bot = FakeBot()
    bot.fail("sendRichMessage", BadRequest("x"))
    sender = RichSender(bot)
    sent = await sender.send(CHAT, _card())
    assert await sender.edit(sent, _card(md="**new**", html="<b>new</b>")) is True
    msg = bot.last(CHAT)
    assert (msg.format, msg.content) == ("html", "<b>new</b>")
    # 一条消息只要退回过 HTML，之后都不再尝试 rich
    assert bot.methods().count("sendRichMessage") == 1


@pytest.mark.asyncio
async def test_edit_without_keyboard_removes_buttons_explicitly():
    bot = FakeBot()
    sender = RichSender(bot)
    sent = await sender.send(CHAT, _card())
    assert await sender.edit(sent, _card(md="done", html="done", buttons=())) is True
    msg = bot.last(CHAT)
    assert (msg.format, msg.content, msg.buttons) == ("rich", "done", [])
    # 显式传了空键盘，而不是依赖"不传就清掉"
    _, _, kwargs = bot.calls[-1]
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_failed_edit_returns_false_and_keeps_message():
    bot = FakeBot()
    sender = RichSender(bot)
    sent = await sender.send(CHAT, _card())
    bot.fail("editMessageText", TimedOut())
    assert await sender.edit(sent, _card(md="new")) is False
    assert bot.last(CHAT).content == "**hi**"


@pytest.mark.asyncio
async def test_not_modified_counts_as_success():
    bot = FakeBot()
    sender = RichSender(bot)
    sent = await sender.send(CHAT, _card())
    bot.fail("editMessageText", BadRequest("Message is not modified: specified new message content is the same"))
    assert await sender.edit(sent, _card()) is True


@pytest.mark.asyncio
async def test_sent_message_of_detects_format_from_existing_message():
    bot = FakeBot()
    rich = await bot.do_api_request("sendRichMessage", api_kwargs={"chat_id": CHAT, "rich_message": {"markdown": "r"}})
    html = await bot.send_message(CHAT, "<b>h</b>", parse_mode="HTML")
    assert SentMessage.of(rich).format == "rich"
    assert SentMessage.of(html).format == "html"


@pytest.mark.asyncio
async def test_remove_keyboard_clears_buttons_only():
    bot = FakeBot()
    sender = RichSender(bot)
    sent = await sender.send(CHAT, _card())
    await sender.remove_keyboard(sent)
    msg = bot.last(CHAT)
    assert (msg.content, msg.buttons) == ("**hi**", [])
