"""引擎测试：状态机、平台限流、通知文案。"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import time

import httpx
import pytest

from radar.adapters.base import (
    Adapter,
    AdapterError,
    AuthError,
    ParseError,
    RateLimitedError,
    TransportError,
)
from radar.config import AppConfig, TaskConfig, WatchRule
from radar.engine import (
    MAX_MESSAGE_TRAINS,
    ChangeDetector,
    HealthRow,
    Monitor,
    PlatformRateLimiter,
    Verdict,
    _classify_error,
    format_message,
)
from radar.models import Change, EventKind, Snapshot, normalize_count, params_fingerprint
from radar.notifier import Message, Notifier
from radar.store import Store
from tests.conftest import make_snapshot, mock_client

ALL_SEATS = WatchRule()


# --- 首轮建立基线 ----------------------------------------------------------


def test_first_run_produces_no_events():
    """首轮只建基线。不然程序一启动就推一屏「有票」，那是骚扰。"""
    current = make_snapshot(trains={"G1": {"二等座": 5}})
    assert ChangeDetector.diff(None, current, ALL_SEATS) == []


# --- 三态表示 --------------------------------------------------------------


def test_count_normalization_three_states():
    """0 / None / n 是三件不同的事。这条测试防止有人再写 `value or None`。"""
    from radar.models import ABUNDANT

    assert normalize_count(0) == 0
    assert normalize_count(3) == 3
    assert normalize_count(ABUNDANT) is None


def test_sold_out_is_zero_not_none():
    """回归测试：早期用 `before or None` 把 0 吃成了 None，导致方向判反。"""
    prev = make_snapshot(trains={"G1": {"二等座": 5}})
    curr = make_snapshot(trains={"G1": {"二等座": 0}})
    change = ChangeDetector.diff(prev, curr, ALL_SEATS)[0]
    assert change.after == 0
    assert change.after is not None
    assert change.describe() == "G1 二等座：5 张 → 无票"


# --- 状态迁移 --------------------------------------------------------------


def test_sold_out_to_available_is_appeared():
    prev = make_snapshot(trains={"G1": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 3}})

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    assert len(changes) == 1
    assert changes[0].kind is EventKind.APPEARED
    assert changes[0].train_code == "G1"
    assert changes[0].seat_type == "二等座"
    assert changes[0].before == 0
    assert changes[0].after == 3


def test_absent_seat_to_available_is_appeared():
    """上一轮整个席别不存在（还在维护/未放票），这一轮放出来了。"""
    prev = make_snapshot(trains={"G1": {}})
    curr = make_snapshot(trains={"G1": {"二等座": 8}})

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    assert [c.kind for c in changes] == [EventKind.APPEARED]
    assert changes[0].before == 0


def test_available_to_sold_out():
    prev = make_snapshot(trains={"G1": {"二等座": 5}})
    curr = make_snapshot(trains={"G1": {"二等座": 0}})

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    assert [c.kind for c in changes] == [EventKind.SOLD_OUT]


def test_increase_and_decrease():
    up = ChangeDetector.diff(
        make_snapshot(trains={"G1": {"二等座": 2}}),
        make_snapshot(trains={"G1": {"二等座": 7}}),
        ALL_SEATS,
    )
    assert [c.kind for c in up] == [EventKind.INCREASED]
    assert (up[0].before, up[0].after) == (2, 7)

    down = ChangeDetector.diff(
        make_snapshot(trains={"G1": {"二等座": 7}}),
        make_snapshot(trains={"G1": {"二等座": 2}}),
        ALL_SEATS,
    )
    assert [c.kind for c in down] == [EventKind.DECREASED]


def test_new_train_detected():
    prev = make_snapshot(trains={"G1": {"二等座": 5}})
    curr = make_snapshot(trains={"G1": {"二等座": 5}, "G99": {"二等座": 2}})

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    assert [c.kind for c in changes] == [EventKind.NEW_TRAIN]
    assert changes[0].train_code == "G99"
    assert changes[0].before == 0
    assert changes[0].after == 2


def test_no_change_no_event():
    snap_a = make_snapshot(trains={"G1": {"二等座": 5}})
    snap_b = make_snapshot(trains={"G1": {"二等座": 5}})
    assert ChangeDetector.diff(snap_a, snap_b, ALL_SEATS) == []


def test_abundant_transition():
    """'有'(哨兵值) 与具体数字之间的迁移也要能识别。"""
    prev = make_snapshot(trains={"G1": {"二等座": None}})
    curr = make_snapshot(trains={"G1": {"二等座": 4}})

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    assert [c.kind for c in changes] == [EventKind.DECREASED]
    # 对外表示必须归一，ABUNDANT 哨兵不能泄漏成 1000000
    assert changes[0].before is None
    assert changes[0].after == 4


def test_multi_seat_multi_train():
    prev = make_snapshot(
        trains={"G1": {"二等座": 0, "一等座": 5}, "G2": {"二等座": 3}}
    )
    curr = make_snapshot(
        trains={"G1": {"二等座": 4, "一等座": 0}, "G2": {"二等座": 3}}
    )

    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)
    got = {(c.train_code, c.seat_type): c.kind for c in changes}

    assert got == {
        ("G1", "二等座"): EventKind.APPEARED,
        ("G1", "一等座"): EventKind.SOLD_OUT,
    }


# --- 关注规则过滤 ----------------------------------------------------------


def test_train_code_filter():
    rule = WatchRule(train_codes=["G1"])
    prev = make_snapshot(trains={"G1": {"二等座": 0}, "G3": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 5}, "G3": {"二等座": 5}})

    changes = ChangeDetector.diff(prev, curr, rule)

    assert [c.train_code for c in changes] == ["G1"]


def test_train_code_wildcard():
    rule = WatchRule(train_codes=["G1*"])
    assert rule.matches_train("G1234") is True
    assert rule.matches_train("D1234") is False

    prev = make_snapshot(trains={"G1": {"二等座": 0}, "D3": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 5}, "D3": {"二等座": 5}})
    assert [c.train_code for c in ChangeDetector.diff(prev, curr, rule)] == ["G1"]


def test_seat_type_filter():
    rule = WatchRule(seat_types=["二等座"])
    prev = make_snapshot(trains={"G1": {"二等座": 0, "商务座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 5, "商务座": 3}})

    changes = ChangeDetector.diff(prev, curr, rule)

    assert [c.seat_type for c in changes] == ["二等座"]


def test_min_count_threshold():
    rule = WatchRule(min_count=5)
    prev = make_snapshot(trains={"G1": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 3}})

    # 3 张不满足「至少 5 张」，不应该报 APPEARED
    changes = ChangeDetector.diff(prev, curr, rule)
    assert all(c.kind is not EventKind.APPEARED for c in changes)


def test_notify_on_filter():
    rule = WatchRule(notify_on=["appeared"])
    assert rule.should_notify(EventKind.APPEARED) is True
    assert rule.should_notify(EventKind.DECREASED) is False


# --- 平台限流 --------------------------------------------------------------


def test_rate_limiter_enforces_interval():
    async def scenario() -> float:
        limiter = PlatformRateLimiter()
        await limiter.acquire("p", 0.05)
        start = time.monotonic()
        await limiter.acquire("p", 0.05)
        return time.monotonic() - start

    assert asyncio.run(scenario()) >= 0.045, "同平台第二次请求没有被限流"


def test_rate_limiter_is_per_platform():
    """不同平台互不阻塞。限流是平台级，不是全局级。"""

    async def scenario() -> float:
        limiter = PlatformRateLimiter()
        await limiter.acquire("alpha", 1.0)
        start = time.monotonic()
        await limiter.acquire("beta", 1.0)
        return time.monotonic() - start

    assert asyncio.run(scenario()) < 0.1


def test_rate_limiter_serializes_concurrent_tasks():
    """最关键的合规测试：同一平台 N 个任务并发，请求仍然是串行的。

    如果这条测试红了，说明「多加几个任务就能提高刷新频率」——
    那正是本项目要避免的行为。
    """

    async def scenario() -> float:
        limiter = PlatformRateLimiter()
        start = time.monotonic()
        await asyncio.gather(*(limiter.acquire("rail12306", 0.05) for _ in range(4)))
        return time.monotonic() - start

    elapsed = asyncio.run(scenario())
    assert elapsed >= 0.14, f"4 个并发请求只花了 {elapsed:.3f}s，限流没生效"


# --- 通知文案 --------------------------------------------------------------


def test_format_message_contains_key_facts():
    task = TaskConfig(
        id="t1",
        adapter="rail12306",
        params={"from": "北京南", "to": "上海虹桥"},
        link="https://www.12306.cn/index/",
    )
    prev = make_snapshot(trains={"G1": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 6}})
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    message = format_message(task, changes, curr)

    assert "北京南" in message.title and "上海虹桥" in message.title
    assert "G1" in message.body
    assert "二等座" in message.body
    assert "6 张" in message.body
    assert "06:43" in message.body
    assert message.url == "https://www.12306.cn/index/"
    # 文案必须表明是提醒而非代购
    assert "自行下单" in message.body


def test_format_message_abundant_renders_as_you_piao():
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    prev = make_snapshot(trains={"G1": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": None}})
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    body = format_message(task, changes, curr).body
    assert "有票" in body
    assert "1000000" not in body


@pytest.mark.parametrize("kind", list(EventKind))
def test_every_event_kind_has_a_label(kind):
    """新增事件类型时，通知文案里必须有对应中文，不能漏出英文枚举值。"""
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    snap = make_snapshot(trains={"G1": {"二等座": 3}})
    change = Change(
        task_id="t1",
        platform="test",
        kind=kind,
        train_code="G1",
        seat_type="二等座",
        before=1,
        after=3,
        detected_at=dt.datetime.now(dt.timezone.utc),
    )

    body = format_message(task, [change], snap).body

    assert kind.value not in body, f"事件 {kind.value} 未映射为中文"


# --- 参数变化要重建基线（`date: "+7"` 跨天滚动的坑）-------------------------


def test_params_fingerprint_ignores_key_order():
    """只关心内容，不关心 YAML 里的书写顺序。"""
    a = params_fingerprint({"date": "+7", "from": "北京南", "to": "上海虹桥"})
    b = params_fingerprint({"to": "上海虹桥", "date": "+7", "from": "北京南"})
    assert a == b


def test_params_fingerprint_changes_with_rolling_date():
    assert params_fingerprint({"date": "+7"}) != params_fingerprint({"date": "+8"})


def test_params_fingerprint_handles_non_json_types():
    """params 里可能有 date / datetime，别让指纹函数自己炸掉。"""
    assert params_fingerprint({"date": dt.date(2026, 10, 6)})


def test_snapshot_payload_roundtrip_preserves_fingerprint():
    snap = dataclasses.replace(
        make_snapshot(trains={"G1": {"二等座": 5}}), params_fingerprint="abc123"
    )
    assert Snapshot.from_payload(snap.to_payload()).params_fingerprint == "abc123"


def test_legacy_snapshot_payload_without_fingerprint_loads():
    """加指纹字段之前存下的老快照要能读出来，缺字段按空串处理。"""
    payload = make_snapshot(trains={"G1": {"二等座": 5}}).to_payload()
    payload.pop("params_fingerprint")
    assert Snapshot.from_payload(payload).params_fingerprint == ""


class _ScriptedAdapter(Adapter):
    """按剧本吐余票的假适配器：第 n 轮返回 ``counts[n]``。"""

    name = "scripted"
    min_interval = 0.0

    def __init__(self, counts: list[int]) -> None:
        super().__init__({})
        self._counts = list(counts)
        self.calls = 0

    async def fetch(self, task, client):  # noqa: ARG002
        count = self._counts[min(self.calls, len(self._counts) - 1)]
        self.calls += 1
        return make_snapshot(
            task_id=task.id, platform=self.name, trains={"G1": {"二等座": count}}
        )


def test_rolling_date_rebuilds_baseline_instead_of_false_alert(tmp_path, monkeypatch):
    """`date: "+7"` 跨过午夜会指向新的一天，但车次号没变。

    不处理的话，diff 会拿 10-07 的余票去比 10-06 的余票，
    把「换了一天」当成「放票了」推给用户。这里钉死正确行为：
    参数一变就丢弃旧基准，且**不能**产生任何变更事件。
    """
    adapter = _ScriptedAdapter([0, 5, 20])
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    cfg = AppConfig()
    today = TaskConfig(
        id="t1", adapter="scripted", params={"date": "+7"}, interval_seconds=300
    )
    tomorrow = today.model_copy(update={"params": {"date": "+8"}})

    async def go():
        async with Monitor(
            cfg,
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[],
        ) as monitor:
            return (
                await monitor.poll_once(today, notify=False),
                await monitor.poll_once(tomorrow, notify=False),
                await monitor.poll_once(tomorrow, notify=False),
            )

    try:
        first, second, third = asyncio.run(go())
    finally:
        store.close()

    assert first == []  # 首轮只建基线
    assert second == [], "日期滚到新的一天，不能把「换了一天」报成「放票了」"
    # 但基线确实换成了新日期的数据——同参数的下一轮对比照常工作
    assert [c.kind for c in third] == [EventKind.INCREASED]


# --- 推送文案要带「哪天的票」-----------------------------------------------


def test_snapshot_context_survives_payload_roundtrip():
    snap = dataclasses.replace(make_snapshot(trains={"G1": {"二等座": 5}}), context={"乘车日期": "2026-10-06"})
    assert Snapshot.from_payload(snap.to_payload()).context == {"乘车日期": "2026-10-06"}


def test_legacy_snapshot_payload_without_context_loads():
    payload = make_snapshot(trains={"G1": {"二等座": 5}}).to_payload()
    payload.pop("context")
    assert Snapshot.from_payload(payload).context == {}


def test_context_line_skips_empty_values():
    snap = dataclasses.replace(
        make_snapshot(), context={"乘车日期": "2026-10-06", "备注": ""}
    )
    assert snap.context_line() == "乘车日期：2026-10-06"
    assert make_snapshot().context_line() == ""


def test_format_message_shows_travel_date_when_known():
    """有上下文时，文案必须写出乘车日期，而不是只给一个检测时间。"""
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "北京南", "to": "上海虹桥"})
    snap = dataclasses.replace(
        make_snapshot(trains={"G1": {"二等座": 5}}), context={"乘车日期": "2026-10-06"}
    )
    change = Change(
        task_id="t1",
        platform="test",
        kind=EventKind.APPEARED,
        train_code="G1",
        seat_type="二等座",
        before=0,
        after=5,
        detected_at=dt.datetime.now(dt.timezone.utc),
    )

    body = format_message(task, [change], snap).body

    assert "乘车日期：2026-10-06" in body


def test_format_message_falls_back_when_no_context():
    """没有上下文的适配器（猫眼/摩天轮等）文案不能变成空行。"""
    task = TaskConfig(id="t1", adapter="maoyan", params={"from": "北京南", "to": "上海虹桥"})
    snap = make_snapshot(trains={"G1": {"二等座": 5}})
    change = Change(
        task_id="t1",
        platform="test",
        kind=EventKind.APPEARED,
        train_code="G1",
        seat_type="二等座",
        before=0,
        after=5,
        detected_at=dt.datetime.now(dt.timezone.utc),
    )

    body = format_message(task, [change], snap).body

    assert "北京南 → 上海虹桥" in body.splitlines()[0]


# --- 票价：只在真要推送时才补 ----------------------------------------------


class _PricedAdapter(Adapter):
    """会补票价的假适配器，记录补价时被点名的车次。"""

    name = "priced"
    min_interval = 0.0

    def __init__(self, counts: list[int]) -> None:
        super().__init__({})
        self._counts = list(counts)
        self.calls = 0
        self.enriched: list[set[str]] = []

    async def fetch(self, task, client):  # noqa: ARG002
        count = self._counts[min(self.calls, len(self._counts) - 1)]
        self.calls += 1
        return make_snapshot(
            task_id=task.id, platform=self.name, trains={"G1": {"二等座": count}}
        )

    async def enrich_prices(self, snapshot, task, client, train_codes):  # noqa: ARG002
        self.enriched.append(set(train_codes))
        seat = snapshot.trains["G1"].seats["二等座"]
        return dataclasses.replace(
            snapshot,
            trains={
                "G1": dataclasses.replace(
                    snapshot.trains["G1"],
                    seats={
                        "二等座": dataclasses.replace(seat, price=661.0),
                    },
                )
            },
        )


class _Capture(Notifier):
    def __init__(self) -> None:
        super().__init__({}, None)
        self.name = "capture"
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)


def test_poll_once_enriches_price_only_when_notifying(tmp_path, monkeypatch):
    """票价要「按车次单独请求」才拿得到，所以只在真要发通知时补。

    常态轮询必须守住每轮 1 次请求——这条测试就是钉这个的。
    """
    adapter = _PricedAdapter([0, 5, 8])
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    capture = _Capture()
    watch = WatchRule(seat_types=["二等座"], notify_on=["appeared"])
    task = TaskConfig(
        id="t1", adapter="priced", params={"date": "+7"}, watch=watch, interval_seconds=300
    )

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[capture],
        ) as monitor:
            await monitor.poll_once(task)          # 首轮建基线：不该补价
            await monitor.poll_once(task)          # 无票 -> 有票：该补价并推送
            await monitor.poll_once(task)          # 5 -> 8：increased 不在 notify_on 里

    try:
        asyncio.run(go())
    finally:
        store.close()

    # 首轮没有任何变更，不该白花一次请求
    assert adapter.enriched == [{"G1"}]
    assert len(capture.sent) == 1
    body = capture.sent[0].body
    assert "¥661" in body, "补到的票价要出现在推送正文里"
    assert "乘车日期" not in body, "这个假适配器没填 context，不该凭空多出一行"


def test_poll_once_pushes_even_if_price_lookup_explodes(tmp_path, monkeypatch):
    """补价炸了不能连累余票提醒——这是 enrich_prices 的契约。"""
    adapter = _PricedAdapter([0, 5])
    adapter.enrich_prices = _boom  # type: ignore[method-assign]
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    capture = _Capture()
    watch = WatchRule(seat_types=["二等座"], notify_on=["appeared"])
    task = TaskConfig(
        id="t1", adapter="priced", params={"date": "+7"}, watch=watch, interval_seconds=300
    )

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[capture],
        ) as monitor:
            await monitor.poll_once(task)
            await monitor.poll_once(task)

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert len(capture.sent) == 1
    assert "余票出现" in capture.sent[0].body


async def _boom(*args, **kwargs):
    raise RuntimeError("price endpoint on fire")


def test_format_message_includes_price_when_known():
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "北京南", "to": "上海虹桥"})
    snap = make_snapshot(trains={"G1": {"二等座": 5}})
    snap = dataclasses.replace(
        snap,
        trains={
            "G1": dataclasses.replace(
                snap.trains["G1"],
                seats={"二等座": dataclasses.replace(snap.trains["G1"].seats["二等座"], price=661.0)},
            )
        },
    )
    change = Change(
        task_id="t1",
        platform="test",
        kind=EventKind.APPEARED,
        train_code="G1",
        seat_type="二等座",
        before=0,
        after=5,
        detected_at=dt.datetime.now(dt.timezone.utc),
    )

    body = format_message(task, [change], snap).body

    assert "二等座　5 张　¥661" in body


def test_format_message_without_price_has_no_dangling_spaces():
    task = TaskConfig(id="t1", adapter="maoyan", params={"from": "北京南", "to": "上海虹桥"})
    snap = make_snapshot(trains={"G1": {"二等座": 5}})
    change = Change(
        task_id="t1",
        platform="test",
        kind=EventKind.APPEARED,
        train_code="G1",
        seat_type="二等座",
        before=0,
        after=5,
        detected_at=dt.datetime.now(dt.timezone.utc),
    )

    body = format_message(task, [change], snap).body

    assert "二等座　5 张　（余票出现）" in body


class _ManyTrainAdapter(Adapter):
    """一轮吐出 N 趟车：首轮全无票，之后全有票——模拟一场大放票。"""

    name = "many"
    min_interval = 0.0

    def __init__(self, n: int) -> None:
        super().__init__({})
        self.n = n
        self.calls = 0
        self.enriched: list[list[str]] = []

    async def fetch(self, task, client):  # noqa: ARG002
        self.calls += 1
        count = 0 if self.calls == 1 else 5
        return make_snapshot(
            task_id=task.id,
            platform=self.name,
            trains={f"G{i}": {"二等座": count} for i in range(1, self.n + 1)},
        )

    async def enrich_prices(self, snapshot, task, client, train_codes):  # noqa: ARG002
        self.enriched.append(sorted(train_codes))
        return snapshot


def test_price_lookups_are_capped_on_mass_release(tmp_path, monkeypatch):
    """大放票会让所有车次同时变有票；不设上限就会一次打出几十个请求。

    那就从「低频只读」变成了本项目明确拒绝的高频行为——
    宁可少显示几个价格，也不能让一次通知变成一次批量请求。
    """
    from radar.engine import MAX_PRICE_LOOKUPS

    adapter = _ManyTrainAdapter(MAX_PRICE_LOOKUPS + 15)
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    capture = _Capture()
    task = TaskConfig(
        id="t1", adapter="many", params={"date": "+7"}, interval_seconds=300
    )

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[capture],
        ) as monitor:
            await monitor.poll_once(task)
            await monitor.poll_once(task)

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert len(adapter.enriched) == 1, "只该补一次价"
    picked = adapter.enriched[0]
    assert len(picked) == MAX_PRICE_LOOKUPS, "补价请求数必须被上限卡住"
    assert set(picked) <= {f"G{i}" for i in range(1, MAX_PRICE_LOOKUPS + 16)}
    # 提醒本身不能因为补价被截而缩水——25 趟车都该出现在推送里
    assert len(capture.sent) == 1
    assert capture.sent[0].body.count("二等座") == MAX_PRICE_LOOKUPS + 15


# --- 发车时间窗必须挡在 diff 里，而不是渲染时 -------------------------------


def test_diff_skips_trains_outside_depart_window():
    """窗外的车次连变更记录都不该产生。

    只在渲染时过滤是不够的：被排除的车次若照常写进 data/radar.db，
    历史表里会堆满你根本不关心的车次，日后就没法拿它看趋势了。
    """
    seats = {"G100": {"二等座": 0}, "G900": {"二等座": 0}}
    times = {"G100": "08:00", "G900": "20:00"}
    prev = make_snapshot(trains=seats, depart_times=times)
    curr = make_snapshot(
        trains={"G100": {"二等座": 5}, "G900": {"二等座": 5}}, depart_times=times
    )
    rule = WatchRule(seat_types=["二等座"], depart_after="06:00", depart_before="12:00")

    changes = ChangeDetector.diff(prev, curr, rule)

    assert [c.train_code for c in changes] == ["G100"]


def test_diff_without_window_keeps_all_trains():
    """不设时间窗时行为不变——老配置不能因为加了新字段就变安静。"""
    seats = {"G100": {"二等座": 0}, "G900": {"二等座": 0}}
    times = {"G100": "08:00", "G900": "20:00"}
    prev = make_snapshot(trains=seats, depart_times=times)
    curr = make_snapshot(
        trains={"G100": {"二等座": 5}, "G900": {"二等座": 5}}, depart_times=times
    )

    changes = ChangeDetector.diff(prev, curr, WatchRule(seat_types=["二等座"]))

    assert sorted(c.train_code for c in changes) == ["G100", "G900"]


def test_diff_combines_train_whitelist_and_time_window():
    """车次白名单和时间窗是「与」的关系，两个都过才算命中。"""
    times = {"G1": "08:00", "G3": "08:30", "G5": "20:00"}
    seats = {"G1": {"二等座": 0}, "G3": {"二等座": 0}, "G5": {"二等座": 0}}
    prev = make_snapshot(trains=seats, depart_times=times)
    curr = make_snapshot(
        trains={k: {"二等座": 5} for k in seats}, depart_times=times
    )
    rule = WatchRule(
        seat_types=["二等座"],
        train_codes=["G1", "G5"],  # G5 在白名单里但不在时间窗内
        depart_after="06:00",
        depart_before="12:00",
    )

    changes = ChangeDetector.diff(prev, curr, rule)

    assert [c.train_code for c in changes] == ["G1"]


# --- 「已推送」标记必须逐条精确 ---------------------------------------------


class _TwoTrainAdapter(Adapter):
    """首轮 G1 无票 / G2 三张；次轮 G1 五张（appeared）、G2 四张（increased）。"""

    name = "twotrain"
    min_interval = 0.0

    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0

    async def fetch(self, task, client):  # noqa: ARG002
        self.calls += 1
        trains = (
            {"G1": {"二等座": 0}, "G2": {"二等座": 3}}
            if self.calls == 1
            else {"G1": {"二等座": 5}, "G2": {"二等座": 4}}
        )
        return make_snapshot(task_id=task.id, platform=self.name, trains=trains)


def test_history_marks_only_the_changes_that_were_really_pushed(tmp_path, monkeypatch):
    """``radar history`` 的「已推送 ✓」必须只说真话。

    这一轮同时产出了 appeared（该推）和 increased（规则只要 appeared，不推）。
    用一个布尔值统标所有行，会让 increased 那条也显示成「已推送」——
    用户就会以为自己收到过一条根本没收到的提醒。
    """
    adapter = _TwoTrainAdapter()
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    capture = _Capture()
    task = TaskConfig(
        id="t1",
        adapter="twotrain",
        params={"date": "+7"},
        watch=WatchRule(seat_types=["二等座"], notify_on=["appeared"]),
        interval_seconds=300,
    )

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[capture],
        ) as monitor:
            await monitor.poll_once(task)  # 首轮建基线
            await monitor.poll_once(task)  # G1 appeared + G2 increased

    try:
        asyncio.run(go())
        rows = {r["train_code"]: r for r in store.history(task_id="t1")}
    finally:
        store.close()

    assert len(rows) == 2, "两条变更都要入库"
    assert len(capture.sent) == 1
    assert rows["G1"]["notified"] == 1
    assert rows["G2"]["notified"] == 0, "increased 没进推送正文，不能标成已推送"


def test_history_marks_nothing_when_notification_fails(tmp_path, monkeypatch):
    """推送失败时整批都不该标「已推送」——用户确实没收到。"""

    class _Broken(Notifier):
        def __init__(self) -> None:
            super().__init__({}, None)
            self.name = "broken"

        async def send(self, message):  # noqa: ARG002
            raise RuntimeError("gateway down")

    adapter = _ScriptedAdapter([0, 5])
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)

    store = Store(tmp_path / "radar.db")
    task = TaskConfig(
        id="t1",
        adapter="scripted",
        params={"date": "+7"},
        watch=WatchRule(seat_types=["二等座"], notify_on=["appeared"]),
        interval_seconds=300,
    )

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[_Broken()],
        ) as monitor:
            await monitor.poll_once(task)
            await monitor.poll_once(task)

    try:
        asyncio.run(go())
        rows = list(store.history(task_id="t1"))
    finally:
        store.close()

    assert len(rows) == 1
    assert rows[0]["notified"] == 0, "推送失败就不算已推送"


# --- 预热结果的复用 --------------------------------------------------------


class _CountingAdapter(Adapter):
    """每次 ``fetch`` 都记一笔，用来验证请求到底有没有真的发出去。

    ``min_interval`` 必须是 0：engine 的限流是**真的等**，
    写个 300 进来测试就会原地睡五分钟（这是真踩过的坑）。
    大麦那个 300 秒的间隔由「复用不打请求」这件事本身来规避，
    不靠把地板调低。
    """

    name = "counting"
    min_interval = 0.0

    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0

    async def fetch(self, task, client):  # noqa: ARG002
        self.calls += 1
        return make_snapshot(
            task_id=task.id, platform=self.name, trains={"G1": {"二等座": 5}}
        )


def test_prefetched_snapshot_is_reused_without_refetching(tmp_path, monkeypatch):
    """预热过的那一轮不该再打一次请求。

    这不是「省一次请求」的抠门。限流是按**平台**算的，大麦是 300 秒——
    重抓一次会让第一轮整整晚 5 分钟才出结果，用户看到的就是
    「启动了，然后什么都没发生」。
    """
    adapter = _CountingAdapter()
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)
    store = Store(tmp_path / "radar.db")
    task = TaskConfig(id="t1", adapter="counting", interval_seconds=600)

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[],
        ) as monitor:
            # ① 预热：真抓一次
            warm = await monitor.poll_once(task, notify=False)
            snapshot = store.latest_snapshot(task.id)
            assert snapshot is not None
            # ② 把结果交给下一轮
            monitor.reuse_next(task.id, snapshot)
            reused = await monitor.poll_once(task, notify=False)
            # ③ 再下一轮：缓存已消费，必须真抓
            after = await monitor.poll_once(task, notify=False)
            return warm, reused, after

    try:
        warm, reused, after = asyncio.run(go())
        count_after_reuse = store.snapshot_count(task.id)
    finally:
        store.close()

    assert warm == [] and reused == [] and after == []
    assert adapter.calls == 2, "中间那一轮必须复用，不能发第二次请求"
    # 复用的那一轮也不再写库：预热时已经写过了，重复写只会在曲线上多个同值点
    assert count_after_reuse == 2, "预热 + 最后一轮，共两条快照"


def test_reuse_is_one_shot(tmp_path, monkeypatch):
    """缓存取走即删。留久了就变成「跳过抓取拿旧数据」——比多抓一次糟得多。"""
    adapter = _CountingAdapter()
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)
    store = Store(tmp_path / "radar.db")
    task = TaskConfig(id="t1", adapter="counting", interval_seconds=600)

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[],
        ) as monitor:
            await monitor.poll_once(task, notify=False)
            snapshot = store.latest_snapshot(task.id)
            assert snapshot is not None
            monitor.reuse_next(task.id, snapshot)
            await monitor.poll_once(task, notify=False)
            await monitor.poll_once(task, notify=False)

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert adapter.calls == 2


def test_reuse_of_fresh_snapshot_produces_no_events(tmp_path, monkeypatch):
    """复用同一份快照去 diff，结果必须为空。

    要是这里冒出事件，启动瞬间就会推一条「余票出现」——
    而变化的其实只是「我们刚刚才第一次看见它」。
    """
    adapter = _CountingAdapter()
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)
    store = Store(tmp_path / "radar.db")
    task = TaskConfig(id="t1", adapter="counting", interval_seconds=600)

    async def go():
        async with Monitor(
            AppConfig(),
            store,
            client=mock_client(lambda request: httpx.Response(200)),
            notifiers=[],
        ) as monitor:
            await monitor.poll_once(task, notify=False)
            snapshot = store.latest_snapshot(task.id)
            assert snapshot is not None
            monitor.reuse_next(task.id, snapshot)
            # notify=True：万一日志被判成变化，这里就会推出去
            return await monitor.poll_once(task, notify=True)

    try:
        changes = asyncio.run(go())
    finally:
        store.close()

    assert changes == []


# --- 「跑一轮就退出」必须真的退出 ------------------------------------------


class _FlakyAdapter(Adapter):
    """前 ``fail_times`` 次抛错，之后成功。用来验证退避与「不重试」两条路。"""

    name = "flaky"
    min_interval = 0.0

    def __init__(self, fail_times: int = 1) -> None:
        super().__init__({})
        self.fail_times = fail_times
        self.calls = 0

    async def fetch(self, task, client):  # noqa: ARG002
        from radar.adapters.base import AdapterError

        self.calls += 1
        if self.calls <= self.fail_times:
            raise AdapterError("cookie 过期")
        return make_snapshot(task_id=task.id, platform=self.name, trains={"G1": {"二等座": 5}})


def _monitor(tmp_path, monkeypatch, adapter):
    monkeypatch.setattr("radar.engine.create_adapter", lambda *a, **k: adapter)
    store = Store(tmp_path / "radar.db")
    return store, Monitor(
        AppConfig(), store, client=mock_client(lambda request: httpx.Response(200)), notifiers=[]
    )


def test_run_once_gives_up_after_one_attempt(tmp_path, monkeypatch):
    """``--once`` 承诺「跑一轮就退出」，永久失败也不能例外。

    实测撞上过：大麦 Cookie 过期，``radar run --once`` 在日志里反复打
    「30s 后重试」，三分钟不返回。看着像慢，其实是永远不会停——
    命令自己的承诺，得由命令自己兑现（``retries=0``）。
    """
    adapter = _FlakyAdapter(fail_times=99)
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    task = TaskConfig(id="t1", adapter="flaky", interval_seconds=600)

    async def go():
        async with monitor:
            # 5 秒内必须返回；真去重试的话第一次就要睡 30 秒
            await asyncio.wait_for(
                monitor._poll_with_backoff(task, notify=False, retries=0), timeout=5
            )

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert adapter.calls == 1, "retries=0 就该只试一次"


def test_constant_monitor_keeps_retrying_until_it_works(tmp_path, monkeypatch):
    """常驻监控相反：失败不能放弃任务，只是拉长间隔。两条路都要钉住。"""
    monkeypatch.setattr("radar.engine.BASE_BACKOFF_SECONDS", 0.0)
    adapter = _FlakyAdapter(fail_times=2)
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    task = TaskConfig(id="t1", adapter="flaky", interval_seconds=600)

    async def go():
        async with monitor:
            await asyncio.wait_for(
                monitor._poll_with_backoff(task, notify=False), timeout=5
            )

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert adapter.calls == 3, "第三次才成功，说明前两次确实重试了"


# --- 故障分级 --------------------------------------------------------------


def test_classify_error_maps_each_subtype():
    """四类错误各自归到正确的 (标签, kind, 提示)。

    这是「会自检」里「故障分级」的核心：推送里能不能给出下一步，全靠这层。
    """
    cases = [
        (AuthError("cookie 过期"), ("登录态失效", "auth", "请重新登录：radar login <平台>")),
        (RateLimitedError("429"), ("被平台限流", "rate_limited", "引擎已自动退避，无需操作")),
        (TransportError("连接超时"), ("网络异常", "transport", "引擎正在自动重试")),
        (ParseError("结构变了"), ("解析失败", "parse", "多半是上游改了结构，需升级适配器")),
        (AdapterError("其它"), ("抓取失败", "adapter", "多半是平台结构变了，需维护者介入")),
    ]
    for exc, (label, kind, hint) in cases:
        got = _classify_error(exc)
        assert got == (label, kind, hint), f"{type(exc).__name__} -> {got}"


def test_auth_failure_triggers_user_alert(tmp_path, monkeypatch):
    """登录态失效是大麦 Cookie 过期那种——不主动叫人，用户会以为还活着。

    钉死：抛出 AuthError 时，监控要**推一条告警**给用户，而不是默默退避。
    """

    class _AuthFail(Adapter):
        name = "authfail"
        min_interval = 0.0

        async def fetch(self, task, client):  # noqa: ARG002
            raise AuthError("大麦令牌过期")

    adapter = _AuthFail()
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    capture = _Capture()
    monitor._notifiers = [capture]
    monitor._hub = __import__("radar.notifier", fromlist=["NotifierHub"]).NotifierHub([capture])
    task = TaskConfig(id="t1", adapter="authfail", interval_seconds=600)

    async def go():
        async with monitor:
            await asyncio.wait_for(
                monitor._poll_with_backoff(task, notify=True, retries=0), timeout=5
            )

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert any("需要你操作" in m.title for m in capture.sent), capture.sent
    assert monitor.stats["t1"]["auth_alerted"] is True
    assert monitor.stats["t1"]["consecutive_errors"] == 1


def test_broken_selfcheck_triggers_adapter_alert(tmp_path, monkeypatch):
    """解析疑似失效（BROKEN）时，主动推一条「适配器可能失效」，而非沉默。

    这是「会自检」招牌里最关键的一条：抓到了但抓得不可信，必须叫人。
    """

    class _BrokenParse(Adapter):
        name = "brokenparse"
        min_interval = 0.0

        async def fetch(self, task, client):  # noqa: ARG002
            # 所有席别都成了占位符「未知」→ check_snapshot 判 BROKEN
            return make_snapshot(
                task_id=task.id, platform=self.name, trains={"G1": {"未知": 0}}
            )

    adapter = _BrokenParse()
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    capture = _Capture()
    monitor._notifiers = [capture]
    monitor._hub = __import__("radar.notifier", fromlist=["NotifierHub"]).NotifierHub([capture])
    task = TaskConfig(id="t1", adapter="brokenparse", interval_seconds=600)

    async def go():
        async with monitor:
            await asyncio.wait_for(monitor.poll_once(task, notify=True), timeout=5)

    try:
        asyncio.run(go())
    finally:
        store.close()

    assert any("适配器可能失效" in m.title for m in capture.sent), capture.sent
    assert monitor.stats["t1"]["broken_alerted"] is True
    assert monitor.stats["t1"]["last_verdict"] is Verdict.BROKEN


def test_health_summary_reflects_last_success(tmp_path, monkeypatch):
    """成功一轮后，health_summary 要反映「最近成功时间 / 条目数 / 自检结论」。"""
    adapter = _CountingAdapter()
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    task = TaskConfig(id="t1", adapter="counting", interval_seconds=600)

    async def go():
        async with monitor:
            await asyncio.wait_for(monitor.poll_once(task, notify=False), timeout=5)

    try:
        asyncio.run(go())
    finally:
        store.close()

    rows = monitor.health_summary()
    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == "t1"
    assert row.last_success is not None
    assert row.items == 1
    assert row.verdict is Verdict.HEALTHY
    assert row.consecutive_errors == 0


def test_heartbeat_sends_summary_without_errors(tmp_path, monkeypatch):
    """健康心跳把「我还活着 + 各任务状态」推一条。没故障时应是「正常」。"""
    adapter = _CountingAdapter()
    store, monitor = _monitor(tmp_path, monkeypatch, adapter)
    capture = _Capture()
    monitor._notifiers = [capture]
    monitor._hub = __import__("radar.notifier", fromlist=["NotifierHub"]).NotifierHub([capture])
    task = TaskConfig(id="t1", adapter="counting", interval_seconds=600)

    async def go():
        async with monitor:
            await asyncio.wait_for(monitor.poll_once(task, notify=True), timeout=5)
            await monitor._send_heartbeat()

    try:
        asyncio.run(go())
    finally:
        store.close()

    heartbeats = [m for m in capture.sent if m.title.startswith("【余票监控】健康检查")]
    assert heartbeats, capture.sent
    assert "正常" in heartbeats[0].body


def test_record_failure_feeds_health_summary(tmp_path, monkeypatch):
    """预热绕开了 `_poll_with_backoff`，失败也必须记进健康统计。

    真踩过的坑（2026-09-30 真机跑 `radar health` 撞见）：预热为了「快」直接调
    ``poll_once``，失败没进 stat，于是健康看板把一个**登录态失效**的大麦任务
    显示成「正常　最近 —　0 个条目」。抓错了还报平安——偏偏是本项目招牌功能
    要消灭的那类故障。这条测试就是钉死它的。
    """
    store, monitor = _monitor(tmp_path, monkeypatch, _CountingAdapter())
    monitor.record_failure("t1", AuthError("令牌过期"))

    row = monitor.health_summary()[0]
    assert row.consecutive_errors == 1
    assert row.last_error_kind == "auth"
    assert "异常" in row.status_line()
    assert "正常" not in row.status_line()
    store.close()


def test_never_succeeded_task_is_not_reported_normal():
    """从没成功抓过的任务不能显示「正常」——没有成功时间却说正常是骗人。"""
    row = HealthRow(
        task_id="t1",
        name="t1",
        last_success=None,
        verdict=None,
        items=0,
        consecutive_errors=0,
        last_error_kind=None,
        last_error=None,
    )
    assert "正常" not in row.status_line()
    assert "尚未成功抓取" in row.status_line()


# --- 通知长度：手机一屏放不下 ---------------------------------------------


def test_format_message_leads_with_how_many_trains_changed():
    """多个车次同时变化时，第一句就该说「动了几个」，而不是先列车次号。

    实测过一次同时放票 39 趟车：用户看到的是一屏车次号，
    真正想要的「有 39 个车次有变化」被埋在中间甚至看不到。
    """
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    trains = {f"G{i:04d}": {"二等座": 3} for i in range(1, 4)}
    prev = make_snapshot(trains={c: {"二等座": 0} for c in trains})
    curr = make_snapshot(trains=trains)
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    body = format_message(task, changes, curr).body
    assert "**3 个车次有变化**" in body
    # 结论必须排在明细前面
    assert body.index("3 个车次有变化") < body.index("G0001")


def test_format_message_caps_details_and_points_to_full_list():
    """超过上限的明细要截断，并告诉用户去哪儿看全部。

    一次放票 200 张就是 400 行——手机推送会被系统折叠成「点击展开」，
    折叠起来的那一屏里可能一条有效信息都没有。
    """
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    n = 14
    trains = {f"G{i:04d}": {"二等座": 2} for i in range(1, n + 1)}
    prev = make_snapshot(trains={c: {"二等座": 0} for c in trains})
    curr = make_snapshot(trains=trains)
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    body = format_message(task, changes, curr).body
    assert f"**{n} 个车次有变化**" in body
    # 明细数受控
    assert body.count("（余票出现）") <= MAX_MESSAGE_TRAINS
    # 被截掉的部分要给出下一步
    assert f"还有 {n - MAX_MESSAGE_TRAINS} 个车次" in body
    assert "radar status" in body


def test_format_message_ranks_best_changes_first():
    """余票多的排前面——用户最想知道的是「哪趟最好买」。"""
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    trains = {"G0001": {"二等座": 1}, "G0002": {"二等座": 9}, "G0003": {"二等座": 5}}
    prev = make_snapshot(trains={c: {"二等座": 0} for c in trains})
    curr = make_snapshot(trains=trains)
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    body = format_message(task, changes, curr).body
    assert body.index("G0002") < body.index("G0003") < body.index("G0001")


def test_format_message_keeps_all_details_when_few():
    """只有一两个变化时不该截断——该给全给。"""
    task = TaskConfig(id="t1", adapter="rail12306", params={"from": "A", "to": "B"})
    prev = make_snapshot(trains={"G1": {"二等座": 0}, "G2": {"二等座": 0}})
    curr = make_snapshot(trains={"G1": {"二等座": 6}, "G2": {"二等座": 3}})
    changes = ChangeDetector.diff(prev, curr, ALL_SEATS)

    body = format_message(task, changes, curr).body
    assert "G1" in body and "G2" in body
    assert "radar status" not in body, "只有 2 个变化时不该提示「还有更多」"
