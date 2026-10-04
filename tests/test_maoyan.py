"""猫眼演出适配器的单测。

这个文件锁的是**实测得到的事实**，不是设想的行为：

* 接口是公开的，一个 Cookie 都不需要；
* 状态码有官方词表（1 即将开售 … 4 已售罄），看不懂就报错、不猜；
* 列表和详情的状态会打架，详情才是权威——这条最要紧，读错会让整条提醒链失效。

全部走 ``httpx.MockTransport``，不联网。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from radar.adapters.base import AdapterError
from radar.adapters.maoyan import (
    AVAILABLE_LABELS,
    MYSHOW_BASE,
    SEAT_NAME,
    TICKET_STATUS_LABELS,
    MaoyanAdapter,
    build_detail_path,
    build_search_path,
    parse_detail,
    parse_search_items,
    search_performances,
    status_label,
    unwrap,
)
from radar.config import TaskConfig


def _detail_payload(status: int = 3, **overrides) -> dict:
    row = {
        "performanceId": 501675,
        "name": "陈粒「一粒」十周年巡回演唱会-广州站",
        "cityName": "广州",
        "shopName": "广州体育馆",
        "showTimeRange": "2026.10.18",
        "priceRange": "399-999",
        "ticketStatus": status,
        "stockOutRegister": True,
    }
    row.update(overrides)
    return {"code": 200, "msg": "ok", "data": row}


# ---------------------------------------------------------------------------
# 状态词表
# ---------------------------------------------------------------------------


def test_status_labels_are_the_official_ones():
    """词表是从站点 JS 里挖出来的官方文案。改动它等于改判据，想清楚再动。"""
    assert TICKET_STATUS_LABELS[1] == "即将开售"
    assert TICKET_STATUS_LABELS[2] == "预售"
    assert TICKET_STATUS_LABELS[3] == "在售中"
    assert TICKET_STATUS_LABELS[4] == "已售罄"
    assert TICKET_STATUS_LABELS[5] == "已结束"


@pytest.mark.parametrize("raw,expected", [(3, "在售中"), (2, "预售"), (4, "已售罄"), (1, "即将开售")])
def test_status_label_translates(raw, expected):
    assert status_label(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "oops", 99, True, False])
def test_status_label_refuses_to_guess(raw):
    """看不懂就返回 None——猜一个状态比说不知道危险得多。"""
    assert status_label(raw) is None


def test_upcoming_sale_counts_as_not_available():
    """「即将开售」必须划到买不到。

    这样它开售的那一刻会产生一次 appeared 事件——正是用户最想收到的那条。
    划反了，开售当天反而一声不吭。
    """
    assert "即将开售" not in AVAILABLE_LABELS
    seat = parse_detail(_detail_payload(1)).seats[SEAT_NAME]
    assert seat.available is False
    assert seat.raw == "即将开售"


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------


def test_search_path_puts_params_in_the_path_segment():
    """参数是塞在路径段里的（``;k=xx;p=1``），不是查询串——这是网关自己的写法。"""
    path = build_search_path("陈粒", city_id=10)
    assert path.startswith("/ajax/performances/0;st=0;k=")
    assert ";p=1;s=20;tft=0" in path
    assert path.endswith("?cityId=10&sellChannel=7")
    # 关键词必须被 URL 编码（中文不能裸奔进路径）
    assert "%E9%99%88%E7%B2%92" in path
    assert "陈粒" not in path


def test_search_path_without_keyword_omits_k():
    assert ";k=" not in build_search_path("", city_id=10)


def test_search_path_keeps_semicolons_intact():
    """``;`` 是分隔符不是选项，被编码成 %3B 网关就不认了。"""
    assert "%3B" not in build_search_path("a").upper()


def test_detail_path_carries_sell_channel():
    assert build_detail_path(501675) == "/ajax/performance/501675?sellChannel=7"


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------


def test_unwrap_rejects_non_200_code():
    """网关出错时 HTTP 仍是 200，只有 code 会说真话。不查这一层就会把错误体当数据。"""
    with pytest.raises(AdapterError) as exc:
        unwrap({"code": 500, "msg": "系统繁忙"}, what="详情")
    assert "系统繁忙" in str(exc.value)


def test_unwrap_rejects_non_dict():
    with pytest.raises(AdapterError):
        unwrap(["oops"], what="详情")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_detail_maps_onsale():
    train = parse_detail(_detail_payload(3))
    seat = train.seats[SEAT_NAME]
    assert seat.available is True
    assert seat.raw == "在售中"
    # 有票但张数未知 —— 不能写成 0（0 的意思是没票，模型层对此有硬约束）
    assert seat.count is None


def test_parse_detail_maps_sold_out():
    seat = parse_detail(_detail_payload(4)).seats[SEAT_NAME]
    assert seat.available is False
    assert seat.count == 0


def test_parse_detail_uses_city_as_key_not_numeric_id():
    """通知里直接渲染 train_code，一串数字对人不构成信息。"""
    train = parse_detail(_detail_payload())
    assert train.train_code == "广州"
    assert train.extra["performance_id"] == "501675"


def test_parse_detail_requires_performance_id():
    with pytest.raises(AdapterError) as exc:
        parse_detail({"code": 200, "data": {"name": "x"}})
    assert "performanceId" in str(exc.value)


def test_parse_detail_unknown_status_is_an_error_not_a_guess():
    with pytest.raises(AdapterError) as exc:
        parse_detail(_detail_payload(status=77))
    # 报错里要带上已知映射，后人加状态时知道往哪补
    assert "ticketStatus" in str(exc.value)
    assert "TICKET_STATUS" in str(exc.value) or "映射" in str(exc.value)


def test_parse_search_items_skips_rows_without_id():
    rows = parse_search_items(
        {"code": 200, "data": [{"performanceId": 1}, {"name": "无 id"}, "oops"]}
    )
    assert rows == [{"performanceId": 1}]


def test_parse_search_items_tolerates_empty():
    assert parse_search_items({"code": 200, "data": None}) == []


# ---------------------------------------------------------------------------
# fetch：详情才是权威
# ---------------------------------------------------------------------------


def test_fetch_hits_detail_without_credentials():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_detail_payload(3))

    adapter = MaoyanAdapter()  # 注意：不传任何凭据
    task = TaskConfig(
        id="my1", adapter="maoyan", interval_seconds=600, params={"performance_id": "501675"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())

    assert len(seen) == 1, "给了 performance_id 就不该再去搜一次"
    url = str(seen[0].url)
    assert url.startswith(MYSHOW_BASE)
    assert "/ajax/performance/501675" in url
    assert "sellChannel=7" in url
    assert "cookie" not in {k.lower() for k in seen[0].headers}
    assert snapshot.platform == "maoyan"
    assert snapshot.trains["广州"].seats[SEAT_NAME].available is True


def test_fetch_trusts_detail_over_stale_search_index():
    """这条是实测钉出来的：同一时刻列表报「预售」、详情报「在售中」。

    列表那份是索引里的旧值。要是信了列表，用户会被一条过期的状态牵着走——
    该响的时候不响，不该响的时候乱响。
    """
    list_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ajax/performances/" in url:
            list_hits.append(url)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "performanceId": 501675,
                            "name": "陈粒「一粒」广州站",
                            "cityName": "广州",
                            "ticketStatus": 2,  # 索引里的旧值：预售
                        }
                    ],
                },
            )
        # 详情：权威值
        return httpx.Response(200, json=_detail_payload(3))

    adapter = MaoyanAdapter()
    task = TaskConfig(
        id="my2", adapter="maoyan", interval_seconds=600, params={"keyword": "陈粒"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())

    assert len(list_hits) == 1, "关键词模式：先搜一次拿 id"
    seat = snapshot.trains["广州"].seats[SEAT_NAME]
    assert seat.raw == "在售中", "必须以详情为准，不能被列表的旧值覆盖"
    assert seat.available is True


def test_fetch_with_city_narrowing_picks_matching_candidate():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/ajax/performances/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {"performanceId": 1, "name": "陈粒·北京站", "cityName": "北京"},
                        {"performanceId": 2, "name": "陈粒·广州站", "cityName": "广州"},
                    ],
                },
            )
        return httpx.Response(200, json=_detail_payload(3, performanceId=2))

    adapter = MaoyanAdapter()
    task = TaskConfig(
        id="my3",
        adapter="maoyan",
        interval_seconds=600,
        params={"keyword": "陈粒", "city": "广州"},
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    assert snapshot.trains["广州"].extra["performance_id"] == "2"


def test_fetch_without_id_or_keyword_explains_the_two_ways():
    adapter = MaoyanAdapter()
    task = TaskConfig(id="my4", adapter="maoyan", interval_seconds=600, params={})

    with pytest.raises(AdapterError) as exc:
        asyncio.run(adapter.fetch(task, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))))

    message = str(exc.value)
    assert "performance_id" in message
    assert "keyword" in message
    # 报错必须给出下一步动作，而不只是陈述失败
    assert "radar find maoyan" in message


def test_search_with_no_hits_says_how_to_recover():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "data": []})

    adapter = MaoyanAdapter()
    task = TaskConfig(
        id="my5", adapter="maoyan", interval_seconds=600, params={"keyword": "不存在的演出"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "radar find maoyan" in str(exc.value)


def test_http_error_tells_user_not_to_bypass():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="blocked")

    adapter = MaoyanAdapter()
    task = TaskConfig(
        id="my6", adapter="maoyan", interval_seconds=600, params={"performance_id": "1"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "不要尝试绕过" in str(exc.value)


def test_non_json_body_is_reported_with_a_preview():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>风控页</html>")

    adapter = MaoyanAdapter()
    task = TaskConfig(
        id="my7", adapter="maoyan", interval_seconds=600, params={"performance_id": "1"}
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "风控页" in str(exc.value)


# ---------------------------------------------------------------------------
# radar find 用的搜索函数
# ---------------------------------------------------------------------------


def test_search_performances_is_usable_without_a_task():
    """用户第一次接平台时手上还没有任务配置，这条路径必须走得通。"""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={"code": 200, "data": [{"performanceId": 501675, "name": "陈粒"}]},
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_performances(client, "陈粒")

    rows = asyncio.run(go())
    assert rows and rows[0]["performanceId"] == 501675
    assert "%E9%99%88%E7%B2%92" in captured[0]
