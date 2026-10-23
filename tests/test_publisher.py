import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError
from bot.services.publisher import UPLOAD_READ_TIMEOUT, publish_video

@pytest.mark.asyncio
async def test_publish_video_success():
    bot = AsyncMock()
    bot.send_video.return_value = MagicMock(message_id=42)
    result = await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4",
        caption="hello", duration=10, width=1920, height=1080,
    )
    assert result == 42
    bot.send_video.assert_awaited_once()
    assert bot.send_video.call_args.kwargs["supports_streaming"] is True

@pytest.mark.asyncio
async def test_publish_video_falls_back_to_document():
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("Wrong file identifier")
    bot.send_document.return_value = MagicMock(message_id=99)
    result = await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
    )
    assert result == 99
    bot.send_document.assert_awaited_once()

@pytest.mark.asyncio
async def test_publish_video_uses_long_read_timeout():
    # 大文件上传要等 Bot API Server 传完才返回，不能用默认的 5 秒
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("codec error")
    bot.send_document.return_value = MagicMock(message_id=99)
    await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
    )
    assert bot.send_video.call_args.kwargs["read_timeout"] == UPLOAD_READ_TIMEOUT
    assert bot.send_document.call_args.kwargs["read_timeout"] == UPLOAD_READ_TIMEOUT
    assert UPLOAD_READ_TIMEOUT >= 600

@pytest.mark.asyncio
async def test_publish_video_both_fail():
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("video error")
    bot.send_document.side_effect = Exception("doc error")
    result = await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
    )
    assert result is None

@pytest.mark.asyncio
async def test_publish_video_fallback_truncates_long_caption():
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("codec error")
    bot.send_document.return_value = MagicMock(message_id=99)
    long_caption = "a" * 1024
    await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption=long_caption,
    )
    call_kwargs = bot.send_document.call_args.kwargs
    assert len(call_kwargs["caption"]) <= 1024
    assert "parse_mode" not in call_kwargs

@pytest.mark.asyncio
async def test_publish_video_forbidden_raises():
    bot = AsyncMock()
    bot.send_video.side_effect = Forbidden("bot was kicked")
    with pytest.raises(Forbidden):
        await publish_video(
            bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
        )

@pytest.mark.asyncio
async def test_publish_video_timed_out_raises():
    bot = AsyncMock()
    bot.send_video.side_effect = TimedOut()
    with pytest.raises(TimedOut):
        await publish_video(
            bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
        )

@pytest.mark.asyncio
async def test_publish_video_network_error_raises():
    bot = AsyncMock()
    bot.send_video.side_effect = NetworkError("connection reset")
    with pytest.raises(NetworkError):
        await publish_video(
            bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="hello",
        )

@pytest.mark.asyncio
async def test_publish_video_fallback_strips_html():
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("codec error")
    bot.send_document.return_value = MagicMock(message_id=99)
    await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4",
        caption="<b>hello</b> &amp; world",
    )
    call_kwargs = bot.send_document.call_args.kwargs
    assert "<b>" not in call_kwargs["caption"]
    assert "hello & world" in call_kwargs["caption"]

@pytest.mark.asyncio
async def test_publish_video_fallback_no_parse_mode():
    bot = AsyncMock()
    bot.send_video.side_effect = BadRequest("codec error")
    bot.send_document.return_value = MagicMock(message_id=99)
    await publish_video(
        bot, channel_chat_id=-100123, file_path="/tmp/test.mp4", caption="test",
    )
    assert "parse_mode" not in bot.send_document.call_args.kwargs
