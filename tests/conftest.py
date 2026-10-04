"""共享测试工具。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx

from radar.models import EventKind, SeatAvailability, Snapshot, TrainState  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_snapshot(
    task_id: str = "t1",
    platform: str = "test",
    trains: dict[str, dict[str, int | None]] | None = None,
    when: dt.datetime | None = None,
    depart_times: dict[str, str] | None = None,
) -> Snapshot:
    """构造快照。

    ``trains`` 形如 ``{"G1": {"二等座": 5, "一等座": None}}``，
    值为 None 表示「有票但数量未知」，值为 0/负数表示无票。

    ``depart_times`` 可给个别车次指定发车时刻（``{"G1": "08:00"}``），
    用于测试按时间窗筛选；不指定的车次沿用默认的 06:43。
    """
    states: dict[str, TrainState] = {}
    for code, seats in (trains or {}).items():
        seat_objs: dict[str, SeatAvailability] = {}
        for seat_type, count in seats.items():
            if count is None:
                seat_objs[seat_type] = SeatAvailability(
                    seat_type=seat_type, raw="有", count=None, available=True
                )
            else:
                seat_objs[seat_type] = SeatAvailability(
                    seat_type=seat_type, raw=str(count), count=count, available=count > 0
                )
        states[code] = TrainState(
            train_code=code,
            from_station="北京南",
            to_station="上海虹桥",
            depart_time=(depart_times or {}).get(code, "06:43"),
            arrive_time="11:36",
            duration="04:53",
            seats=seat_objs,
        )
    return Snapshot(
        task_id=task_id,
        platform=platform,
        captured_at=when or dt.datetime.now(dt.timezone.utc),
        trains=states,
    )


def mock_client(handler) -> httpx.AsyncClient:
    """包一个 MockTransport 的 AsyncClient，用于测试通知渠道。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


__all__ = ["FIXTURES", "load_fixture", "make_snapshot", "mock_client"]
