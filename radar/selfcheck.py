"""语义自检：抓到了，但抓得**讲不讲得通**？

监控工具最危险的失败模式不是「抓不到」——而是「抓错了还告诉你一切正常」。
一个平时能解析出 20 个票档的适配器，某天突然返回 0 条，如果引擎只把它当成
「这次没票」，就会一直沉默，用户直到开场都不知道监控早瞎了。

本模块只做一件事：拿到一轮快照后，判断「这个结果本身可信吗」。
它不碰网络、不碰平台知识，只看快照内部是否自相矛盾、或与历史对不上。

三个判定层级（严重度递增）
------------------------
``HEALTHY``   结果讲得通，正常入库、正常 diff。
``DEGRADED``  可疑，但可能是真的（比如真的突然全售罄）。记一条日志，连续出现才告警。
``BROKEN``    几乎可以肯定是解析坏了（比如所有票档都解析成了「未知」）。
             引擎应当**立刻**推一条「适配器可能失效」的告警，而不是沉默。

为什么不做成「和上一轮比」：那种比对属于 ``ChangeDetector`` 的职责（它关心
「余票变多了没有」）。自检关心的是「这一轮本身合不合理」——
两者正交，所以自检不依赖 diff，可以直接在抓到快照后独立跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import TaskConfig
    from .models import Snapshot

#: 席别名 / 原始值落在这套集合里，说明这一格根本没解析出来，是占位的。
#: 注意 **不含** ``"无"`` / ``"售罄"`` —— 那是合法的「真的没票」，不是解析失败。
SUSPICIOUS_SEAT_MARKERS = {"", "未知", "unknown", "?", "n/a", "null", "none", "-"}


class Verdict(str, Enum):
    """一轮快照的可信度结论。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"

    def __str__(self) -> str:
        return self.value

    @property
    def is_ok(self) -> bool:
        """还能当真用吗？BROKEN 不可用，DEGRADED/HEALTHY 可用。"""
        return self is not Verdict.BROKEN


@dataclass
class SelfCheckReport:
    """一次自检的结论 + 人能看懂的理由。"""

    verdict: Verdict
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict.is_ok

    def describe(self) -> str:
        if not self.reasons:
            return "快照结构正常"
        return "；".join(self.reasons)


def _suspicious_seat_count(snapshot: Snapshot) -> tuple[int, int]:
    """返回 (票档总数, 其中可疑的个数)。

    一个票档「可疑」= 它的席别名落在 :data:`SUSPICIOUS_SEAT_MARKERS` 里。
    席别名是解析器从接口里抠出来的关键字段，抠不到它，这一格就废了。
    """
    total = 0
    suspicious = 0
    for train in snapshot.trains.values():
        for seat in train.seats.values():
            total += 1
            if (seat.seat_type or "").strip().lower() in SUSPICIOUS_SEAT_MARKERS:
                suspicious += 1
    return total, suspicious


def check_snapshot(
    snapshot: Snapshot,
    previous: Snapshot | None,
    task: TaskConfig | None = None,
) -> SelfCheckReport:
    """判断这一轮快照讲不讲得通。

    :param snapshot: 本轮抓到的快照。
    :param previous: 上一轮快照（来自库）；为 ``None`` 表示首轮，不做事后对比。
    :param task: 当前任务（仅用于把任务名写进理由，便于排查）。
     """
    task_label = task.display_name if task is not None else snapshot.task_id
    reasons: list[str] = []

    if snapshot.trains:
        total, suspicious = _suspicious_seat_count(snapshot)
        if total and suspicious == total:
            # 所有票档都废了：这基本可以肯定是解析器失效，而不是真的没票。
            # 真没票时，票档仍然会以「无票/售罄」的形式存在，席别名不会丢。
            return SelfCheckReport(
                Verdict.BROKEN,
                [
                    f"[{task_label}] 全部 {total} 个票档都解析成了未知/占位值，"
                    "解析器很可能失效（上游改了字段？）。建议升级适配器。"
                ],
            )
        if suspicious:
            reasons.append(
                f"[{task_label}] {suspicious}/{total} 个票档解析异常"
                f"（席别名成了占位符），其余正常"
            )
    else:
        # 没有任何条目本身不是故障——演出可能真没开卖、二手平台可能真没挂单。
        # 但「上一轮明明有数据，这一轮突然 0 个」就对不上了：
        # 最可能是触发了风控、被重定向到登录页、或页面结构变了。
        if previous is not None and previous.trains:
            return SelfCheckReport(
                Verdict.DEGRADED,
                [
                    f"[{task_label}] 上一轮抓到 {len(previous.trains)} 个条目，"
                    "本轮突然 0 个——可能是触发风控、被弹到登录页，或页面结构变了。"
                ],
            )

    if reasons:
        return SelfCheckReport(Verdict.DEGRADED, reasons)
    return SelfCheckReport(Verdict.HEALTHY, [])


__all__ = ["SelfCheckReport", "SUSPICIOUS_SEAT_MARKERS", "Verdict", "check_snapshot"]
