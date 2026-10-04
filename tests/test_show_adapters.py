"""演出票平台适配器的单测：大麦（含 mtop 签名）+ 纷玩岛（档案）。

签名部分是这个文件里最要紧的——它保证「请求格式正确」，
而不是「我以为正确」。公式一旦被改动，参考值测试会立刻失败。

猫眼和摩天轮在这个文件里**只剩「注册了、不需要凭据」两条断言**：
2026-09-30 实测证明它们的接口是公开的，于是各自有了真适配器，
细节测试分别搬去了 ``tests/test_maoyan.py`` 和 ``tests/test_moretickets.py``。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from radar.adapters import available_adapters, create_adapter
from radar.adapters.damai import (
    DEFAULT_APP_KEY,
    DamaiAdapter,
    is_token_expired,
    mtop_sign,
    parse_damai_payload,
    parse_mtop_token,
)
from radar.adapters.shows import FenWanDaoAdapter, ShowPlatformAdapter
from radar.config import TaskConfig

#: 固定参考值：md5("tok123&1700000000000&23739456&{\"itemId\":\"700000\"}")
#: 用于锁定签名公式。改动公式前请先想清楚为什么。
REFERENCE_SIGN = "5a815a22e12abc6082ce12862b438ccd"

DAMAI_PAYLOAD = {
    "api": "mtop.damai.wireless.project.getprojectdetail",
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "result": {
            "projectName": "示例演唱会",
            "performBases": [
                {
                    "performId": "P1",
                    "performName": "2026-10-01 周五 19:30",
                    "performTime": "2026-10-01 19:30",
                },
                {
                    "performId": "P2",
                    "performName": "2026-10-02 周六 19:30",
                    "performTime": "2026-10-02 19:30",
                },
            ],
            "skuList": [
                {"skuId": "S1", "performId": "P1", "priceName": "380元看台", "status": 1},
                {"skuId": "S2", "performId": "P1", "priceName": "580元内场", "status": 0},
                {"skuId": "S3", "performId": "P2", "priceName": "380元看台", "status": 1},
            ],
        }
    },
}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_all_platforms_are_registered():
    names = available_adapters()
    for expected in (
        "rail12306",
        "amadeus",
        "damai",
        "maoyan",
        "moretickets",
        "fenwandao",
        "json-api",
    ):
        assert expected in names, f"适配器 {expected} 没有注册"


def test_every_adapter_respects_interval_floor():
    for name in available_adapters():
        assert create_adapter(name).min_interval >= 60


# ---------------------------------------------------------------------------
# mtop 签名
# ---------------------------------------------------------------------------


def test_parse_mtop_token():
    assert parse_mtop_token("a=1; _m_h5_tk=abc123_1699999999999; b=2") == "abc123"
    assert parse_mtop_token("_m_h5_tk=onlytoken") == "onlytoken"
    assert parse_mtop_token("") == ""
    assert parse_mtop_token("other=1") == ""


def test_mtop_sign_matches_reference_value():
    assert mtop_sign("tok123", 1700000000000, "23739456", '{"itemId":"700000"}') == REFERENCE_SIGN


def test_mtop_sign_is_order_and_space_sensitive():
    """data 的序列化方式会影响签名——这正是我们要固定 separators 的原因。"""
    compact = mtop_sign("t", 1, "k", '{"a":1}')
    spaced = mtop_sign("t", 1, "k", '{"a": 1}')
    assert compact != spaced


@pytest.mark.parametrize(
    "payload",
    [
        {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
        {"ret": "FAIL_SYS_TOKEN_EMPTY"},
        {"ret": ["SUCCESS::调用成功", "FAIL_SYS_TOKEN_EXOIRED"]},
    ],
)
def test_is_token_expired_true(payload):
    assert is_token_expired(payload) is True


@pytest.mark.parametrize("payload", [{"ret": ["SUCCESS::调用成功"]}, {}, None, "oops"])
def test_is_token_expired_false(payload):
    assert is_token_expired(payload) is False


# ---------------------------------------------------------------------------
# 大麦：请求与解析
# ---------------------------------------------------------------------------


def _damai_task(**params) -> TaskConfig:
    return TaskConfig(
        id="damai1", adapter="damai", interval_seconds=600, params=params
    )


def test_damai_request_is_correctly_signed():
    """抓下真实发出的请求，按同样的公式重算签名并对齐。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=DAMAI_PAYLOAD)

    adapter = create_adapter("damai", {"cookie": "_m_h5_tk=seekret_1700000000000"})
    task = _damai_task(item_id="700000")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    assert len(captured) == 1

    query = dict(httpx.URL(str(captured[0].url)).params)
    # 2026-09-30 实测纠正：旧的 getprojectdetail 已返回 API_NOT_FOUNDED，
    # 23739456 也不是 H5 端的 appKey。这两个默认值写错过一次，锁在这里防复发。
    assert query["api"] == "mtop.damai.item.detail.getdetail"
    assert query["appKey"] == "12574478"
    assert query["data"] == '{"itemId":"700000"}'
    assert query["sign"] == mtop_sign(
        "seekret", int(query["t"]), "12574478", query["data"]
    )
    assert captured[0].headers["cookie"].startswith("_m_h5_tk=")

    # 顺便验证解析结果
    assert set(snapshot.trains) == {"P1", "P2"}
    assert snapshot.trains["P1"].seats["380元看台"].available is True
    assert snapshot.trains["P1"].seats["580元内场"].available is False
    assert snapshot.trains["P2"].depart_time == "2026-10-02 19:30"
    assert snapshot.platform == "damai"


def test_damai_without_cookie_explains_how_to_get_it():
    from radar.adapters.base import AdapterError

    adapter = create_adapter("damai", {})
    task = _damai_task(item_id="1")

    with pytest.raises(AdapterError) as exc:
        asyncio.run(adapter.build_request(task, None))

    message = str(exc.value)
    assert "_m_h5_tk" in message
    assert "DAMAI_COOKIE" in message


def test_damai_refreshes_expired_token_once():
    """token 过期时从响应 Cookie 取新 token 重签一次，而不是直接失败。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(
                200,
                json={"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
                headers={"set-cookie": "_m_h5_tk=freshtok_1700000000001; Path=/"},
            )
        return httpx.Response(200, json=DAMAI_PAYLOAD)

    adapter = create_adapter("damai", {"cookie": "_m_h5_tk=staletok_1"})
    task = _damai_task(item_id="1")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch_raw(task, client)

    data = asyncio.run(go())
    assert data["ret"] == ["SUCCESS::调用成功"]
    assert len(captured) == 2

    second = dict(httpx.URL(str(captured[1].url)).params)
    # 这个用例关心的是「换用新 token 重签」，appKey 用当前默认值即可
    assert second["sign"] == mtop_sign(
        "freshtok", int(second["t"]), DEFAULT_APP_KEY, second["data"]
    )


def test_parse_damai_payload_groups_sku_by_perform():
    trains = parse_damai_payload(DAMAI_PAYLOAD)
    assert set(trains) == {"P1", "P2"}
    assert set(trains["P1"].seats) == {"380元看台", "580元内场"}
    assert set(trains["P2"].seats) == {"380元看台"}


def test_parse_damai_payload_keeps_more_available_duplicate():
    payload = {
        "data": {
            "result": {
                "skuList": [
                    {"performId": "P1", "priceName": "看台", "status": 0},
                    {"performId": "P1", "priceName": "看台", "status": 4},
                ]
            }
        }
    }
    trains = parse_damai_payload(payload)
    assert trains["P1"].seats["看台"].count == 4


def test_parse_damai_payload_falls_back_to_single_bucket():
    """没有 performBases、票档也没带 performId 时，收拢成一个单元而不是丢弃。"""
    payload = {"data": {"result": {"skuList": [{"priceName": "看台", "status": 1}]}}}
    trains = parse_damai_payload(payload)
    assert list(trains) == ["ALL"]
    assert trains["ALL"].seats["看台"].available is True


def test_parse_damai_payload_reports_shape_when_no_result():
    from radar.adapters.base import AdapterError

    with pytest.raises(AdapterError) as exc:
        parse_damai_payload({"ret": ["FAIL_BIZ_XXX"], "data": {}})
    assert "FAIL_BIZ_XXX" in str(exc.value) or "data.result" in str(exc.value)


def test_damai_http_error_mentions_no_bypass():
    from radar.adapters.base import AdapterError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    adapter = create_adapter("damai", {"cookie": "_m_h5_tk=tok_1"})
    task = _damai_task(item_id="1")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch_raw(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "不要尝试绕过" in str(exc.value)


# ---------------------------------------------------------------------------
# 纷玩岛：档案完整性（猫眼/摩天轮已各自独立成文件）
# ---------------------------------------------------------------------------


def test_fenwandao_is_a_show_platform():
    assert issubclass(FenWanDaoAdapter, ShowPlatformAdapter)
    instance = FenWanDaoAdapter()
    # 纷玩岛确实需要登录（真·接不了），这一条与「公开接口」的两个平台相反
    assert instance.requires_credentials is True
    assert instance.min_interval >= 60
    # 档案必须给出票档名与状态的候选路径，否则用户无从下手
    assert instance.defaults["seat_name"]
    assert instance.defaults["seat_status"]
    assert instance.find_url_hint


def test_fenwandao_hint_says_it_is_blocked_not_unfinished():
    """「没有网页端」和「还没写」是两件事，提示里必须说清是前者。"""
    hint = FenWanDaoAdapter().find_url_hint
    assert "没有网页端" in hint
    # 小程序签名逆向是红线，提示里要明确不做，免得用户以为是我们偷懒
    assert "签名逆向" in hint


@pytest.mark.parametrize("name", ["maoyan", "moretickets"])
def test_public_show_platforms_do_not_require_credentials(name):
    """实测结论锁在这里：这两家匿名可用。写错成 True 会让用户白折腾一轮取 Cookie。"""
    adapter = create_adapter(name)
    assert adapter.requires_credentials is False
    assert adapter.base_url


def test_damai_shares_the_json_api_engine():
    """大麦只加了签名这一层，其余（解析兜底、模板、报错）都来自通用引擎。"""
    from radar.adapters.json_api import JsonApiAdapter

    assert issubclass(DamaiAdapter, JsonApiAdapter)
    # 同一套 fail-safe：结构不认识时要能退回通用解析，而不是直接崩
    assert callable(DamaiAdapter.parse_payload)
