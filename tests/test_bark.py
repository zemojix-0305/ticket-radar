"""Bark 渠道测试。全部离线，用 httpx.MockTransport。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.notifier import Message, list_notifiers
from radar.notifier.bark import BarkNotifier, parse_key
from tests.conftest import mock_client

MSG = Message(
    title="【余票提醒】北京南 → 上海虹桥",
    body="**北京南 → 上海虹桥**\n\n[点此打开](https://www.12306.cn/index/)",
    url="https://www.12306.cn/index/",
)


def test_bark_is_registered():
    assert "bark" in list_notifiers()


# ---------------------------------------------------------------------------
# parse_key：自建用户常直接粘贴完整地址，得能认出来
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "want_key", "want_server"),
    [
        ("abc123", "abc123", None),
        ("https://api.day.app/abc123", "abc123", "https://api.day.app"),
        ("https://api.day.app/abc123/", "abc123", "https://api.day.app"),
        ("https://bark.example.com/abc123", "abc123", "https://bark.example.com"),
        ("https://api.day.app/abc123/push", "abc123", "https://api.day.app"),
        ("", "", None),
    ],
)
def test_parse_key(given, want_key, want_server):
    assert parse_key(given) == (want_key, want_server)


def test_bark_uses_json_push_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200, "message": "success"})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier({"key": "abc123"}, client).send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://api.day.app/push"
    payload = json.loads(captured["json"])
    assert payload["device_key"] == "abc123"
    assert payload["title"] == MSG.title
    assert payload["url"] == "https://www.12306.cn/index/"


def test_bark_degrades_markdown_to_plain_text():
    """Bark 不认 Markdown，正文必须降级成纯文本。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier({"key": "k"}, client).send(MSG)

    asyncio.run(scenario())
    body = json.loads(captured["json"])["body"]
    assert "**" not in body
    assert "北京南 → 上海虹桥" in body


def test_bark_infers_server_from_pasted_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier({"key": "https://bark.mine.dev/xyz"}, client).send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://bark.mine.dev/push"
    assert json.loads(captured["json"])["device_key"] == "xyz"


def test_bark_explicit_server_wins_over_inferred():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": 200})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier(
                {"key": "abc", "server": "https://bark.mine.dev/"}, client
            ).send(MSG)

    asyncio.run(scenario())
    assert captured["url"] == "https://bark.mine.dev/push"


def test_bark_passes_group_sound_and_level():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 200})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier(
                {"key": "k", "group": "余票", "sound": "alarm", "level": "critical"},
                client,
            ).send(MSG)

    asyncio.run(scenario())
    payload = json.loads(captured["json"])
    assert payload["group"] == "余票"
    assert payload["sound"] == "alarm"
    assert payload["level"] == "critical"


def test_bark_requires_key():
    with pytest.raises(ValueError, match="key"):
        BarkNotifier({}, None)


def test_bark_raises_on_business_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 400, "message": "device token is invalid"})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier({"key": "bad"}, client).send(MSG)

    with pytest.raises(RuntimeError, match="device token is invalid"):
        asyncio.run(scenario())


def test_bark_accepts_string_ok_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "200"})

    async def scenario():
        async with mock_client(handler) as client:
            await BarkNotifier({"key": "k"}, client).send(MSG)

    asyncio.run(scenario())  # 不该抛异常
