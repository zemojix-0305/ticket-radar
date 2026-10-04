"""配置助手（radar/assist.py）的离线测试。

全部用 httpx.MockTransport 假装网关，不联网、不花钱。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.assist import (
    LLMConfig,
    LLMNotConfigured,
    build_messages,
    complete,
    extract_json,
    infer_params,
    list_models,
    verify_params,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

#: 一个典型的「私有演出票接口」返回：数组里每个元素既有票档名也有余量。
PAYLOAD = {
    "data": {
        "result": [
            {"skuId": "1001", "priceName": "看台 380", "remainNum": 5, "performId": "P1"},
            {"skuId": "1002", "priceName": "内场 880", "remainNum": 0, "performId": "P1"},
        ]
    }
}

#: 正确配置：元素自身就是票档，所以 seats_path 留空。
GOOD_PARAMS = {
    "items_path": "data.result",
    "item_id": ["skuId"],
    "item_label": ["priceName"],
    "seat_name": ["priceName"],
    "seat_status": ["remainNum"],
}

#: 路径不存在 —— 必须被真实解析器当场否掉。
BAD_PARAMS = {
    "items_path": "data.nope",
    "item_id": ["skuId"],
    "seat_name": ["priceName"],
    "seat_status": ["remainNum"],
}


def llm(**kw) -> LLMConfig:
    base = {"base_url": "https://gw.test/v1", "api_key": "sk-test", "model": "test-model"}
    base.update(kw)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_code_block():
    text = "好的，配置如下：\n```json\n{\"items_path\": \"data.result\"}\n```\n希望有帮助。"
    assert extract_json(text) == {"items_path": "data.result"}


def test_extract_json_from_bare_code_block():
    text = "```\n{\"a\": {\"b\": 2}}\n```"
    assert extract_json(text) == {"a": {"b": 2}}


def test_extract_json_ignores_surrounding_prose():
    text = "我先分析一下。{\"items_path\": \"data.result\"} 以上就是配置。"
    assert extract_json(text) == {"items_path": "data.result"}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("我无法完成这个任务。")


def test_extract_json_nested_braces_prefers_code_block():
    """带嵌套的 JSON 也要能整体取出来，不能只截到第一个 }。"""
    text = '```json\n{"items_path": "a.b", "available_values": ["有", 1]}\n```'
    assert extract_json(text) == {"items_path": "a.b", "available_values": ["有", 1]}


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------


def test_build_messages_contains_shape_and_sample():
    messages = build_messages(PAYLOAD, "查演出余量", "https://ex.test/api")
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    user = messages[1]["content"]

    assert "https://ex.test/api" in user
    assert "查演出余量" in user
    # 结构树必须带上，否则模型只能瞎猜
    assert "data:" in user and "result:" in user
    # 样例值也要带，否则看不出哪个字段像余量
    assert "remainNum" in user and "5" in user


def test_build_messages_default_intent():
    user = build_messages({"a": 1}, "")[1]["content"]
    assert "查看" in user or "查询" in user


def test_build_messages_survives_non_list_payload():
    """没有数组时也要能构造提示词（模型会回 __error__）。"""
    messages = build_messages({"code": 0, "msg": "ok"}, "查余量")
    assert "code" in messages[1]["content"]


# ---------------------------------------------------------------------------
# verify_params —— 用真实适配器当裁判
# ---------------------------------------------------------------------------


def test_verify_params_accepts_correct_paths():
    ok, note = verify_params(PAYLOAD, GOOD_PARAMS)
    assert ok, note
    assert "2 个单元" in note and "2 个票档" in note


def test_verify_params_rejects_missing_path():
    ok, note = verify_params(PAYLOAD, BAD_PARAMS)
    assert not ok
    assert "items_path" in note or "找不到" in note


def test_verify_params_rejects_when_no_seats():
    """单元解出来了但票档是空的 —— 也要判失败，否则会静默漏报。"""
    params = {
        "items_path": "data.result",
        "item_id": ["skuId"],
        "seat_name": ["notExist"],
        "seat_status": ["alsoNotExist"],
    }
    ok, note = verify_params(PAYLOAD, params)
    assert not ok


def test_verify_params_ignores_error_marker_keys():
    """模型偶尔会在正常配置里夹带 __error__，不该因此崩掉。"""
    ok, _ = verify_params(PAYLOAD, {**GOOD_PARAMS, "__note__": "说明"})
    assert ok


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_sends_bearer_and_model():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return _reply("ok")

    async def go():
        async with _client(handler) as c:
            return await complete(llm(), [{"role": "user", "content": "hi"}], client=c)

    assert _run(go()) == "ok"
    assert captured["url"] == "https://gw.test/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["temperature"] == 0


@pytest.mark.parametrize(
    ("status", "keyword"),
    [
        (401, "401"),
        (402, "402"),
        (404, "models"),
        (500, "HTTP 500"),
    ],
)
def test_complete_maps_error_codes_to_guidance(status, keyword):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="boom")

    async def go():
        async with _client(handler) as c:
            await complete(llm(), [{"role": "user", "content": "x"}], client=c)

    with pytest.raises(RuntimeError) as exc:
        _run(go())
    assert keyword in str(exc.value)


def test_complete_reports_proxy_hint_on_connect_error():
    """连不上时必须提示「代理要覆盖命令行」——这是国内最容易踩的坑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy returned 502", request=request)

    async def go():
        async with _client(handler) as c:
            await complete(llm(), [{"role": "user", "content": "x"}], client=c)

    with pytest.raises(RuntimeError) as exc:
        _run(go())
    assert "代理" in str(exc.value)


def test_complete_rejects_empty_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply("   ")

    async def go():
        async with _client(handler) as c:
            await complete(llm(), [{"role": "user", "content": "x"}], client=c)

    with pytest.raises(RuntimeError) as exc:
        _run(go())
    assert "空内容" in str(exc.value)


def test_complete_rejects_response_without_choices():
    """有些网关把错误塞进 200 响应里，不能当成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "quota exceeded"}})

    async def go():
        async with _client(handler) as c:
            await complete(llm(), [{"role": "user", "content": "x"}], client=c)

    with pytest.raises(RuntimeError) as exc:
        _run(go())
    assert "choices" in str(exc.value)


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_returns_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "deepseek-v4-flash"}, {"id": "mimo-v2.5"}, {"no_id": 1}]},
        )

    async def go():
        async with _client(handler) as c:
            return await list_models(llm(), client=c)

    assert _run(go()) == ["deepseek-v4-flash", "mimo-v2.5"]


# ---------------------------------------------------------------------------
# infer_params —— 端到端（含重试）
# ---------------------------------------------------------------------------


def test_infer_succeeds_first_try():
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(json.dumps(GOOD_PARAMS))

    async def go():
        async with _client(handler) as c:
            return await infer_params(PAYLOAD, llm=llm(), client=c, intent="查余量")

    result = _run(go())
    assert result.ok, result.note
    assert result.attempts == 1
    assert result.params["items_path"] == "data.result"
    assert "验证通过" in result.note


def test_infer_feeds_error_back_and_retries():
    """第一次给错路径 → 真实解析器否掉 → 错误回灌 → 第二次改正。"""
    calls: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["messages"])
        if len(calls) == 1:
            return _reply(json.dumps(BAD_PARAMS))
        return _reply(json.dumps(GOOD_PARAMS))

    async def go():
        async with _client(handler) as c:
            return await infer_params(PAYLOAD, llm=llm(), client=c, attempts=2)

    result = _run(go())
    assert result.ok, result.note
    assert result.attempts == 2
    assert len(calls) == 2

    # 第二次请求里必须带上失败原因，否则模型没有纠正依据
    second = " ".join(m["content"] for m in calls[1])
    assert "没有通过" in second or "修正" in second
    # 且第一次的（错误）回答要在上下文里
    assert any(m["role"] == "assistant" for m in calls[1])


def test_infer_gives_up_after_attempts(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(json.dumps(BAD_PARAMS))

    async def go():
        async with _client(handler) as c:
            return await infer_params(PAYLOAD, llm=llm(), client=c, attempts=2)

    result = _run(go())
    assert not result.ok
    assert result.attempts == 2
    assert result.params == {}
    # 中间过程要留痕，便于用户判断是模型不行还是结构树给少了
    assert len(result.trail) == 2


def test_infer_retries_on_unparseable_reply():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply("我想想……这个结构有点复杂。")
        return _reply(json.dumps(GOOD_PARAMS))

    async def go():
        async with _client(handler) as c:
            return await infer_params(PAYLOAD, llm=llm(), client=c, attempts=2)

    result = _run(go())
    assert result.ok
    assert result.attempts == 2


def test_infer_honours_error_marker():
    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(json.dumps({"__error__": "结构里没有余量字段"}))

    async def go():
        async with _client(handler) as c:
            return await infer_params(PAYLOAD, llm=llm(), client=c, attempts=2)

    result = _run(go())
    assert not result.ok
    assert "没有余量字段" in result.note
    # 模型明确说做不到，不该再浪费一次调用
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# LLMConfig.from_env
# ---------------------------------------------------------------------------


def test_llm_config_from_env_full():
    cfg = LLMConfig.from_env(
        {"LLM_API_KEY": "sk-abc", "LLM_BASE_URL": "https://gw.test/v1/", "LLM_MODEL": "m1"}
    )
    assert cfg.api_key == "sk-abc"
    assert cfg.base_url == "https://gw.test/v1"  # 末尾斜杠被规范化
    assert cfg.model == "m1"


def test_llm_config_defaults():
    cfg = LLMConfig.from_env({"LLM_API_KEY": "sk-abc"})
    assert cfg.base_url == "https://api.b.ai/v1"
    assert cfg.model == "deepseek-v4-flash"


def test_llm_config_missing_key_raises_with_guidance():
    with pytest.raises(LLMNotConfigured) as exc:
        LLMConfig.from_env({})
    text = str(exc.value)
    assert "LLM_API_KEY" in text
    # 必须说明这功能是可选的，否则用户以为整个项目要配 key 才能跑
    assert "可选" in text
