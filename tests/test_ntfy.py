"""ntfy 渠道测试。全部离线，用 httpx.MockTransport。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.notifier import Message, list_notifiers
from radar.notifier.ntfy import MAX_MESSAGE_BYTES, NtfyNotifier, clip_utf8
from tests.conftest import mock_client

MSG = Message(
    title="【余票提醒】北京南 → 上海虹桥",
    body="**北京南 → 上海虹桥**\n\nG1　二等座　5 张",
    url="https://www.12306.cn/index/",
)


def test_ntfy_is_registered():
    assert "ntfy" in list_notifiers()


def test_ntfy_posts_json_to_server_root():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "abc", "topic": "my-topic"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "my-topic"}, client).send(MSG)

    asyncio.run(scenario())

    # ntfy 的 JSON 发布接口是根路径，topic 放在 body 里
    assert captured["url"] == "https://ntfy.sh/"
    payload = json.loads(captured["json"])
    assert payload["topic"] == "my-topic"
    assert payload["title"] == MSG.title
    assert payload["markdown"] is True
    assert payload["click"] == "https://www.12306.cn/index/"
    assert "二等座" in payload["message"]


def test_ntfy_keeps_chinese_title_out_of_http_headers():
    """回归测试：中文标题绝不能走 HTTP header。

    HTTP 头按规范只能是 ASCII，把「【余票提醒】…」塞进 Title 头，
    h11 会直接拒绝整个请求。所以必须走 JSON body —— 这条测试就是防回退。
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "abc"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t"}, client).send(MSG)

    asyncio.run(scenario())

    assert "title" not in captured["headers"], "标题不该出现在 HTTP 头里"
    assert "content-type" in captured["headers"]
    assert "json" in captured["headers"]["content-type"]
    # 标题在 body 里，且中文完好
    assert "【余票提醒】" in json.loads(captured["json"])["title"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [("high", 4), ("urgent", 5), ("min", 1), ("default", 3), (2, 2), (99, 5), ("乱写", 3)],
)
def test_ntfy_priority_accepts_words_and_numbers(given, expected):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "x"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t", "priority": given}, client).send(MSG)

    asyncio.run(scenario())
    assert json.loads(captured["json"])["priority"] == expected


def test_ntfy_defaults_priority_when_absent():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "x"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t"}, client).send(MSG)

    asyncio.run(scenario())
    assert json.loads(captured["json"])["priority"] == 3


def test_ntfy_parses_comma_separated_tags():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "x"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t", "tags": "rotating_light, tada"}, client).send(MSG)

    asyncio.run(scenario())
    assert json.loads(captured["json"])["tags"] == ["rotating_light", "tada"]


def test_ntfy_supports_self_hosted_server():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "x"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier(
                {"topic": "t", "server": "https://ntfy.example.com/"}, client
            ).send(MSG)

    asyncio.run(scenario())
    assert captured["url"] == "https://ntfy.example.com/"


def test_ntfy_requires_topic():
    with pytest.raises(ValueError, match="topic"):
        NtfyNotifier({}, None)


def test_ntfy_raises_on_error_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "topic is invalid"})

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t"}, client).send(MSG)

    with pytest.raises(RuntimeError, match="topic is invalid"):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 截断逻辑：ntfy 单条上限 4096 字节，超了会被服务端转成附件
# ---------------------------------------------------------------------------


def test_clip_keeps_short_text_untouched():
    assert clip_utf8("短消息") == "短消息"


def test_clip_truncates_long_text_within_byte_budget():
    big = "中" * 2000  # 6000 字节
    out = clip_utf8(big)
    assert len(out.encode("utf-8")) < len(big.encode("utf-8"))
    assert "已截断" in out


def test_clip_never_splits_a_multibyte_character():
    """在字节中间切开会产生非法 UTF-8，解码时必须不炸。"""
    # 用一个能让 3800 正好落在「中」字中间的字符串
    for count in range(1200, 1300):
        out = clip_utf8("中" * count)
        out.encode("utf-8").decode("utf-8")  # 不抛异常就算过


def test_ntfy_send_clips_oversized_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"id": "x"})

    huge = Message(title="大", body="中" * 3000)

    async def scenario():
        async with mock_client(handler) as client:
            await NtfyNotifier({"topic": "t"}, client).send(huge)

    asyncio.run(scenario())
    sent = json.loads(captured["json"])["message"]
    assert len(sent.encode("utf-8")) <= MAX_MESSAGE_BYTES + 64
    assert "已截断" in sent
