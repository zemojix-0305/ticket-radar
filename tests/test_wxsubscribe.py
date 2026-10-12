"""wxsubscribe（微信小程序订阅消息）渠道测试。不联网，token 与发送全走 MockTransport。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.config import AppConfig, NotifyConfig
from radar.notifier import build_notifiers, list_notifiers
from radar.notifier.base import Message, NotConfiguredError
from radar.notifier.wxsubscribe import WxSubscribeNotifier, _clip

VALID_OPTS = {
    "app_id": "wxtest1234567890ab",
    "app_secret": "secret-abc",
    "template_id": "tmpl-123",
    "open_id": "oABC123",
}


def _make(handler) -> WxSubscribeNotifier:
    return WxSubscribeNotifier(VALID_OPTS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_registered():
    assert "wxsubscribe" in list_notifiers()


def test_missing_credentials_is_not_configured():
    """四项必填缺一个都算「没配」，应静默跳过而不是报错。"""
    for drop in ("app_id", "app_secret", "template_id", "open_id"):
        opts = {k: v for k, v in VALID_OPTS.items() if k != drop}
        with pytest.raises(NotConfiguredError):
            WxSubscribeNotifier(opts)


def test_send_success_payload():
    """成功路径：先取 token，再发订阅消息；thing 字段来自 title/plain_text。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "cgi-bin/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "TK", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    notifier = _make(handler)
    msg = Message(
        title="【余票提醒】北京南 → 上海虹桥",
        body="**G1** 二等座 5 张\n[去下单](https://example.com)",
        url="https://example.com",
    )
    asyncio.run(notifier.send(msg))

    assert len(calls) == 2
    send_req = calls[1]
    assert "access_token=TK" in str(send_req.url)
    payload = json.loads(send_req.content)
    assert payload["touser"] == "oABC123"
    assert payload["template_id"] == "tmpl-123"
    assert payload["page"] == "pages/index/index"
    # title 进 thing1，body 的 Markdown 被降级成纯文本进 thing2；
    # URL 不进正文——thing 只有 20 字放不下，点消息本来就会跳小程序页面
    assert payload["data"]["thing1"]["value"] == "【余票提醒】北京南 → 上海虹桥"
    assert "**" not in payload["data"]["thing2"]["value"]
    assert "https://example.com" not in payload["data"]["thing2"]["value"]
    assert "二等座" in payload["data"]["thing2"]["value"]


def test_thing_field_truncated_to_20_chars():
    assert _clip("一" * 30) == "一" * 19 + "…"
    assert len(_clip("一" * 30)) == 20


def test_43101_gives_friendly_hint():
    """额度用完是最常见的坑，异常里必须写清楚怎么续。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "cgi-bin/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "TK", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 43101, "errmsg": "user refuse to accept the msg"})

    notifier = _make(handler)
    with pytest.raises(RuntimeError, match="43101"):
        asyncio.run(notifier.send(Message(title="t", body="b")))


def test_invalid_secret_raises_on_token_step():
    """secret 错了会在取 token 那一步就失败，不该继续发。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "cgi-bin/token" in request.url.path:
            return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})
        return httpx.Response(200, json={"errcode": 0})

    notifier = _make(handler)
    with pytest.raises(RuntimeError, match="40001"):
        asyncio.run(notifier.send(Message(title="t", body="b")))
    assert all("token" in p for p in calls)


def test_token_cached_between_sends():
    """token 要缓存：连发两条只取一次 token，不然两小时额度会被自己刷爆。"""
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if "cgi-bin/token" in request.url.path:
            token_calls += 1
            return httpx.Response(200, json={"access_token": "TK", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0})

    notifier = _make(handler)
    asyncio.run(notifier.send(Message(title="t", body="b")))
    asyncio.run(notifier.send(Message(title="t", body="b")))
    assert token_calls == 1


def test_expired_token_retried_once():
    """发送时若返回 40001（token 失效），应强制刷新 token 并重试一次。"""
    token_calls = 0
    send_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, send_calls
        if "cgi-bin/token" in request.url.path:
            token_calls += 1
            return httpx.Response(200, json={"access_token": f"TK{token_calls}", "expires_in": 7200})
        send_calls += 1
        # 第一次发送报 token 失效，第二次（刷新后）成功
        return httpx.Response(
            200,
            json={"errcode": 0} if send_calls >= 2 else {"errcode": 40001, "errmsg": "access_token expired"},
        )

    notifier = _make(handler)
    asyncio.run(notifier.send(Message(title="t", body="b")))
    assert token_calls == 2 and send_calls == 2


def test_custom_field_keys():
    """模板字段名可配，默认 thing1/thing2。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "cgi-bin/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "TK", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0})

    opts = {**VALID_OPTS, "field_title": "thing5", "field_body": "thing9"}
    notifier = WxSubscribeNotifier(opts, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    seen: dict = {}

    async def capture() -> None:
        original = notifier._post_send

        async def spy(token, payload):
            seen.update(payload)
            return await original(token, payload)

        notifier._post_send = spy  # type: ignore[method-assign]
        await notifier.send(Message(title="标题", body="正文"))

    asyncio.run(capture())
    assert "thing5" in seen["data"] and "thing9" in seen["data"]


def test_build_notifiers_skips_empty_wxsubscribe(caplog):
    """示例配置里 enabled 但凭据为空 → 静默跳过，这是开源项目的常态。"""
    cfg = AppConfig(notify=[NotifyConfig(type="wxsubscribe", options={})])
    assert build_notifiers(cfg, httpx.AsyncClient()) == []
