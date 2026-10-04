"""目标解析（定位 + 消歧 + 验证）的测试。

这一层最容易出的错是**「看起来找到了，其实是错的」**——实测踩过两次：

1. 搜「陈粒深圳场」返回 7 条，陈粒在贵阳/三亚/广州/临沂，**没有一条深圳**；
2. 城市筛完剩 1 条「Jordan Chan ... In Shenzhen」——**城市对，演出名不对**。

所以核心断言只有一条：**宁可不许给错**。找不到要说找不到，
搜到但不对要说清哪里不对，绝不能把一条错的当答案交出去。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import yaml

from radar.intent import parse_intent
from radar.target import (
    Target,
    _city_matches,
    _normalize_city,
    resolve,
    to_yaml_fragment,
    verify,
)

# --- 城市归一化 -------------------------------------------------------------


def test_normalize_keeps_chinese_characters():
    """踩过的坑：只保留 [a-z] 会把「广州」清成空串，于是猫眼返回的
    「广州」被判成「不在广州」，筛选静默地一个都匹配不上、还不报错。"""
    assert _normalize_city("广州") == "广州"
    assert _normalize_city("香港") == "香港"


def test_normalize_strips_punctuation_and_case():
    assert _normalize_city("Shenzhen, CN") == "shenzhencn"
    assert _normalize_city("HongKong, CN") == "hongkongcn"


@pytest.mark.parametrize(
    ("target_city", "want", "expected"),
    [
        ("广州", "广州", True),            # 猫眼：中文城市名
        ("shenzhencn", "深圳", True),      # 摩天轮：英文地名
        ("hongkongcn", "香港", True),
        ("guangzhou", "广州", True),
        ("shenzhencn", "广州", False),     # 城市不对
        ("", "深圳", False),               # 没有地点信息 = 不匹配，不猜
    ],
)
def test_city_matching(target_city: str, want: str, expected: bool):
    assert _city_matches(target_city, want) is expected


def test_empty_wanted_city_matches_everything():
    """用户没说城市时不该拦——那是用户的自由。"""
    assert _city_matches("anything", "") is True


# --- 策略选择：能不用搜就不用 -----------------------------------------------


def _resolve(text: str, **kw):
    intent = parse_intent(text)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 200, "data": []}))
        ) as client:
            return await resolve(client, intent, **kw)

    return asyncio.run(go())


def test_link_with_id_skips_search():
    """链接里已有 id 就不搜——搜索要花配额，也可能撞限流。"""
    result = _resolve("https://detail.damai.cn/item.htm?id=1076797433026")
    assert result.strategy == "direct"
    assert len(result.targets) == 1
    assert result.targets[0].params == {"item_id": "1076797433026"}


def test_rail_route_skips_search():
    """铁路靠线路 + 日期定位，不该去搜演出平台。"""
    result = _resolve("广州南到长沙南的高铁")
    assert result.strategy == "route"
    assert result.targets[0].params == {"from": "广州南", "to": "长沙南"}
    assert result.targets[0].platform == "rail12306"


def test_rail_route_keeps_date():
    result = _resolve("明天广州到长沙的票")
    assert result.targets[0].params.get("date") == "+1"


def test_nothing_to_go_on_says_so():
    """既没 id、没线路、也没关键词时要明说，不能返回空列表装完成。"""
    result = _resolve("随便看看", need_search=False)
    assert result.targets == []
    assert result.notes, "要说清楚为什么定位不了"


# --- 消歧 -------------------------------------------------------------------

MAOYAN_ROWS = [
    {"performanceId": "502581", "name": "陈粒「一粒」十周年巡回演唱会-贵阳站",
     "cityName": "贵阳", "showTimeRange": "2026.11.07", "priceRange": "399-999", "ticketStatus": 2},
    {"performanceId": "501675", "name": "陈粒「一粒」十周年巡回演唱会-广州站",
     "cityName": "广州", "showTimeRange": "2026.10.17", "priceRange": "399-999", "ticketStatus": 3},
    {"performanceId": "498813", "name": "泸州酒要会·超级银河左岸", "cityName": "泸州",
     "showTimeRange": "2026.10.05", "priceRange": "299-698", "ticketStatus": 3},
]


def _resolve_with(rows: list[dict], text: str, **kw):
    """用假搜索结果跑一次 resolve。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "myshow" in str(request.url):
            return httpx.Response(200, json={"code": 200, "data": rows})
        return httpx.Response(200, json={"code": 200, "data": []})

    intent = parse_intent(text)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve(client, intent, platforms=["maoyan"], **kw)

    return asyncio.run(go())


def test_city_filter_keeps_only_matching_city():
    """搜「陈粒广州」不该返回贵阳场次。"""
    result = _resolve_with(MAOYAN_ROWS, "陈粒广州站")
    assert [t.target_id for t in result.targets] == ["501675"]


def test_name_filter_rejects_wrong_show():
    """**最关键的一条**：城市对但演出名不对的，必须被拒。

    实测踩过：搜「陈粒深圳场」，城市筛完剩下「Jordan Chan ... Shenzhen」，
    城市完全正确、演出完全错误。当答案交出去 = 用户建了个盯错对象的监控，
    而且要等下一次变化才会发现盯错了。
    """
    rows = [
        {"performanceId": "999", "name": "陈粒深圳演唱会", "cityName": "深圳",
         "showTimeRange": "2026.11.01", "priceRange": "399-999", "ticketStatus": 3},
        {"performanceId": "888", "name": "别人在深圳的演出", "cityName": "深圳",
         "showTimeRange": "2026.11.02", "priceRange": "100-200", "ticketStatus": 3},
    ]
    result = _resolve_with(rows, "陈粒深圳场")
    assert [t.target_id for t in result.targets] == ["999"]


def test_nothing_matches_moves_candidates_to_rejected():
    """全都不匹配时不能返回空列表了事——要说搜到了什么、哪里不对。"""
    result = _resolve_with(MAOYAN_ROWS, "陈粒深圳场")
    assert result.targets == []
    assert result.rejected, "搜到过的东西要留着给用户判断"
    assert result.city_miss is True


def test_too_many_candidates_advises_narrowing():
    rows = [
        {"performanceId": str(i), "name": f"陈粒演唱会 {i}", "cityName": "广州",
         "showTimeRange": "2026.10.1", "priceRange": "399-999", "ticketStatus": 3}
        for i in range(20)
    ]
    result = _resolve_with(rows, "陈粒广州")
    assert len(result.targets) > 12
    assert result.outcome() == "too_many"
    assert any("收窄" in n for n in result.notes)


def test_outcome_reports_none_when_nothing_found():
    assert _resolve_with([], "陈粒广州").outcome() == "none"


def test_outcome_reports_unique_for_single_candidate():
    result = _resolve_with(MAOYAN_ROWS, "陈粒广州站")
    assert result.unique is True
    assert result.outcome() == "unique"


# --- 验证 -------------------------------------------------------------------


def test_verify_reports_failure_instead_of_raising():
    """验证失败要返回「不行」+ 原因，不能抛异常——它只是一个答案。"""

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as c:
            return await verify(c, Target(platform="maoyan", params={"performance_id": "1"}))

    ok, note = asyncio.run(go())
    assert ok is False
    assert note


def test_verify_detects_wrong_id_by_empty_result():
    """id 错了但接口通（200 + 空数据）也算失败——那是「盯不到」而不是「没票」。"""

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"code": 200, "data": {"performances": []}})
            )
        ) as c:
            return await verify(c, Target(platform="maoyan", params={"performance_id": "1"}))

    ok, note = asyncio.run(go())
    assert ok is False
    assert note


# --- YAML 输出 --------------------------------------------------------------


def test_yaml_fragment_is_pasteable():
    """输出必须能直接粘进 tasks.yaml——缩进或引号错了就白搭。"""
    fragment = to_yaml_fragment(
        Target(
            platform="maoyan",
            target_id="501675",
            label="陈粒广州站",
            params={"performance_id": "501675"},
        )
    )
    # 片段是 tasks.yaml 里的**一个列表项**，所以 safe_load 出来是 list
    parsed = yaml.safe_load(fragment)
    assert isinstance(parsed, list) and len(parsed) == 1
    entry = parsed[0]
    assert entry["adapter"] == "maoyan"
    assert entry["enabled"] is True
    assert entry["params"]["performance_id"] == "501675"
    assert entry["watch"]["notify_on"] == ["appeared"]


def test_yaml_fragment_sanitises_id():
    """id 里带斜杠空格会生成非法 YAML id。"""
    fragment = to_yaml_fragment(
        Target(platform="x", target_id="a/b c", params={"k": "v"})
    )
    slug = yaml.safe_load(fragment)[0]["id"]
    assert "/" not in slug and " " not in slug, f"id 没被清洗：{slug!r}"
