"""PushPlus 渠道测试。全部离线，用 httpx.MockTransport。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.notifier import Message, list_notifiers
from radar.notifier.pushplus import PushPlusNotifier
from tests.conftest import mock_client

MSG = Message(
    title="【余票提醒】北京南 → 上海虹桥",
    body="**北京南 → 上海虹桥**\n\nG1　二等座　5 张",
    url="https://www.12306.cn/index/",
)


def test_pushplus_is_registered():
    assert "pushplus" in list_notifiers()


def test_pushplus_posts_markdown_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200, "msg": "请求成功"})

    async def scenario():
        async with mock_client(handler) as client:
            notifier = PushPlusNotifier({"token": "PP_TOKEN"}, client)
            await notifier.send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://www.pushplus.plus/send"
    payload = json.loads(captured["json"])
    assert payload["token"] == "PP_TOKEN"
    assert payload["template"] == "markdown"
    assert payload["channel"] == "wechat"
    assert "余票提醒" in payload["title"]
    assert "二等座" in payload["content"]
    assert "12306.cn" in payload["content"]


def test_pushplus_raises_with_hint_on_business_error():
    """业务码不是 HTTP 状态码，200 响应里也可能是失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 903, "msg": "无效的用户令牌"})

    async def scenario():
        async with mock_client(handler) as client:
            notifier = PushPlusNotifier({"token": "bad"}, client)
            await notifier.send(MSG)

    with pytest.raises(RuntimeError, match="token 无效"):
        asyncio.run(scenario())


def test_pushplus_surfaces_unverified_account():
    """905 = 没实名。这是新用户最容易踩的坑，错误信息必须说人话。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 905, "msg": "未实名"})

    async def scenario():
        async with mock_client(handler) as client:
            notifier = PushPlusNotifier({"token": "t"}, client)
            await notifier.send(MSG)

    with pytest.raises(RuntimeError, match="实名"):
        asyncio.run(scenario())


def test_pushplus_requires_token():
    with pytest.raises(ValueError, match="token"):
        PushPlusNotifier({}, None)


def test_pushplus_accepts_string_code():
    """平台偶尔把 code 序列化成字符串，别把它误判成失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "200"})

    async def scenario():
        async with mock_client(handler) as client:
            notifier = PushPlusNotifier({"token": "t"}, client)
            await notifier.send(MSG)

    asyncio.run(scenario())


def test_pushplus_allows_overriding_template_channel_and_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200})

    async def scenario():
        async with mock_client(handler) as client:
            notifier = PushPlusNotifier(
                {
                    "token": "t",
                    "template": "txt",
                    "channel": "mail",
                    "endpoint": "https://relay.example.com/send/",
                },
                client,
            )
            await notifier.send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://relay.example.com/send"
    payload = json.loads(captured["json"])
    assert payload["template"] == "txt"
    assert payload["channel"] == "mail"
