"""大麦「巡演各站在售状态」解析的单测。

为什么单独一个文件：这是大麦这条线上**唯一稳定的可购信号**。
Web 端不卖票（PC 渠道恒为「该渠道不支持购票」），票档余量只在 App 的选座页，
能拿到的就是 ``saleStatus``（热卖 / 缺货 / 预约）。它一旦被改坏，
用户的回流票提醒就悄无声息地失效了——所以每个取值都要有测试钉着。

样例数据取自 2026-09-30 对真实项目（陈粒广州站，itemId=1076797433026）的实测响应。
"""

from __future__ import annotations

from radar.adapters.damai import (
    TOUR_SEAT_NAME,
    DamaiAdapter,
    parse_damai_tour,
)
from radar.config import TaskConfig

#: 真实响应（2026-09-30 陈粒巡演）的结构，裁掉了无关字段。
TOUR_PAYLOAD = {
    "api": "mtop.damai.item.detail.getdetail",
    "v": "1.0",
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "guide": {
            "tour": {
                "projectList": [
                    {
                        "cityName": "三亚站",
                        "itemId": 1077886637254,
                        "saleStatus": "预约",
                        "showTime": "12.05",
                        "statusLight": True,
                    },
                    {
                        "cityName": "广州站",
                        "itemId": 1076797433026,
                        "saleStatus": "热卖",
                        "showTime": "10.17-10.18",
                        "statusLight": True,
                    },
                    {
                        "cityName": "临沂站",
                        "itemId": 1069526844481,
                        "saleStatus": "热卖",
                        "showTime": "10.24",
                        "statusLight": True,
                    },
                    {
                        "cityName": "贵阳站",
                        "itemId": 1077728405297,
                        "saleStatus": "缺货",
                        "showTime": "11.07",
                        "statusLight": False,
                    },
                    # 状态待定的站：既没 saleStatus 也没演出时间
                    {"cityName": "泉州站", "itemId": 1027900172735, "showTime": "演出时间待定"},
                ]
            }
        }
    },
}

GUANGZHOU_ITEM_ID = "1076797433026"


def _task(**params) -> TaskConfig:
    return TaskConfig(id="t1", adapter="damai", interval_seconds=600, params=params)


# ---------------------------------------------------------------------------
# 1. 正常解析
# ---------------------------------------------------------------------------


def test_parses_every_station_that_has_a_known_status():
    trains = parse_damai_tour(TOUR_PAYLOAD)
    # 泉州站的状态看不懂 → 跳过（不猜、不误报），所以是 4 站而不是 5 站
    assert set(trains) == {"三亚站", "广州站", "临沂站", "贵阳站"}


def test_hot_sale_means_available():
    """「热卖」= 在售。这是「现在能买」的信号。"""
    seat = parse_damai_tour(TOUR_PAYLOAD)["广州站"].seats[TOUR_SEAT_NAME]
    assert seat.available is True
    assert seat.count is None      # 只知道「有票」，不知道张数
    assert seat.raw == "热卖"


def test_sold_out_station_is_unavailable():
    seat = parse_damai_tour(TOUR_PAYLOAD)["贵阳站"].seats[TOUR_SEAT_NAME]
    assert seat.available is False
    assert seat.count == 0


def test_reservation_counts_as_unavailable():
    """「预约」= 还没开票，现在买不到。

    必须算「不可购」——否则「预约 → 热卖」的转变不会触发提醒，
    而那正是用户等的开票那一刻。
    """
    seat = parse_damai_tour(TOUR_PAYLOAD)["三亚站"].seats[TOUR_SEAT_NAME]
    assert seat.available is False


def test_show_time_is_kept_for_display():
    assert parse_damai_tour(TOUR_PAYLOAD)["广州站"].depart_time == "10.17-10.18"


def test_seat_name_is_a_fixed_string():
    """席别名必须固定。若拿 saleStatus 原文当 key，状态一变 key 就跟着变，
    diff 会把「改名」误读成「票没了又来了」。"""
    for train in parse_damai_tour(TOUR_PAYLOAD).values():
        assert list(train.seats) == [TOUR_SEAT_NAME]


def test_station_name_is_used_as_the_unit_code():
    assert "广州站" in parse_damai_tour(TOUR_PAYLOAD)


# ---------------------------------------------------------------------------
# 2. 过滤：只盯一站
# ---------------------------------------------------------------------------


def test_item_id_filter_keeps_only_that_station():
    trains = parse_damai_tour(TOUR_PAYLOAD, item_id=GUANGZHOU_ITEM_ID)
    assert set(trains) == {"广州站"}


def test_city_filter_is_a_fuzzy_match():
    trains = parse_damai_tour(TOUR_PAYLOAD, city="广州")
    assert set(trains) == {"广州站"}


def test_city_filter_can_match_several():
    trains = parse_damai_tour(TOUR_PAYLOAD, city="站")
    assert len(trains) == 4


def test_filters_combine():
    assert parse_damai_tour(TOUR_PAYLOAD, item_id=GUANGZHOU_ITEM_ID, city="临沂") == {}


# ---------------------------------------------------------------------------
# 3. 容错
# ---------------------------------------------------------------------------


def test_missing_tour_section_returns_empty():
    assert parse_damai_tour({"data": {"item": {"itemId": 1}}}) == {}


def test_empty_payload_returns_empty():
    assert parse_damai_tour({}) == {}
    assert parse_damai_tour({"data": None}) == {}


def test_accepts_data_already_unwrapped():
    """调用方若已经剥掉了外层 ``data``，也要能解析。"""
    inner = TOUR_PAYLOAD["data"]
    assert set(parse_damai_tour(inner)) == {"三亚站", "广州站", "临沂站", "贵阳站"}


def test_unknown_status_is_skipped_not_guessed():
    payload = {
        "data": {
            "guide": {
                "tour": {
                    "projectList": [
                        {"cityName": "某站", "itemId": 1, "saleStatus": "内部测试中"}
                    ]
                }
            }
        }
    }
    assert parse_damai_tour(payload) == {}


def test_station_without_name_is_skipped():
    payload = {"data": {"guide": {"tour": {"projectList": [{"saleStatus": "热卖"}]}}}}
    assert parse_damai_tour(payload) == {}


# ---------------------------------------------------------------------------
# 4. 接进适配器：详情接口优先走巡演解析
# ---------------------------------------------------------------------------


def test_adapter_prefers_tour_parsing():
    trains = DamaiAdapter().parse_payload(TOUR_PAYLOAD, _task(item_id=GUANGZHOU_ITEM_ID))
    assert set(trains) == {"广州站"}
    assert trains["广州站"].seats[TOUR_SEAT_NAME].available is True


def test_adapter_applies_city_filter_from_params():
    trains = DamaiAdapter().parse_payload(TOUR_PAYLOAD, _task(city="贵阳"))
    assert set(trains) == {"贵阳站"}
    assert trains["贵阳站"].seats[TOUR_SEAT_NAME].available is False


def test_adapter_falls_back_to_perform_payload():
    """没有巡演站点时，退回老的「场次 + 票档」解析——接口漂回旧形态也还能用。"""
    payload = {
        "data": {
            "result": {
                "performBases": [
                    {"performId": "P1", "performName": "2026-10-01 周五 19:30"}
                ],
                "skuList": [{"performId": "P1", "priceName": "380元看台", "status": 1}],
            }
        }
    }
    trains = DamaiAdapter().parse_payload(payload, _task())
    assert set(trains) == {"P1"}
    assert trains["P1"].seats["380元看台"].available is True


def test_adapter_raises_with_shape_when_structure_is_unknown():
    """既没有巡演也没有场次时，要报错并附上结构树，而不是编造一个空结果。

    报错信息带结构树是刻意的：没有它，用户只看到「解析失败」四个字，
    还得自己去翻接口返回。
    """
    import pytest

    from radar.adapters.base import AdapterError

    with pytest.raises(AdapterError) as exc:
        DamaiAdapter().parse_payload({"data": {"foo": "bar"}}, _task())
    assert "items_path" in str(exc.value) or "foo" in str(exc.value)
