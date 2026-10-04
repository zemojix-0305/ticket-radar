"""通用 JSON 适配器与路径工具的单测。

这里覆盖的是「加平台不用写代码」这条承诺的地基：路径解析、状态归一、
模板展开。它们错了，所有基于配置的平台都会错。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from radar.adapters.json_api import (
    MISSING,
    coerce_availability,
    dig,
    first_of,
    render_template,
    shape,
    split_path,
    template_mapping,
)
from radar.adapters.registry import create_adapter
from radar.config import TaskConfig

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def test_split_path_accepts_bracket_and_dot_forms():
    assert split_path("data.list") == ["data", "list"]
    assert split_path("data[0].name") == ["data", "0", "name"]
    assert split_path("data.0.name") == ["data", "0", "name"]
    assert split_path("$") == []


def test_dig_walks_dict_and_list():
    payload = {"data": {"result": [{"name": "a"}, {"name": "b"}]}}
    assert dig(payload, "data.result.1.name") == "b"
    assert dig(payload, "data.result[0].name") == "a"
    assert dig(payload, "$") is payload


def test_dig_returns_sentinel_for_missing_path():
    payload = {"data": {}}
    assert dig(payload, "data.nope") is MISSING
    assert dig(payload, "data.list.0") is MISSING
    # 区分「取到了 None」和「压根没这个键」
    assert dig({"a": None}, "a") is None


def test_first_of_skips_empty_and_accepts_pipe_syntax():
    item = {"a": "", "b": None, "c": "hit"}
    assert first_of(item, ["a", "b", "c"]) == "hit"
    assert first_of(item, "a|b|c") == "hit"
    assert first_of(item, "x.y", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# 状态归一
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "count", "available"),
    [
        (5, 5, True),
        ("5", 5, True),
        (0, 0, False),
        ("0", 0, False),
        (True, None, True),
        (False, 0, False),
        ("有", None, True),
        ("充足", None, True),
        ("可购买", None, True),
        ("售罄", 0, False),
        ("缺货", 0, False),
        ("无票", 0, False),
        ("false", 0, False),
    ],
)
def test_coerce_availability_known_values(raw, count, available):
    seat = coerce_availability("看台", raw)
    assert seat is not None
    assert seat.count == count
    assert seat.available is available
    assert seat.seat_type == "看台"


@pytest.mark.parametrize("raw", ["未知状态", "敬请期待", -1, None, ""])
def test_coerce_availability_skips_unknown_values(raw):
    """看不懂就跳过——宁可漏报也不能误报。"""
    assert coerce_availability("看台", raw) is None


def test_coerce_availability_accepts_custom_vocabulary():
    seat = coerce_availability("看台", "HOT", available_values=["hot"])
    assert seat is not None and seat.available is True
    seat = coerce_availability("看台", "GONE", sold_out_values=["gone"])
    assert seat is not None and seat.available is False


def test_bool_is_not_treated_as_int():
    """True 在 Python 里也是 int，顺序写反了 False 会被当成「0 张有效票」。"""
    seat = coerce_availability("x", False)
    assert seat.available is False and seat.count == 0


# ---------------------------------------------------------------------------
# 模板与占位符命名空间
# ---------------------------------------------------------------------------


def test_render_template_expands_and_tolerates_missing_keys():
    assert render_template("/items/{item_id}/sku", {"item_id": 42}) == "/items/42/sku"
    assert render_template("/items/{nope}/sku", {}) == "/items//sku"
    assert render_template("plain", {}) == "plain"


def test_template_mapping_url_vars_win_over_flat_params():
    """`item_id` 这种名字在两边都有含义，url_vars 必须优先。"""
    params = {"item_id": [1, 2], "url_vars": {"item_id": "8888"}}
    mapping = template_mapping(params, {})
    assert mapping["item_id"] == "8888"


def test_template_mapping_provides_time_helpers():
    mapping = template_mapping({}, {})
    assert len(str(mapping["today"])) == 10
    assert isinstance(mapping["timestamp_ms"], int)


def test_shape_prints_structure_not_payload():
    rendered = shape({"data": {"list": [{"a": 1}]}}, depth=3)
    assert "data:" in rendered
    assert "list(1)" in rendered


# ---------------------------------------------------------------------------
# 适配器端到端（MockTransport，不联网）
# ---------------------------------------------------------------------------


def _task(**params) -> TaskConfig:
    return TaskConfig(id="t1", adapter="json-api", interval_seconds=600, params=params)


def _run_with(payload, task: TaskConfig, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.last_request = request
        return httpx.Response(status, json=payload)

    handler.last_request = None
    adapter = create_adapter("json-api")
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    return snapshot, handler.last_request


def test_json_api_end_to_end():
    payload = {
        "data": {
            "list": [
                {
                    "skuId": "1001",
                    "name": "380元看台",
                    "showTime": "2026-10-01 19:30",
                    "status": 3,
                },
                {"skuId": "1002", "name": "580元内场", "status": "售罄"},
            ]
        }
    }
    task = _task(
        url="https://api.example.com/skus",
        items_path="data.list",
        item_id=["skuId"],
        item_label=["name"],
        item_fields={"depart_time": "showTime"},
        seats_path="",
        seat_name=["name"],
        seat_status=["status"],
    )
    snapshot, request = _run_with(payload, task)

    assert request.headers["user-agent"].startswith("Mozilla")
    assert set(snapshot.trains) == {"1001", "1002"}
    assert snapshot.trains["1001"].depart_time == "2026-10-01 19:30"
    assert snapshot.trains["1001"].seats["380元看台"].count == 3
    assert snapshot.trains["1002"].seats["580元内场"].available is False
    assert snapshot.platform == "json-api"


def test_json_api_merges_multiple_rows_of_same_id():
    """同一场次按票档打平返回时，要归并成一个单元而不是互相覆盖。"""
    payload = {
        "data": [
            {"sessionId": "S1", "priceName": "看台", "stock": 5},
            {"sessionId": "S1", "priceName": "内场", "stock": 2},
        ]
    }
    task = _task(
        url="https://api.example.com/x",
        items_path="data",
        item_id=["sessionId"],
        seat_name=["priceName"],
        seat_status=["stock"],
    )
    snapshot, _ = _run_with(payload, task)

    assert list(snapshot.trains) == ["S1"]
    assert set(snapshot.trains["S1"].seats) == {"看台", "内场"}
    assert snapshot.trains["S1"].seats["内场"].count == 2


def test_json_api_expands_url_placeholders_and_query():
    payload = {"data": [{"id": "1", "name": "x", "status": 1}]}
    task = _task(
        url="https://api.example.com/items/{item_id}/skus",
        url_vars={"item_id": "700000"},
        query={"date": "+7"},
        headers={"Referer": "https://example.com/"},
        items_path="data",
        item_id=["id"],
        seats_path="",
        seat_name=["name"],
        seat_status=["status"],
    )
    _, request = _run_with(payload, task)

    assert str(request.url).startswith("https://api.example.com/items/700000/skus")
    assert "date=" in str(request.url)
    assert request.headers["referer"] == "https://example.com/"


def test_json_api_posts_json_body():
    payload = {"data": [{"id": "1", "name": "x", "status": 1}]}
    task = _task(
        url="https://api.example.com/x",
        method="POST",
        json_body={"pageSize": 50},
        items_path="data",
        item_id=["id"],
        seats_path="",
        seat_name=["name"],
        seat_status=["status"],
    )
    _, request = _run_with(payload, task)

    assert request.method == "POST"
    assert json.loads(request.content) == {"pageSize": 50}


def test_json_api_error_message_contains_structure_hint():
    """解析失败时必须把真实结构打出来，否则用户只能干瞪眼。"""
    from radar.adapters.base import AdapterError

    payload = {"code": 0, "result": {"items": []}}
    task = _task(
        url="https://api.example.com/x",
        items_path="data.list",
        item_id=["id"],
        seat_name=["name"],
        seat_status=["status"],
    )
    with pytest.raises(AdapterError) as exc:
        _run_with(payload, task)

    message = str(exc.value)
    assert "items_path" in message
    assert "result:" in message  # 结构树里应该出现真实的键


def test_json_api_requires_url():
    from radar.adapters.base import AdapterError

    with pytest.raises(AdapterError) as exc:
        _run_with({"data": []}, _task(items_path="data"))
    assert "params.url" in str(exc.value)


def test_json_api_reports_non_json_response():
    from radar.adapters.base import AdapterError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    adapter = create_adapter("json-api")
    task = _task(url="https://api.example.com/x", items_path="data")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "不是 JSON" in str(exc.value)


def test_json_api_surfaces_auth_failure_hint():
    from radar.adapters.base import AdapterError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    adapter = create_adapter("json-api")
    task = _task(url="https://api.example.com/x", items_path="data")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "403" in str(exc.value)
    assert "不要尝试绕过" in str(exc.value)
