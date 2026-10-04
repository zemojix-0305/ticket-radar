"""12306 适配器的解析逻辑测试。全部离线，用固定 fixture。"""

from __future__ import annotations

import dataclasses
import datetime as dt

import httpx
import pytest

from radar.adapters.rail12306 import (
    Rail12306Adapter,
    parse_price,
    parse_seat_value,
    parse_station_js,
    parse_ticket_prices,
    parse_trains,
    resolve_date,
)
from tests.conftest import load_fixture

# --- 日期归一化 ------------------------------------------------------------


def test_resolve_date_iso_passthrough():
    assert resolve_date("2026-10-01") == "2026-10-01"


def test_resolve_date_relative():
    today = dt.date(2026, 9, 29)
    assert resolve_date("today", today=today) == "2026-09-29"
    assert resolve_date("tomorrow", today=today) == "2026-09-30"
    assert resolve_date("+3", today=today) == "2026-10-02"
    assert resolve_date("-1", today=today) == "2026-09-28"


def test_resolve_date_invalid_raises():
    with pytest.raises(Exception, match="无法解析日期"):
        resolve_date("下周")


# --- 席别取值 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_count", "expected_available"),
    [
        ("有", None, True),
        ("5", 5, True),
        ("0", 0, False),
        ("无", 0, False),
    ],
)
def test_parse_seat_value_known(raw, expected_count, expected_available):
    seat = parse_seat_value("二等座", raw)
    assert seat is not None
    assert seat.count == expected_count
    assert seat.available is expected_available


@pytest.mark.parametrize("raw", ["", "--", "*", "-"])
def test_parse_seat_value_not_offered_returns_none(raw):
    assert parse_seat_value("商务座", raw) is None


def test_seat_effective_is_comparable():
    """'有' 归一成哨兵值，保证和数字比大小有意义。"""
    abundant = parse_seat_value("二等座", "有")
    five = parse_seat_value("二等座", "5")
    none = parse_seat_value("二等座", "无")
    assert abundant is not None and five is not None and none is not None
    assert abundant.effective > five.effective > none.effective


# --- 站名表 ----------------------------------------------------------------


def test_parse_station_js():
    text = "var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0@shh|上海|SHH|shanghai|sh|1';"
    table = parse_station_js(text)
    assert table["北京北"] == "VAP"
    assert table["VAP"] == "VAP"
    assert table["beijingbei"] == "VAP"
    assert table["上海"] == "SHH"


# --- 车次解析 --------------------------------------------------------------


def test_parse_trains_from_fixture():
    fixture = load_fixture("left_ticket_sample.json")
    rows = fixture["data"]["result"]
    station_map = fixture["data"]["map"]

    trains = parse_trains(rows, station_map)

    assert set(trains) == {"G1", "G3"}

    g1 = trains["G1"]
    assert g1.from_station == "北京南"
    assert g1.to_station == "上海虹桥"
    assert g1.depart_time == "06:43"
    assert g1.arrive_time == "11:36"
    assert g1.duration == "04:53"

    # 二等座「有」-> count None / available True
    assert g1.seats["二等座"].count is None
    assert g1.seats["二等座"].available is True
    # 一等座 5
    assert g1.seats["一等座"].count == 5
    # 商务座 0 -> 有记录但不可用
    assert g1.seats["商务座"].count == 0
    assert g1.seats["商务座"].available is False
    # 「--」的席别应该直接不存在，不污染快照
    assert "软卧" not in g1.seats
    assert "高级软卧" not in g1.seats


def test_parse_trains_handles_short_rows():
    """字段缺失的行不能让解析崩掉——余票接口字段是会漂移的。"""
    trains = parse_trains(["|预订|240000G1010|G9"], {"VNP": "北京南"})
    assert "G9" in trains
    assert trains["G9"].depart_time == ""
    assert trains["G9"].seats == {}


def test_parse_trains_skips_blank_rows():
    assert parse_trains(["", "|"]) == {}


def test_parse_trains_field_override():
    """字段漂移时能用配置覆盖下标，不必改代码。"""
    row = "a|b|c|G42|d|e|VNP|AOH|08:00|09:00|01:00|Y|x|20261001||P2|01|01|Y|N|--|--|--|--|--|--|--|--|--|--|7|--|--|--"
    trains = parse_trains([row], {"VNP": "北京南", "AOH": "上海虹桥"})
    assert trains["G42"].seats["二等座"].count == 7


# --- 适配器元信息 ----------------------------------------------------------


def test_adapter_min_interval_is_not_aggressive():
    """守住合规下限：这个值被人改小之前，测试先红。"""
    adapter = Rail12306Adapter()
    assert adapter.min_interval >= 60.0
    assert adapter.requires_credentials is False


def test_adapter_registered():
    from radar.adapters import available_adapters

    assert "rail12306" in available_adapters()


# --- 快照要带上「这次查的是哪一天」-----------------------------------------


def test_fetch_records_resolved_travel_date_in_context(monkeypatch):
    """配置里写的是 `+7` 这种相对日期，只有适配器知道它解析成了哪一天。

    不带给上层，推送里就只剩「检测时间」，同时盯多个日期的用户收到的
    通知长得一模一样，分不清是哪天的票。
    """
    import asyncio

    from radar.config import TaskConfig

    adapter = Rail12306Adapter()

    async def fake_telecode(client, name):
        return "VNP"

    async def fake_query(client, travel_date, from_code, to_code):
        return load_fixture("left_ticket_sample.json")

    monkeypatch.setattr(adapter, "_to_telecode", fake_telecode)
    monkeypatch.setattr(adapter, "_query_raw", fake_query)

    task = TaskConfig(
        id="t1",
        adapter="rail12306",
        params={"from": "北京南", "to": "上海虹桥", "date": "+7"},
    )
    snapshot = asyncio.run(adapter.fetch(task, None))

    assert snapshot.context == {"乘车日期": resolve_date("+7")}
    assert snapshot.context["乘车日期"] == resolve_date("+7")
    # 顺带确认上下文没把余票解析带坏
    assert set(snapshot.trains) == {"G1", "G3"}


# --- 票价 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("¥661.0", 661.0),   # 带符号 -> 元
        ("￥1058.0", 1058.0),  # 全角符号也要认
        ("6610", 661.0),      # 裸数字 -> 角
        ("10580", 1058.0),
        ("", None),
        ("--", None),
        ("¥0.0", None),       # 0 元不是有效票价，别显示成「免费」
        ("有", None),
    ],
)
def test_parse_price_handles_both_units(raw, expected):
    """12306 的票价字段有「元」和「角」两种单位，混淆会把 ¥661 显示成 ¥6610。"""
    assert parse_price(raw) == expected


def test_parse_ticket_prices_maps_codes_to_chinese_seat_names():
    payload = {
        "data": {
            "O": "¥661.0",
            "M": "¥1058.0",
            "A9": "¥2315.0",
            "9": "23150",
            "MIN": "¥661.0",          # 聚合字段，要忽略
            "OT": ["优选一等座: ¥1468.0"],
            "train_no": "24000000G10L",
        }
    }
    prices = parse_ticket_prices(payload)

    assert prices["二等座"] == 661.0
    assert prices["一等座"] == 1058.0
    assert prices["商务座"] == 2315.0
    assert "MIN" not in prices and "OT" not in prices


def test_parse_ticket_prices_missing_data_is_empty():
    assert parse_ticket_prices({}) == {}
    assert parse_ticket_prices({"data": {}}) == {}


def test_parse_trains_records_price_lookup_credentials():
    """补价要拿 train_no + 站序去另一个端点换，解析时就得顺手记下来。"""
    fixture = load_fixture("left_ticket_sample.json")
    trains = parse_trains(fixture["data"]["result"], fixture["data"]["map"])

    g1 = trains["G1"]
    assert g1.extra["train_no"] == "240000G1010"
    assert g1.extra["from_station_no"] == "01"
    assert g1.extra["to_station_no"] == "05"
    assert g1.extra["seat_types"] == "9MODO"


def _price_handler(price_payload: dict):
    def handler(request):
        return httpx.Response(200, json=price_payload)

    return handler


def test_enrich_prices_fills_seats_without_touching_availability():
    import asyncio

    from radar.config import TaskConfig
    from tests.conftest import make_snapshot, mock_client

    adapter = Rail12306Adapter()
    task = TaskConfig(
        id="t1", adapter="rail12306",
        params={"from": "北京南", "to": "上海虹桥", "date": "+7"},
    )
    snap = make_snapshot(trains={"G1": {"二等座": 5, "一等座": None}})
    snap = dataclasses.replace(
        snap,
        trains={
            "G1": dataclasses.replace(
                snap.trains["G1"],
                extra={
                    "train_no": "24000000G10L",
                    "from_station_no": "01",
                    "to_station_no": "07",
                    "seat_types": "9MOO",
                },
            )
        },
    )
    payload = {"data": {"O": "¥661.0", "M": "¥1058.0"}}

    async def go():
        async with mock_client(_price_handler(payload)) as client:
            return await adapter.enrich_prices(snap, task, client, {"G1"})

    out = asyncio.run(go())

    assert out.trains["G1"].seats["二等座"].price == 661.0
    assert out.trains["G1"].seats["一等座"].price == 1058.0
    # 余票状态不能被补价改名换姓
    assert out.trains["G1"].seats["二等座"].count == 5
    assert out.trains["G1"].seats["一等座"].available is True
    assert out.trains["G1"].seats["二等座"].label == "二等座 5 张 ¥661"


def test_enrich_prices_skips_trains_without_credentials():
    """字段漂移导致没记下 train_no 时，应该跳过而不是发一个空参数请求。"""
    import asyncio

    from radar.config import TaskConfig
    from tests.conftest import make_snapshot, mock_client

    adapter = Rail12306Adapter()
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "北京南", "to": "上海虹桥"})
    snap = make_snapshot(trains={"G1": {"二等座": 5}})  # extra 为空
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": {"O": "¥661.0"}})

    async def go():
        async with mock_client(handler) as client:
            return await adapter.enrich_prices(snap, task, client, {"G1"})

    out = asyncio.run(go())

    assert out.trains["G1"].seats["二等座"].price is None
    assert all("queryTicketPrice" not in url for url in calls)


def test_enrich_prices_degrades_silently_on_http_error():
    """拿不到票价只是没有票价，绝不能连余票提醒一起丢掉。"""
    import asyncio

    from radar.config import TaskConfig
    from tests.conftest import make_snapshot, mock_client

    adapter = Rail12306Adapter()
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "北京南", "to": "上海虹桥"})
    snap = make_snapshot(trains={"G1": {"二等座": 5}})
    snap = dataclasses.replace(
        snap,
        trains={
            "G1": dataclasses.replace(
                snap.trains["G1"], extra={"train_no": "24000000G10L", "seat_types": "9MOO"}
            )
        },
    )

    async def go():
        async with mock_client(lambda r: httpx.Response(500, text="boom")) as client:
            return await adapter.enrich_prices(snap, task, client, {"G1"})

    out = asyncio.run(go())

    assert out.trains["G1"].seats["二等座"].price is None
    assert out.trains["G1"].seats["二等座"].count == 5
