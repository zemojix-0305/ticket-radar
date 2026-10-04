"""摩天轮票务适配器的单测。

锁三件事：

1. **必须 POST**。同一个地址用 GET 会拿到一个 111 字节的 ``statusCode=12123``
   空壳——那是「姿势不对」，不是平台故障，不锁住很容易被误当成风控。
2. ``hasTicket`` 布尔值是唯一的余票判据；缺了就跳过该场次，不猜。
3. 二手票平台的性质：只有部分城市挂得上票，「某站没挂单」是常态不是 bug。

全部走 ``httpx.MockTransport``，不联网。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from radar.adapters.base import AdapterError
from radar.adapters.moretickets import (
    PUBLIC,
    SEAT_NAME,
    UNIFY_BASE,
    MoreTicketsAdapter,
    list_path,
    parse_search_items,
    parse_sessions,
    search_path,
    search_tours,
    session_path,
    unwrap,
)
from radar.config import TaskConfig


def _sessions_payload(groups: list[dict] | None = None) -> dict:
    if groups is None:
        groups = [
            {
                "regionCode": "GZ",
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s-gz-1018",
                        "sessionName": "2026-10-18 19:30",
                        "showName": "周杰伦嘉年华",
                        "venueName": "广州体育馆",
                        "cityName": "广州",
                        "hasTicket": True,
                        "sessionStatusDesc": "",
                        "price": {"minSalePrice": 1280},
                    }
                ],
            }
        ]
    return {"statusCode": 200, "message": "ok", "data": {"sessionGroupList": groups}}


# ---------------------------------------------------------------------------
# 路径 / 信封
# ---------------------------------------------------------------------------


def test_search_path_has_no_pub_prefix():
    """搜索接口是个例外：它**没有** pub 段，却同样匿名可用。别顺手加上去。"""
    assert search_path() == "/show/search/v1"
    assert PUBLIC not in search_path()


def test_public_paths_carry_the_prefix():
    assert list_path().startswith(PUBLIC)
    assert session_path().startswith(PUBLIC)


def test_unwrap_surfaces_the_12123_shell():
    """HTTP 200 也可能是「你请求的姿势不对」，只有 statusCode 会说真话。"""
    with pytest.raises(AdapterError) as exc:
        unwrap(
            {"statusCode": 12123, "message": "An unknown error has occurred"},
            what="场次",
        )
    message = str(exc.value)
    assert "12123" in message
    # 报错要把最可能的成因说出来，不然下次还得重新推
    assert "POST" in message


def test_unwrap_tolerates_a_successful_envelope():
    assert unwrap(_sessions_payload(), what="场次")["statusCode"] == 200


# ---------------------------------------------------------------------------
# 解析场次
# ---------------------------------------------------------------------------


def test_parse_sessions_reads_has_ticket_boolean():
    trains = parse_sessions(_sessions_payload())
    assert list(trains) == ["广州 2026-10-18 19:30"]
    train = trains["广州 2026-10-18 19:30"]
    seat = train.seats[SEAT_NAME]
    assert seat.available is True
    assert seat.count is None, "有票但张数未知，不能写成 0"
    assert seat.price == 1280
    assert train.to_station == "广州体育馆"
    assert train.extra["session_id"] == "s-gz-1018"


def test_parse_sessions_sold_out_has_count_zero():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "广州",
                        "hasTicket": False,
                        "sessionStatusDesc": "Sold Out",
                    }
                ],
            }
        ]
    )
    seat = parse_sessions(payload)["广州 2026-10-18 19:30"].seats[SEAT_NAME]
    assert seat.available is False
    assert seat.count == 0
    # 原文照搬：站点自己写的就是 "Sold Out"，它还有中/繁两套文案
    assert seat.raw == "Sold Out"


def test_parse_sessions_skips_unreadable_rows_instead_of_guessing():
    """hasTicket 缺失、文案也看不懂 → 跳过。读不到就当没这一场，宁可漏报。"""
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "广州",
                        "sessionStatusDesc": "待定",
                    }
                ],
            }
        ]
    )
    assert parse_sessions(payload) == {}


def test_parse_sessions_falls_back_to_sold_out_text_when_flag_missing():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "广州",
                        "sessionStatusDesc": "售罄",
                    }
                ],
            }
        ]
    )
    seat = parse_sessions(payload)["广州 2026-10-18 19:30"].seats[SEAT_NAME]
    assert seat.available is False


def test_parse_sessions_key_includes_city():
    """巡演里两地同点开场是常态，只用时刻当键会把两场并成一场。"""
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "a",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "广州",
                        "hasTicket": True,
                    }
                ],
            },
            {
                "regionName": "深圳",
                "sessionList": [
                    {
                        "sessionId": "b",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "深圳",
                        "hasTicket": True,
                    }
                ],
            },
        ]
    )
    assert set(parse_sessions(payload)) == {"广州 2026-10-18 19:30", "深圳 2026-10-18 19:30"}


def test_parse_sessions_city_filter_keeps_only_that_city():
    """巡演横跨多城，不过滤的话每个城市售罄都推一条，纯噪音。"""
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [{"sessionId": "a", "sessionName": "10-18", "cityName": "广州", "hasTicket": True}],
            },
            {
                "regionName": "深圳",
                "sessionList": [{"sessionId": "b", "sessionName": "10-19", "cityName": "深圳", "hasTicket": True}],
            },
        ]
    )
    trains = parse_sessions(payload, city="广州")
    assert list(trains) == ["广州 10-18"]


def test_key_prefers_region_name_over_country():
    """实测字段含义：regionName = "Guangzhou, CN"（真实城市），
    cityName = "China"（国家级）。

    只用 cityName 会让整个中国巡演挤在一个 "China" 下面，场次键互相覆盖，
    于是「广州 10-18 有票」和「三亚 11-07 有票」被当成同一条记录。
    """
    payload = _sessions_payload(
        [
            {
                "regionCode": "CN-GD-01",
                "regionName": "Guangzhou, CN",
                "sessionList": [
                    {
                        "sessionId": "a",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "China",
                        "hasTicket": True,
                    }
                ],
            },
            {
                "regionCode": "CN-HI-02",
                "regionName": "Sanya, CN",
                "sessionList": [
                    {
                        "sessionId": "b",
                        "sessionName": "2026-10-18 19:30",
                        "cityName": "China",
                        "hasTicket": True,
                    }
                ],
            },
        ]
    )
    trains = parse_sessions(payload)
    assert set(trains) == {"Guangzhou, CN 2026-10-18 19:30", "Sanya, CN 2026-10-18 19:30"}


def test_city_filter_matches_region_name():
    """用户过滤时说的是城市（广州），而 cityName 给的是国家（China）。"""
    payload = _sessions_payload(
        [
            {
                "regionName": "Guangzhou, CN",
                "sessionList": [
                    {"sessionId": "a", "sessionName": "10-18", "cityName": "China", "hasTicket": True}
                ],
            },
            {
                "regionName": "Melbourne, AU",
                "sessionList": [
                    {"sessionId": "b", "sessionName": "10-17", "cityName": "Australia", "hasTicket": True}
                ],
            },
        ]
    )
    trains = parse_sessions(payload, city="Guangzhou")
    assert list(trains) == ["Guangzhou, CN 10-18"]


def test_price_keeps_the_platform_currency():
    """境外场次挂单价是 HK$。渲染成「¥3499」等于报错价，用户可能据此下单。"""
    payload = _sessions_payload(
        [
            {
                "regionName": "Melbourne, AU",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "10-17",
                        "cityName": "Australia",
                        "hasTicket": True,
                        "currencySymbol": "HK$",
                        "price": {"minSalePrice": "3499"},
                    }
                ],
            }
        ]
    )
    seat = parse_sessions(payload)["Melbourne, AU 10-17"].seats[SEAT_NAME]
    assert seat.price == 3499
    assert seat.currency == "HK$"
    assert seat.price_text == "HK$3499"


def test_price_defaults_to_renminbi_when_platform_omits_it():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "10-18",
                        "cityName": "China",
                        "hasTicket": True,
                        "price": {"minSalePrice": "399"},
                    }
                ],
            }
        ]
    )
    seat = parse_sessions(payload)["广州 10-18"].seats[SEAT_NAME]
    assert seat.currency == "¥"
    assert seat.price_text == "¥399"


def test_parse_sessions_reports_shape_change():
    """结构变了要报出来，而不是静默返回空——空会被误读成「没票」。"""
    with pytest.raises(AdapterError) as exc:
        parse_sessions({"statusCode": 200, "data": {"groups": []}})
    assert "sessionGroupList" in str(exc.value)


def test_sessions_without_id_or_time_are_skipped():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {"sessionId": "", "sessionName": "10-18", "cityName": "广州", "hasTicket": True},
                    {"sessionId": "s2", "sessionName": "", "cityName": "广州", "hasTicket": True},
                ],
            }
        ]
    )
    assert parse_sessions(payload) == {}


def test_price_is_optional_and_never_breaks_parsing():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [
                    {
                        "sessionId": "s1",
                        "sessionName": "10-18",
                        "cityName": "广州",
                        "hasTicket": True,
                        "price": {"minSalePrice": "not-a-number"},
                    }
                ],
            }
        ]
    )
    seat = parse_sessions(payload)["广州 10-18"].seats[SEAT_NAME]
    assert seat.available is True
    assert seat.price is None


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def test_parse_search_items_requires_tour_id():
    rows = parse_search_items(
        {"statusCode": 200, "data": [{"tourId": "t1", "title": "a"}, {"title": "无 id"}, "oops"]}
    )
    assert rows == [{"tourId": "t1", "title": "a"}]


def test_search_tours_is_usable_without_a_task():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"statusCode": 200, "data": [{"tourId": "t1", "title": "Jay Chou"}]}
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_tours(client, "Jay Chou")

    rows = asyncio.run(go())
    assert rows and rows[0]["tourId"] == "t1"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_posts_json_to_session_endpoint():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_sessions_payload())

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt1", adapter="moretickets", interval_seconds=900, params={"tour_id": "t1"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST", "GET 会拿到 12123 空壳，必须 POST"
    url = str(request.url)
    assert url.startswith(UNIFY_BASE)
    assert url.endswith("/pub/session/city/list/v1")
    assert b'"tourId"' in request.content
    assert snapshot.platform == "moretickets"
    assert snapshot.context["演出"] == "周杰伦嘉年华"


def test_fetch_with_keyword_resolves_tour_id_first():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "search" in url:
            return httpx.Response(
                200, json={"statusCode": 200, "data": [{"tourId": "t9", "title": "Jay"}]}
            )
        return httpx.Response(200, json=_sessions_payload())

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt2", adapter="moretickets", interval_seconds=900, params={"keyword": "Jay Chou"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    asyncio.run(go())
    assert len(seen) == 2
    assert "search" in seen[0]


def test_fetch_applies_city_filter_from_params():
    payload = _sessions_payload(
        [
            {
                "regionName": "广州",
                "sessionList": [{"sessionId": "a", "sessionName": "10-18", "cityName": "广州", "hasTicket": True}],
            },
            {
                "regionName": "深圳",
                "sessionList": [{"sessionId": "b", "sessionName": "10-19", "cityName": "深圳", "hasTicket": True}],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt3",
        adapter="moretickets",
        interval_seconds=900,
        params={"tour_id": "t1", "city": "广州"},
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    assert list(snapshot.trains) == ["广州 10-18"]


def test_fetch_without_tour_id_or_keyword_explains_both_ways():
    adapter = MoreTicketsAdapter()
    task = TaskConfig(id="mt4", adapter="moretickets", interval_seconds=900, params={})

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    message = str(exc.value)
    assert "tour_id" in message
    assert "radar find moretickets" in message


def test_search_miss_explains_secondhand_nature():
    """搜不到 ≠ 配置错。二手平台没挂单是常态，报错里要说出来，否则用户会反复调。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"statusCode": 200, "data": []})

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt5", adapter="moretickets", interval_seconds=900, params={"keyword": "冷门"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "二手" in str(exc.value)


def test_no_sessions_yields_empty_snapshot_not_an_error():
    """整站没挂单是合法的（也是常态）。空快照 ≠ 抓取失败——两者在引擎里语义不同。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"statusCode": 200, "data": {"sessionGroupList": []}})

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt6", adapter="moretickets", interval_seconds=900, params={"tour_id": "t1"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    assert snapshot.trains == {}
    assert snapshot.platform == "moretickets"


def test_http_error_tells_user_not_to_bypass():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="blocked")

    adapter = MoreTicketsAdapter()
    task = TaskConfig(
        id="mt7", adapter="moretickets", interval_seconds=900, params={"tour_id": "t1"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "不要尝试绕过" in str(exc.value)
