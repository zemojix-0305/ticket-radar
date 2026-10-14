"""持久层测试。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from radar.models import Change, EventKind
from radar.store import Store
from tests.conftest import make_snapshot


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_snapshot_roundtrip(store: Store):
    snap = make_snapshot(trains={"G1": {"二等座": 5, "一等座": None, "商务座": 0}})
    store.save_snapshot(snap)

    loaded = store.latest_snapshot("t1")

    assert loaded is not None
    assert set(loaded.trains) == {"G1"}
    g1 = loaded.trains["G1"]
    assert g1.from_station == "北京南"
    assert g1.depart_time == "06:43"
    assert g1.seats["二等座"].count == 5
    # 「有票但数量未知」必须原样往返，不能被写成 0
    assert g1.seats["一等座"].count is None
    assert g1.seats["一等座"].available is True
    assert g1.seats["商务座"].available is False


def test_latest_snapshot_returns_newest(store: Store):
    older = make_snapshot(
        trains={"G1": {"二等座": 1}},
        when=dt.datetime(2026, 9, 29, 10, 0, tzinfo=dt.timezone.utc),
    )
    newer = make_snapshot(
        trains={"G1": {"二等座": 9}},
        when=dt.datetime(2026, 9, 29, 11, 0, tzinfo=dt.timezone.utc),
    )
    store.save_snapshot(older)
    store.save_snapshot(newer)

    loaded = store.latest_snapshot("t1")
    assert loaded is not None
    assert loaded.trains["G1"].seats["二等座"].count == 9


def test_latest_snapshot_none_when_empty(store: Store):
    """没有任何快照时返回 None —— engine 靠这个判断首轮，建立基线。"""
    assert store.latest_snapshot("never-seen") is None


def test_snapshots_are_isolated_by_task(store: Store):
    store.save_snapshot(make_snapshot(task_id="a", trains={"G1": {"二等座": 1}}))
    store.save_snapshot(make_snapshot(task_id="b", trains={"G2": {"二等座": 2}}))

    a = store.latest_snapshot("a")
    assert a is not None and set(a.trains) == {"G1"}
    assert store.snapshot_count("a") == 1
    assert store.snapshot_count("b") == 1


def test_record_and_list_changes(store: Store):
    now = dt.datetime.now(dt.timezone.utc)
    changes = [
        Change("t1", "rail12306", EventKind.APPEARED, "G1", "二等座", 0, 5, now),
        Change("t1", "rail12306", EventKind.SOLD_OUT, "G3", "商务座", 2, 0, now),
    ]
    assert store.record_changes(changes, notified=changes) == 2

    rows = store.history(task_id="t1")
    assert len(rows) == 2
    kinds = {r["kind"] for r in rows}
    assert kinds == {"appeared", "sold_out"}
    assert all(r["notified"] == 1 for r in rows)


def test_notified_marks_only_the_changes_that_were_actually_sent(store: Store):
    """「已推送」必须逐条精确。

    一轮里常常同时产出「该推的」和「只入库的」两类变更（比如 notify_on 只要
    appeared，却也出现了 increased）。若用一个布尔值统标所有行，
    `radar history` 就会把没推过的也显示成「已推送 ✓」——等于说谎。
    """
    now = dt.datetime.now(dt.timezone.utc)
    appeared = Change("t1", "p", EventKind.APPEARED, "G1", "二等座", 0, 1, now)
    increased = Change("t1", "p", EventKind.INCREASED, "G2", "二等座", 3, 4, now)

    store.record_changes([appeared, increased], notified=[appeared])

    by_code = {r["train_code"]: r for r in store.history(task_id="t1")}
    assert by_code["G1"]["notified"] == 1
    assert by_code["G2"]["notified"] == 0, "increased 没进推送正文，不该标成已推送"


def test_history_filters_by_task(store: Store):
    now = dt.datetime.now(dt.timezone.utc)
    store.record_changes([Change("a", "p", EventKind.APPEARED, "G1", "二等座", 0, 1, now)])
    store.record_changes([Change("b", "p", EventKind.APPEARED, "G2", "二等座", 0, 1, now)])

    assert len(store.history(task_id="a")) == 1
    assert len(store.history()) == 2


def test_record_empty_changes_is_noop(store: Store):
    assert store.record_changes([]) == 0


def test_history_limit(store: Store):
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(5):
        store.record_changes(
            [Change("t1", "p", EventKind.APPEARED, f"G{i}", "二等座", 0, 1, now)]
        )
    assert len(store.history(limit=2)) == 2


def test_purge_old_snapshots(store: Store):
    old = make_snapshot(
        trains={"G1": {"二等座": 1}},
        when=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30),
    )
    recent = make_snapshot(trains={"G1": {"二等座": 2}})
    store.save_snapshot(old)
    store.save_snapshot(recent)

    removed = store.purge_before(days=7)

    assert removed == 1
    assert store.snapshot_count("t1") == 1


def test_store_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "radar.db"
    s = Store(nested)
    try:
        assert nested.exists()
    finally:
        s.close()


# --- 回流规律：自有数据（2026-10-14）----------------------------------------


def _appeared(task_id: str, when: dt.datetime, code: str = "G1") -> Change:
    return Change(
        task_id=task_id,
        platform="rail12306",
        kind=EventKind.APPEARED,
        train_code=code,
        seat_type="二等座",
        before=0,
        after=5,
        detected_at=when,
    )


def test_restock_hours_counts_appeared_by_local_hour(store: Store):
    """回流时刻统计只认 appeared（回流），sold_out 不算。"""
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    ten = base.replace(hour=10, minute=5, second=0, microsecond=0)
    store.record_changes(
        [
            _appeared("t1", ten),
            _appeared("t1", ten + dt.timedelta(minutes=30), code="G2"),
            _appeared("t1", ten + dt.timedelta(hours=3), code="G3"),
            # 不相关的类型不该被算进来
            Change(
                task_id="t1",
                platform="rail12306",
                kind=EventKind.SOLD_OUT,
                train_code="G9",
                seat_type="二等座",
                before=5,
                after=0,
                detected_at=ten,
            ),
            # 别的任务不该串进来
            _appeared("t2", ten),
        ]
    )

    hours = store.restock_hours("t1")

    assert sum(hours.values()) == 3
    ten_h = ten.astimezone().hour
    assert hours[ten_h] == 2
    assert store.restock_total("t1") == 3
    assert store.restock_total("t2") == 1


def test_restock_hours_respects_window(store: Store):
    """超出时间窗的老数据不算——否则「近 30 天」就成了谎话。"""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
    store.record_changes([_appeared("t1", old)])

    assert store.restock_total("t1", days=30) == 0
    assert store.restock_total("t1", days=365) == 1
