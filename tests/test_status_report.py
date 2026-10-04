"""状态播报（`--announce` 与 `radar status`）的测试。

这一组测试守的是一个很容易被当成 bug 的行为：
**大麦有票，但 ntfy 里一条大麦消息都没有。**

原因是监控只在「变化」时开口，而「一开始就在卖」不构成变化。
所以状态播报不是锦上添花，它是这类任务唯一的发声渠道——
它要是坏了，用户就会以为平台没接上。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from radar.cli import (
    MAX_STATUS_ITEMS,
    _seat_text,
    _startup_message,
    _status_block,
    _status_message,
    _warm_up,
)
from radar.config import TaskConfig
from radar.models import SeatAvailability, Snapshot, TrainState

# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def _task(tid: str, name: str = "", link: str | None = None) -> TaskConfig:
    return TaskConfig(id=tid, name=name, adapter="damai", link=link)


def _seat(
    seat_type: str = "二等座",
    raw: str = "3",
    count: int | None = 3,
    available: bool = True,
) -> SeatAvailability:
    return SeatAvailability(
        seat_type=seat_type, raw=raw, count=count, available=available, price=None
    )


def _train(code: str, seats: dict[str, SeatAvailability], depart: str = "") -> TrainState:
    return TrainState(train_code=code, depart_time=depart, seats=seats)


def _snap(trains: dict[str, TrainState] | None = None, context: dict[str, str] | None = None) -> Snapshot:
    return Snapshot(
        task_id="t1",
        platform="damai",
        captured_at=datetime.now(timezone.utc),
        context=context or {},
        trains=trains or {},
    )


def _flat(lines: list[str]) -> str:
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _seat_text：优先照搬平台原话
# ---------------------------------------------------------------------------


def test_seat_text_prefers_platform_wording_when_count_unknown():
    """大麦返回的是「热卖」而不是张数，照搬比翻译成「有票」信息量大。"""
    assert _seat_text(_seat(seat_type="售票状态", raw="热卖", count=None)) == "售票状态 热卖"


def test_seat_text_uses_count_when_known():
    assert _seat_text(_seat(count=12, raw="12")) == "二等座 12 张"


def test_seat_text_falls_back_when_raw_missing():
    """有的适配器不填 raw，不能因此渲染出一个空荡荡的「二等座 」。"""
    assert _seat_text(_seat(raw="", count=None)) == "二等座 有票"


# ---------------------------------------------------------------------------
# _status_block：一个任务压成几行
# ---------------------------------------------------------------------------


def test_status_block_reports_damai_hot_sale():
    """大麦场景：项目级「热卖」，必须一眼看出来是「能买」。"""
    snap = _snap(
        trains={
            "广州站": _train(
                "广州站",
                {"售票状态": _seat(seat_type="售票状态", raw="热卖", count=None)},
                depart="10.17-10.18",
            )
        }
    )
    text = _flat(_status_block(_task("chenli", "陈粒广州站"), snap))

    assert "陈粒广州站" in text
    assert "热卖" in text
    assert "有票：1/1 项" in text
    assert "10.17-10.18" in text


def test_status_block_says_sold_out_when_nothing_available():
    """全无票时要明说，否则用户看到一屏空白还是会怀疑程序坏了。"""
    snap = _snap(trains={"广州站": _train("广州站", {"售票状态": _seat(raw="缺货", count=0, available=False)})})
    text = _flat(_status_block(_task("chenli", "陈粒广州站"), snap))

    assert "暂无余票" in text
    assert "1 项全部无票" in text
    # 「没票」是用户最想看到的那句话，必须加粗，不能夹在一堆明细里
    assert "**暂无余票**" in text


def test_status_detail_budget_stays_phone_readable():
    """守护「播报是在手机上看」这个设计意图。

    这条测试的作用是**挡住未来的回退**：有人觉得「多列几项更清楚」把
    MAX_STATUS_ITEMS 调大时，这里会红，并提醒他去看一眼真实手机上的效果。
    实测在一屏放 5 条明细时，结论「有票 193/211」会被顶出屏幕。
    """
    assert MAX_STATUS_ITEMS <= 3, (
        f"明细上限是 {MAX_STATUS_ITEMS}，在手机上会把结论顶出屏幕。"
        "要更多信息让用户跑 radar status，别塞进播报。"
    )


def test_status_block_marks_fetch_failure_instead_of_lying():
    """抓取失败要写成「没抓到」，不能渲染成「没票」——那是两句完全不同的话。"""
    text = _flat(_status_block(_task("chenli", "陈粒广州站"), None))

    assert "没抓到数据" in text
    assert "暂无余票" not in text


def test_status_block_handles_empty_result():
    snap = _snap(trains={})
    assert "没有可售项" in _flat(_status_block(_task("x", "X"), snap))


def test_status_block_truncates_long_lists():
    """12306 一个上午二三十趟车，全列出来播报本身就是噪音。"""
    trains = {
        f"G{i:04d}": _train(f"G{i:04d}", {"二等座": _seat(count=i)})
        for i in range(1, MAX_STATUS_ITEMS + 4)
    }
    lines = _status_block(_task("rail", "广州南 → 长沙南"), _snap(trains=trains))
    text = _flat(lines)

    assert f"有票：{MAX_STATUS_ITEMS + 3}/{MAX_STATUS_ITEMS + 3} 项" in text
    assert f"另有 3 项" in text
    # 省略提示必须告诉用户「去哪儿看全部」，只说「未列出」等于让人无从下手
    assert "radar status" in text
    # 结论行要加粗：手机上先看到的应该是「有票还是没票」，不是一屏车次
    assert f"**有票：{MAX_STATUS_ITEMS + 3}/{MAX_STATUS_ITEMS + 3} 项**" in text
    # 明细行数受控：标题 + 结论 + 明细 + 省略提示
    assert len(lines) <= MAX_STATUS_ITEMS + 3


def test_status_block_carries_query_context():
    """同时盯多个日期时，不写上下文根本分不清这条说的是哪天。"""
    snap = _snap(
        trains={"G1": _train("G1", {"二等座": _seat(count=1)})},
        context={"乘车日期": "2026-10-06"},
    )
    assert "乘车日期：2026-10-06" in _flat(_status_block(_task("r", "R"), snap))


# ---------------------------------------------------------------------------
# _startup_message：老措辞必须原样保留
# ---------------------------------------------------------------------------


def test_startup_message_without_snapshots_still_lists_names():
    """不传快照时退化成原来的纯名单，老行为不能坏。"""
    msg = _startup_message([_task("a", "广州南 → 长沙南"), _task("b", "京沪早班")])

    assert msg.title == "【余票监控】已启动"
    assert "广州南 → 长沙南" in msg.body
    assert "2 个任务" in msg.body
    assert "只有检测到余票变化才会再提醒你" in msg.body
    assert "不是程序停了" in msg.body


def test_startup_message_with_snapshots_shows_state_not_just_names():
    """带快照时，播报必须回答「现在有没有票」，这是它升级的全部意义。"""
    snap = _snap(
        trains={"广州站": _train("广州站", {"售票状态": _seat(seat_type="售票状态", raw="热卖", count=None)})}
    )
    msg = _startup_message([_task("chenli", "陈粒广州站")], {"chenli": snap})

    assert "热卖" in msg.body
    assert "只有检测到余票变化才会再提醒你" in msg.body
    assert "不是程序停了" in msg.body


def test_startup_message_survives_partial_failure():
    """一个任务抓挂了，不能连累另一个任务的播报。"""
    snap = _snap(trains={"G1": _train("G1", {"二等座": _seat(count=2)})})
    msg = _startup_message(
        [_task("ok", "正常任务"), _task("bad", "挂掉的任务")], {"ok": snap, "bad": None}
    )

    assert "二等座 2 张" in msg.body
    assert "没抓到数据" in msg.body


# ---------------------------------------------------------------------------
# _status_message：radar status --push
# ---------------------------------------------------------------------------


def test_status_message_is_not_a_change_alert():
    """`status` 回答「现在怎样」，不能和「发生变化了」混为一谈。"""
    snap = _snap(trains={"G1": _train("G1", {"二等座": _seat(count=1)})})
    msg = _status_message([_task("r", "R")], {"r": snap})

    assert msg.title == "【余票状态】当前快照"
    assert "不是变化提醒" in msg.body
    assert "抓取时间" in msg.body
    # 尾句不能误用启动播报的「安静 = 没变化」
    assert "不是程序停了" not in msg.body


# ---------------------------------------------------------------------------
# _warm_up：播报前那一轮抓取
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, snapshots: dict[str, Snapshot | None]) -> None:
        self._snapshots = snapshots

    def latest_snapshot(self, task_id: str) -> Snapshot | None:
        return self._snapshots.get(task_id)


class _FakeMonitor:
    def __init__(self, store: _FakeStore, results: dict[str, object]) -> None:
        self.store = store
        self._results = results
        self.calls: list[tuple[str, bool]] = []
        self.reused: list[tuple[str, Snapshot]] = []
        self.failures: list[tuple[str, BaseException]] = []

    async def poll_once(self, task: TaskConfig, *, notify: bool = True) -> list[object]:
        self.calls.append((task.id, notify))
        result = self._results.get(task.id)
        if isinstance(result, BaseException):
            raise result
        return []

    def reuse_next(self, task_id: str, snapshot: Snapshot) -> None:
        self.reused.append((task_id, snapshot))

    def record_failure(self, task_id: str, exc: BaseException) -> None:
        """预热失败要记进健康统计，否则 `radar health` 会把它显示成「正常」。"""
        self.failures.append((task_id, exc))


def test_warm_up_never_notifies():
    """预抓的那一轮只建基线，绝不能顺手推一屏「有票」——那是启动噪音。"""
    snap = _snap(trains={"G1": _train("G1", {"二等座": _seat(count=1)})})
    monitor = _FakeMonitor(_FakeStore({"a": snap}), {"a": []})

    result = asyncio.run(_warm_up(monitor, [_task("a", "A")]))

    assert result == {"a": snap}
    assert monitor.calls == [("a", False)]


def test_warm_up_hands_the_snapshot_to_the_next_round():
    """预热抓到的快照必须交出去给第一轮复用。

    不交出去的后果不是「多花一次请求」那么轻：平台限流按平台算，
    大麦是 300 秒，重抓会让首轮晚整整 5 分钟才出结果。
    """
    snap = _snap(trains={"G1": _train("G1", {"二等座": _seat(count=1)})})
    monitor = _FakeMonitor(_FakeStore({"a": snap}), {"a": []})

    asyncio.run(_warm_up(monitor, [_task("a", "A")]))

    assert monitor.reused == [("a", snap)]


def test_warm_up_isolates_failures():
    """一个任务抛异常，其余任务的状态照常带回。"""
    snap = _snap(trains={"G1": _train("G1", {"二等座": _seat(count=1)})})
    monitor = _FakeMonitor(
        _FakeStore({"ok": snap, "bad": None}),
        {"ok": [], "bad": RuntimeError("cookie 过期")},
    )

    result = asyncio.run(_warm_up(monitor, [_task("ok", "正常"), _task("bad", "挂了")]))

    assert result["ok"] is snap
    assert result["bad"] is None  # 失败标记成 None，播报里会写成「没抓到数据」
    # 挂掉的任务不该把垃圾塞进复用槽
    assert monitor.reused == [("ok", snap)]
    # 失败必须进健康统计：不记的话 `radar health` 会把这个任务显示成「正常」，
    # 而它其实一次都没抓成功——抓错了还报平安，正是本项目要消灭的故障。
    assert monitor.failures == [("bad", monitor._results["bad"])]


def test_warm_up_marks_task_absent_from_store():
    """抓取成功但库里没有快照（首次运行且没落盘）也不能炸。"""
    monitor = _FakeMonitor(_FakeStore({}), {"a": []})
    assert asyncio.run(_warm_up(monitor, [_task("a", "A")])) == {"a": None}


@pytest.mark.parametrize("count", [0])
def test_sold_out_zero_is_not_confused_with_unknown(count: int):
    """0 张 = 没票；None = 有票但数量未知。这个区分是状态机的地基。

    把 0 误当成「有票」会让状态机在「售罄」时反而报「放票」。
    """
    assert _seat_text(_seat(count=count, available=False, raw="0")) == "二等座 0 张"
    assert _seat_text(_seat(count=None, available=True, raw="有")) == "二等座 有"
