"""`radar check --json` 的结构化输出测试。

这个命令存在的理由是「先看再挑」：先用结构化数据把候选筛一遍，
再决定 tasks.yaml 里到底盯哪几趟车。所以测试重点不是 JSON 格式本身，
而是**筛选时需要的那几个字段是否可靠**——尤其是 ``matched`` 和 ``price``。
"""

from __future__ import annotations

import dataclasses
import json

from radar.config import TaskConfig, WatchRule
from radar.models import Snapshot
from radar.view import snapshot_to_dict as _snapshot_to_dict
from tests.conftest import make_snapshot


def _task(**kwargs) -> TaskConfig:
    base = {
        "id": "t1",
        "name": "京沪高铁 早班车",
        "adapter": "rail12306",
        "params": {"date": "+7", "from": "北京南", "to": "上海虹桥"},
    }
    return TaskConfig(**{**base, **kwargs})


def _with_prices(seats: dict[str, dict[str, int | None]], prices: dict[str, float]) -> Snapshot:
    """造一个给指定席别贴上价格的快照。"""
    snap = make_snapshot(trains=seats)
    trains = {}
    for code, train in snap.trains.items():
        trains[code] = dataclasses.replace(
            train,
            seats={
                seat_type: (
                    dataclasses.replace(seat, price=prices[seat_type])
                    if seat_type in prices
                    else seat
                )
                for seat_type, seat in train.seats.items()
            },
        )
    return dataclasses.replace(snap, trains=trains)


def test_snapshot_to_dict_is_json_serializable():
    """必须能直接 json.dumps——有 date/datetime 混进去就会炸。"""
    data = _snapshot_to_dict(_task(), make_snapshot(trains={"G1": {"二等座": 5}}))
    assert json.loads(json.dumps(data, ensure_ascii=False))["task_id"] == "t1"


def test_snapshot_to_dict_carries_context_and_query():
    snap = dataclasses.replace(
        make_snapshot(trains={"G1": {"二等座": 5}}), context={"乘车日期": "2026-10-06"}
    )
    data = _snapshot_to_dict(_task(), snap)

    assert data["context"] == {"乘车日期": "2026-10-06"}
    # query 要原样回显，这样才知道 "+7" 对应的 context 是算出来的
    assert data["query"]["date"] == "+7"
    assert data["trains"][0]["depart"] is not None


def test_matched_requires_both_train_filter_and_seat_rule():
    """只看「有票」不够，得看「你的 watch 规则抓不抓得到」。

    挑出一趟明明有票、但 train_codes 里没写的车，是白高兴一场。
    """
    watch = WatchRule(
        seat_types=["二等座"], min_count=1, train_codes=["G1"], notify_on=["appeared"]
    )
    snap = make_snapshot(trains={"G1": {"二等座": 5}, "G3": {"二等座": 8}})
    data = _snapshot_to_dict(_task(watch=watch), snap)

    by_code = {t["train_code"]: t for t in data["trains"]}
    assert by_code["G1"]["matched"] is True
    assert by_code["G3"]["matched"] is False, "G3 有票但不在 train_codes 里，不该算命中"
    assert data["matched_count"] == 1


def test_matched_false_when_seat_below_min_count():
    watch = WatchRule(seat_types=["一等座"], min_count=5)
    snap = make_snapshot(trains={"G1": {"一等座": 3}})
    data = _snapshot_to_dict(_task(watch=watch), snap)

    assert data["matched_count"] == 0
    assert data["trains"][0]["matched"] is False


def test_seat_entries_flag_which_types_are_watched():
    watch = WatchRule(seat_types=["二等座"])
    snap = make_snapshot(trains={"G1": {"二等座": 5, "商务座": 1}})
    seats = _snapshot_to_dict(_task(watch=watch), snap)["trains"][0]["seats"]

    assert seats["二等座"]["watched"] is True
    assert seats["商务座"]["watched"] is False
    # 未关注的席别也要出现在输出里，方便临时改主意
    assert seats["商务座"]["available"] is True


def test_trains_sorted_by_departure_time():
    snap = make_snapshot(trains={"G9": {"二等座": 1}, "G1": {"二等座": 1}})
    # make_snapshot 里所有车次时间相同，这里手工改一下出发时间
    trains = {
        code: dataclasses.replace(train, depart_time=t)
        for code, t in (("G9", "09:00"), ("G1", "06:30"))
        for train in [snap.trains[code]]
    }
    data = _snapshot_to_dict(_task(), dataclasses.replace(snap, trains=trains))

    assert [t["train_code"] for t in data["trains"]] == ["G1", "G9"]


def test_seat_entries_expose_price_field():
    """票价要能筛——「700 块以内的二等座」是最常见的挑法。"""
    snap = _with_prices({"G1": {"二等座": 5, "一等座": 1}}, {"二等座": 661.0})
    seats = _snapshot_to_dict(_task(), snap)["trains"][0]["seats"]

    assert seats["二等座"]["price"] == 661.0
    assert seats["一等座"]["price"] is None


def test_price_defaults_to_null_when_not_looked_up():
    """没加 --with-price 时 price 必须是 null（而不是 0），否则筛价格会把免费票筛出来。"""
    seats = _snapshot_to_dict(_task(), make_snapshot(trains={"G1": {"二等座": 5}}))["trains"][0][
        "seats"
    ]

    assert seats["二等座"]["price"] is None


# --- watched vs matched：无票的车也在监控范围内 -----------------------------


def test_sold_out_train_is_watched_but_not_matched():
    """最容易读错的一处：无票的车 ``matched`` 是 False，但它**确实在监控范围内**。

    余票监控的核心目标就是「现在没票、等它放票」，所以把这两件事塞进一个字段，
    会让人以为无票的车不会被盯——恰好反了。
    """
    watch = WatchRule(seat_types=["二等座"], depart_after="06:00", depart_before="12:00")
    snap = make_snapshot(
        trains={"G1": {"二等座": 0}, "G3": {"二等座": 5}},
        depart_times={"G1": "08:00", "G3": "09:00"},
    )
    data = _snapshot_to_dict(_task(watch=watch), snap)

    by_code = {t["train_code"]: t for t in data["trains"]}
    assert by_code["G1"]["watched"] is True
    assert by_code["G1"]["matched"] is False, "现在没票，不该被推"

    assert by_code["G3"]["watched"] is True
    assert by_code["G3"]["matched"] is True

    assert data["watched_count"] == 2
    assert data["matched_count"] == 1


def test_train_outside_depart_window_is_not_watched():
    """窗外车次的 ``watched`` 必须是 False——它真的不会被监控，别给人虚假安全感。"""
    watch = WatchRule(seat_types=["二等座"], depart_after="06:00", depart_before="12:00")
    snap = make_snapshot(
        trains={"G1": {"二等座": 5}, "G900": {"二等座": 5}},
        depart_times={"G1": "08:00", "G900": "20:00"},
    )
    data = _snapshot_to_dict(_task(watch=watch), snap)

    by_code = {t["train_code"]: t for t in data["trains"]}
    assert by_code["G900"]["watched"] is False
    assert by_code["G900"]["matched"] is False
    assert data["watched_count"] == 1


def test_watch_echo_includes_depart_bounds():
    """回显 watch 才解释得了「这趟车为什么没被盯」——少了时间窗就说不清。"""
    watch = WatchRule(depart_after="6:00", depart_before="12:00")
    data = _snapshot_to_dict(_task(watch=watch), make_snapshot(trains={"G1": {"二等座": 5}}))

    assert data["watch"]["depart_after"] == "06:00"
    assert data["watch"]["depart_before"] == "12:00"


def test_train_without_watched_seat_type_is_not_watched():
    """上午的普速车（K/Z/T）压根没有「二等座」这一档，放票也不会推。

    把它们算进 ``watched`` 会让计数比实际能推的车多，又是虚假安全感。
    """
    watch = WatchRule(seat_types=["二等座"], depart_after="06:00", depart_before="12:00")
    snap = make_snapshot(
        trains={"G1": {"二等座": 5}, "K100": {"硬座": 5, "硬卧": 5}},
        depart_times={"G1": "08:00", "K100": "09:00"},
    )
    data = _snapshot_to_dict(_task(watch=watch), snap)

    by_code = {t["train_code"]: t for t in data["trains"]}
    assert by_code["K100"]["watched"] is False
    assert by_code["G1"]["watched"] is True
    assert data["watched_count"] == 1
