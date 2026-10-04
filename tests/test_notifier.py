"""通知渠道测试。用 httpx.MockTransport，不联网。"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from radar.config import AppConfig, NotifyConfig
from radar.notifier import NotifierHub, build_notifiers, list_notifiers
from radar.notifier.base import Message, Notifier
from radar.notifier.dingtalk import DingTalkNotifier
from radar.notifier.serverchan import ServerChanNotifier
from radar.notifier.telegram import TelegramNotifier
from radar.notifier.wecom import WeComNotifier
from tests.conftest import mock_client

MSG = Message(
    title="【余票提醒】北京南 → 上海虹桥", body="G1 二等座 5 张", url="https://example.com"
)


def test_all_channels_registered():
    for name in (
        "ntfy",
        "bark",
        "pushplus",
        "serverchan",
        "dingtalk",
        "wecom",
        "telegram",
        "smtp",
    ):
        assert name in list_notifiers()


def test_recommended_free_channels_are_all_registered():
    """README 主推的「零注册 / 永久免费」渠道必须真实存在，不能是文档里的空头支票。"""
    from radar.notifier import RECOMMENDED_FREE

    assert set(RECOMMENDED_FREE) <= set(list_notifiers())


# --- 「没配」与「配错」必须区分对待 ----------------------------------------


def test_unconfigured_channels_are_skipped_silently(caplog):
    """凭据为空是「还没选渠道」，不是故障——已有可用渠道时不该产生警告噪音。"""
    import logging

    cfg = AppConfig(
        notify=[
            NotifyConfig(type="ntfy", options={"topic": ""}),  # 空 topic
            NotifyConfig(type="pushplus", options={}),  # 空 token
            NotifyConfig(type="wecom", options={"webhook": "https://x/y"}),
        ]
    )

    async def scenario():
        async with mock_client(lambda r: httpx.Response(200, json={"errcode": 0})) as client:
            return build_notifiers(cfg, client)

    with caplog.at_level(logging.WARNING, logger="radar.notifier"):
        result = asyncio.run(scenario())

    assert [n.name for n in result] == ["wecom"]
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], f"未配置的渠道不该报错：{[r.message for r in noisy]}"


def test_all_unconfigured_points_to_cheapest_start(caplog):
    """一个渠道都没配成时，要给可执行的引导，而不是让用户自己猜。"""
    import logging

    cfg = AppConfig(notify=[NotifyConfig(type="pushplus", options={})])

    async def scenario():
        async with mock_client(lambda r: httpx.Response(200)) as client:
            return build_notifiers(cfg, client)

    with caplog.at_level(logging.WARNING, logger="radar.notifier"):
        result = asyncio.run(scenario())

    assert result == []
    assert "ntfy" in caplog.text
    assert "不用注册" in caplog.text


def test_real_config_error_still_warns(monkeypatch, caplog):
    """「配错了」必须和「没配」区分开——前者要能被看见，否则用户排查无门。"""
    import logging

    from radar.notifier import registry as reg

    def boom(name, options=None, client=None):
        raise RuntimeError("webhook 不是合法 URL")

    monkeypatch.setattr(reg, "create_notifier", boom)
    cfg = AppConfig(notify=[NotifyConfig(type="wecom", options={"webhook": "x"})])

    async def scenario():
        async with mock_client(lambda r: httpx.Response(200)) as client:
            return build_notifiers(cfg, client)

    with caplog.at_level(logging.WARNING, logger="radar.notifier"):
        result = asyncio.run(scenario())

    assert result == []
    assert any("webhook 不是合法 URL" in str(r.message) for r in caplog.records)


def test_notify_literal_matches_registry():
    """NotifyConfig.type 是硬编码的 Literal，必须和注册表严格一致。

    加渠道时很容易只改注册表、忘了改 Literal——那样示例配置会直接加载失败，
    而且报错指向 YAML 而非代码，很难查。这条测试就是防这个。
    """
    from typing import get_args

    from radar.config import NotifyConfig

    declared = set(get_args(NotifyConfig.model_fields["type"].annotation))
    assert declared == set(list_notifiers())


# --- Server酱 --------------------------------------------------------------


def test_serverchan_posts_to_sendkey_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 0, "message": "ok"})

    async def scenario():
        async with mock_client(handler) as client:
            n = ServerChanNotifier({"sendkey": "SCT_ABC"}, client)
            await n.send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://sctapi.ftqq.com/SCT_ABC.send"
    form = parse_qs(captured["body"])
    assert form["title"][0] == MSG.title
    assert "二等座" in form["desp"][0]
    assert "example.com" in form["desp"][0]


def test_serverchan_raises_on_error_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "message": "bad key"})

    async def scenario():
        async with mock_client(handler) as client:
            n = ServerChanNotifier({"sendkey": "bad"}, client)
            await n.send(MSG)

    with pytest.raises(RuntimeError, match="bad key"):
        asyncio.run(scenario())


def test_serverchan_requires_sendkey():
    with pytest.raises(ValueError, match="sendkey"):
        ServerChanNotifier({}, None)


# --- 钉钉 ------------------------------------------------------------------


def test_dingtalk_markdown_payload_with_sign():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async def scenario():
        async with mock_client(handler) as client:
            n = DingTalkNotifier(
                {
                    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=tok",
                    "secret": "SEC123",
                },
                client,
            )
            await n.send(MSG)

    asyncio.run(scenario())

    query = parse_qs(urlparse(captured["url"]).query)
    assert "timestamp" in query and "sign" in query
    assert "access_token=tok" in captured["url"]

    payload = json.loads(captured["json"])
    assert payload["msgtype"] == "markdown"
    assert "余票提醒" in payload["markdown"]["title"]
    assert "二等座" in payload["markdown"]["text"]
    assert "example.com" in payload["markdown"]["text"]


def test_dingtalk_without_secret_has_no_sign():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"errcode": 0})

    async def scenario():
        async with mock_client(handler) as client:
            n = DingTalkNotifier(
                {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=t"}, client
            )
            await n.send(MSG)

    asyncio.run(scenario())
    assert "sign=" not in captured["url"]


def test_dingtalk_raises_on_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

    async def scenario():
        async with mock_client(handler) as client:
            n = DingTalkNotifier({"webhook": "https://x/y"}, client)
            await n.send(MSG)

    with pytest.raises(RuntimeError, match="钉钉返回错误"):
        asyncio.run(scenario())


# --- 企业微信 --------------------------------------------------------------


def test_wecom_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"errcode": 0})

    async def scenario():
        async with mock_client(handler) as client:
            n = WeComNotifier(
                {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"}, client
            )
            await n.send(MSG)

    asyncio.run(scenario())
    payload = json.loads(captured["json"])
    assert payload["msgtype"] == "markdown"
    assert "warning" in payload["markdown"]["content"]
    assert "二等座" in payload["markdown"]["content"]


def test_wecom_raises_on_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    async def scenario():
        async with mock_client(handler) as client:
            n = WeComNotifier({"webhook": "https://x/y"}, client)
            await n.send(MSG)

    with pytest.raises(RuntimeError, match="企业微信返回错误"):
        asyncio.run(scenario())


# --- Telegram --------------------------------------------------------------


def test_telegram_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with mock_client(handler) as client:
            n = TelegramNotifier({"bot_token": "123:ABC", "chat_id": "999"}, client)
            await n.send(MSG)

    asyncio.run(scenario())

    assert captured["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    payload = json.loads(captured["json"])
    assert payload["chat_id"] == "999"
    assert payload["parse_mode"] == "Markdown"
    assert MSG.title in payload["text"]


def test_telegram_base_url_override():
    """国内直连 api.telegram.org 不通，要允许换成自建反代。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with mock_client(handler) as client:
            n = TelegramNotifier(
                {"bot_token": "t", "chat_id": "c", "base_url": "https://tg.mirror.local"},
                client,
            )
            await n.send(MSG)

    asyncio.run(scenario())
    assert captured["url"].startswith("https://tg.mirror.local/bott/sendMessage")


def test_telegram_raises_when_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    async def scenario():
        async with mock_client(handler) as client:
            n = TelegramNotifier({"bot_token": "t", "chat_id": "bad"}, client)
            await n.send(MSG)

    with pytest.raises(RuntimeError, match="chat not found"):
        asyncio.run(scenario())


# --- Hub 扇出 --------------------------------------------------------------


class _Ok(Notifier):
    def __init__(self, name: str = "ok"):
        super().__init__({}, None)
        self.name = name
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


class _Bad(Notifier):
    def __init__(self, name: str = "bad"):
        super().__init__({}, None)
        self.name = name

    async def send(self, message: Message) -> None:
        raise RuntimeError(f"{self.name} boom")


def test_hub_fans_out_to_all():
    a, b = _Ok("a"), _Ok("b")
    hub = NotifierHub([a, b])
    failures = asyncio.run(hub.send(MSG))

    assert failures == {}
    assert len(a.sent) == 1 and len(b.sent) == 1


def test_hub_isolates_partial_failure():
    """一个渠道挂了不能让整轮监控算失败。"""
    ok, bad = _Ok(), _Bad()
    hub = NotifierHub([ok, bad])

    failures = asyncio.run(hub.send(MSG))

    assert "bad" in failures
    assert "boom" in failures["bad"]
    assert len(ok.sent) == 1


def test_hub_raises_when_all_fail():
    hub = NotifierHub([_Bad("x"), _Bad("y")])
    with pytest.raises(RuntimeError, match="所有通知渠道均失败"):
        asyncio.run(hub.send(MSG))


def test_hub_with_no_channels_is_noop():
    hub = NotifierHub([])
    assert bool(hub) is False
    assert asyncio.run(hub.send(MSG)) == {}


# --- 工厂 ------------------------------------------------------------------


def test_build_notifiers_skips_broken_config():
    """漏填 sendkey 只跳过该渠道，不能让整个程序起不来。"""
    cfg = AppConfig(
        notify=[
            NotifyConfig(type="serverchan", options={}),                    # 缺 sendkey
            NotifyConfig(type="wecom", options={"webhook": "https://x/y"}),  # OK
        ]
    )

    async def scenario():
        async with mock_client(lambda r: httpx.Response(200, json={"errcode": 0})) as client:
            return build_notifiers(cfg, client)

    assert [n.name for n in asyncio.run(scenario())] == ["wecom"]


def test_build_notifiers_respects_enabled_flag():
    cfg = AppConfig(
        notify=[
            NotifyConfig(type="wecom", enabled=False, options={"webhook": "https://x"}),
            NotifyConfig(type="wecom", enabled=True, options={"webhook": "https://y"}),
        ]
    )

    async def scenario():
        async with mock_client(lambda r: httpx.Response(200, json={"errcode": 0})) as client:
            return build_notifiers(cfg, client)

    assert len(asyncio.run(scenario())) == 1


# --- 纯文本降级 ------------------------------------------------------------


def test_plain_text_strips_markdown_for_email():
    msg = Message(title="t", body="**加粗** 和 [链接](https://a.example.com)")
    plain = msg.plain_text
    assert "**" not in plain
    assert "[" not in plain
    assert "链接" in plain and "https://a.example.com" in plain
