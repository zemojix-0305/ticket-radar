"""领域模型：快照、席别、变更事件。

设计约束（务必先读）
--------------------
模型层刻意**不包含**任何下单相关字段——没有 secretStr、没有乘车人、
没有提交流程、没有支付参数。这是有意的架构性约束而非遗漏：

只要模型层表达不出「提交订单」这件事，上层就不可能误用它去抢票。
把约束下沉到类型系统，比写在文档里靠人自觉可靠得多。

如果你打算 fork 之后加抢票功能，请先读 README 的「合规边界」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

#: 「有票」但没有具体张数时使用的等效哨兵值。
#: 12306 部分席别返回「有」而不是数字，表示余票充足。
ABUNDANT = 10**6


def normalize_count(value: int) -> int | None:
    """把内部等效张数归一成对外的三态表示。

    * ``None`` —— 有票但数量未知（内部是 ``ABUNDANT``）
    * ``0``    —— 无票
    * ``n``    —— n 张

    务必区分 ``None`` 和 ``0``：前者是「有票」，后者是「没票」。
    早期版本图省事写成 ``value or None``，结果把 0 和「有票」混成同一个值，
    直接导致状态机判错方向。
    """
    return None if value >= ABUNDANT else value


class EventKind(str, Enum):
    """余票状态变化类型。"""

    APPEARED = "appeared"      # 无票 -> 有票。默认唯一会推送的事件
    INCREASED = "increased"    # 有票 -> 更多票
    DECREASED = "decreased"    # 有票 -> 更少票
    SOLD_OUT = "sold_out"      # 有票 -> 无票
    NEW_TRAIN = "new_train"    # 上一轮不存在的车次出现（加开列车）

    def __str__(self) -> str:  # 便于 f-string 直接输出
        return self.value


@dataclass(frozen=True)
class SeatAvailability:
    """单个席别的余票状态。"""

    seat_type: str
    raw: str                      # 平台原始值，保留以便排查解析问题
    count: int | None             # None = 「有」但数量未知
    available: bool
    #: 票价（元）。``None`` = 平台不提供，或本次没去补查。
    #: 为什么默认不查：票价要**按车次**单独请求，常态轮询必须保持每轮 1 次请求，
    #: 所以只在「确实要发通知」时由 engine 调 :meth:`Adapter.enrich_prices` 补。
    price: float | None = None
    #: 票价的币种符号。默认人民币——12306 和国内演出票都是。
    #: 加这个字段是因为摩天轮会给境外场次返回 ``HK$``：把 3499 港币渲染成
    #: 「¥3499」不是显示不美观，是**说错了价格**。
    currency: str = "¥"

    @property
    def effective(self) -> int:
        """用于比较的等效张数。'有' 按 ABUNDANT 处理，保证排序和差值有意义。"""
        return ABUNDANT if self.count is None else self.count

    @property
    def price_text(self) -> str:
        """便于展示的票价文本，例如 ``¥661``。没查到返回空串。"""
        if self.price is None:
            return ""
        # :g 避免整元票价显示成 "¥661.0"
        return f"{self.currency}{self.price:g}"

    @property
    def label(self) -> str:
        if not self.available:
            base = f"{self.seat_type} 无票"
        elif self.count is None:
            base = f"{self.seat_type} 有票"
        else:
            base = f"{self.seat_type} {self.count} 张"
        return f"{base} {self.price_text}".strip()

    def to_payload(self) -> dict[str, Any]:
        return {
            "seat_type": self.seat_type,
            "raw": self.raw,
            "count": self.count,
            "available": self.available,
            "price": self.price,
            # 币种要落库：老快照里没有这个键，读的时候按默认人民币兜底
            "currency": self.currency,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SeatAvailability:
        return cls(
            seat_type=payload["seat_type"],
            raw=payload.get("raw", ""),
            count=payload.get("count"),
            available=bool(payload.get("available")),
            price=payload.get("price"),
            currency=payload.get("currency") or "¥",
        )


@dataclass(frozen=True)
class TrainState:
    """一趟车（或一场演出）在某次抓取时的状态。"""

    train_code: str
    from_station: str = ""
    to_station: str = ""
    depart_time: str = ""
    arrive_time: str = ""
    duration: str = ""
    seats: dict[str, SeatAvailability] = field(default_factory=dict)
    #: 适配器私有簿记，用于「需要时再补一次请求」的场景（12306 补票价要带
    #: train_no 和站序）。刻意**不进出 diff、不落库、不展示**——它只是
    #: 抓取时顺手记下的凭据，不是业务状态。
    extra: dict[str, str] = field(default_factory=dict)

    def seat(self, seat_type: str) -> SeatAvailability | None:
        return self.seats.get(seat_type)

    def effective_of(self, seat_type: str) -> int:
        """该席别等效张数；席别不存在时按 0 处理。"""
        s = self.seats.get(seat_type)
        return 0 if s is None else s.effective

    def to_payload(self) -> dict[str, Any]:
        return {
            "train_code": self.train_code,
            "from_station": self.from_station,
            "to_station": self.to_station,
            "depart_time": self.depart_time,
            "arrive_time": self.arrive_time,
            "duration": self.duration,
            "seats": {k: v.to_payload() for k, v in self.seats.items()},
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TrainState:
        return cls(
            train_code=payload["train_code"],
            from_station=payload.get("from_station", ""),
            to_station=payload.get("to_station", ""),
            depart_time=payload.get("depart_time", ""),
            arrive_time=payload.get("arrive_time", ""),
            duration=payload.get("duration", ""),
            seats={
                k: SeatAvailability.from_payload(v)
                for k, v in (payload.get("seats") or {}).items()
            },
        )


@dataclass(frozen=True)
class Snapshot:
    """一次抓取的完整结果。存库 + 作为下一轮 diff 的基准。"""

    task_id: str
    platform: str
    captured_at: datetime
    #: 抓取时任务 ``params`` 的指纹（见 :func:`params_fingerprint`）。
    #: 两轮指纹不同 = 抓的其实不是同一批货（典型场景：``date: "+7"`` 跨天滚动），
    #: 此时绝不能拿新旧快照做 diff，否则会把「换了一天」误报成「放票了」。
    params_fingerprint: str = ""
    #: 这次查询的可读上下文，由适配器填写，例如 ``{"乘车日期": "2026-10-06"}``。
    #: 存在的理由：配置里写的是 ``+7`` 这种相对日期，只有适配器知道它解析成了
    #: 哪一天。不带上这个，推送里就只剩「检测时间」，同时盯多个日期的用户
    #: 根本分不清是哪天的票。
    context: dict[str, str] = field(default_factory=dict)
    trains: dict[str, TrainState] = field(default_factory=dict)

    @classmethod
    def empty(cls, task_id: str, platform: str) -> Snapshot:
        return cls(task_id=task_id, platform=platform, captured_at=datetime.now(timezone.utc))

    def context_line(self) -> str:
        """把上下文拼成一行 ``乘车日期：2026-10-06``。无上下文返回空串。"""
        return "　".join(f"{k}：{v}" for k, v in self.context.items() if v)

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "captured_at": self.captured_at.isoformat(),
            "params_fingerprint": self.params_fingerprint,
            "context": dict(self.context),
            "trains": {k: v.to_payload() for k, v in self.trains.items()},
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Snapshot:
        return cls(
            task_id=payload["task_id"],
            platform=payload["platform"],
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            # 老库没有这两个字段，取默认值即可——空串/空字典正好触发一次重建基线
            params_fingerprint=payload.get("params_fingerprint", ""),
            context=dict(payload.get("context") or {}),
            trains={
                k: TrainState.from_payload(v) for k, v in (payload.get("trains") or {}).items()
            },
        )


def params_fingerprint(params: Any) -> str:
    """把任务的 ``params`` 压成一个稳定短哈希。

    用途：判断「这一轮抓的」和「上一轮抓的」是不是同一批货。
    典型受害者是 ``date: "+7"`` 这类相对日期——过了午夜它就指向新的一天，
    但车次号不变，diff 会误判成余票变化。

    只关心内容不关心顺序，所以 ``sort_keys=True``；
    ``default=str`` 兜住 date / datetime 之类的非 JSON 原生类型。
    """
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Change:
    """一条余票变更事件，由 ChangeDetector 产出，是推送的最小单位。

    ``before`` / ``after`` 是三态的，见 ``normalize_count``：
    ``None`` = 有票（数量未知）、``0`` = 无票、``n`` = n 张。
    """

    task_id: str
    platform: str
    kind: EventKind
    train_code: str
    seat_type: str
    before: int | None
    after: int | None
    detected_at: datetime

    @staticmethod
    def format_count(value: int | None) -> str:
        if value is None:
            return "有票"
        if value == 0:
            return "无票"
        return f"{value} 张"

    def describe(self) -> str:
        """人类可读的一行描述，用于拼通知正文。"""
        return (
            f"{self.train_code} {self.seat_type}："
            f"{self.format_count(self.before)} → {self.format_count(self.after)}"
        )


__all__ = [
    "ABUNDANT",
    "Change",
    "EventKind",
    "SeatAvailability",
    "Snapshot",
    "TrainState",
    "normalize_count",
]
