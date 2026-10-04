"""语义自检：抓到了，但抓得讲不讲得通？

监控工具最危险的失败不是「抓不到」，而是「抓错了还报平安」。
本文件只测 :func:`radar.selfcheck.check_snapshot` 的三档判定，
不碰网络、不碰平台知识——纯靠快照内部结构。
"""

from __future__ import annotations

import datetime as dt

from radar.selfcheck import SUSPICIOUS_SEAT_MARKERS, Verdict, check_snapshot
from tests.conftest import make_snapshot


def _when() -> dt.datetime:
    return dt.datetime(2026, 10, 6, 9, 0, tzinfo=dt.timezone.utc)


def test_healthy_when_seats_parse_normally():
    """票档席别名都正常 → 健康。最基本的「没问题」路径。"""
    snap = make_snapshot(trains={"G1": {"二等座": 5, "一等座": None}}, when=_when())
    report = check_snapshot(snap, previous=None)
    assert report.verdict is Verdict.HEALTHY
    assert report.ok
    assert report.reasons == []


def test_degraded_when_some_seats_are_suspicious():
    """部分票档解析异常（席别名成了占位符），但还有正常的 → 可疑。

    真没票时票档仍以「无票」形式存在，席别名不会丢。所以「一部分废了」
    更可能是解析器手抖，而不是整场售罄——判 DEGRADED 而非 BROKEN。
    """
    snap = make_snapshot(trains={"G1": {"二等座": 5, "未知": 0}}, when=_when())
    report = check_snapshot(snap, previous=None)
    assert report.verdict is Verdict.DEGRADED
    assert report.ok  # DEGRADED 仍然可用，只是可疑
    assert "1/2" in report.describe()  # 1 个可疑 / 共 2 个


def test_broken_when_all_seats_are_suspicious():
    """所有票档都解析成占位符 → 几乎可以确定解析器失效。

    这才是要主动叫人的那一类：平时能抠出 20 个席别，某天全成了「未知」，
    不是真没票（真没票席别名还在），是上游改了结构 / 被弹回登录页。
    """
    snap = make_snapshot(trains={"G1": {"未知": 0}}, when=_when())
    report = check_snapshot(snap, previous=None)
    assert report.verdict is Verdict.BROKEN
    assert not report.ok  # BROKEN 不可用
    assert "解析器很可能失效" in report.describe()


def test_degraded_when_trains_vanish_after_having_data():
    """上一轮有数据，本轮突然 0 个 → 可疑（风控 / 登录页 / 结构变了）。"""
    prev = make_snapshot(trains={"G1": {"二等座": 5}}, when=_when())
    curr = make_snapshot(trains={}, when=_when())
    report = check_snapshot(curr, previous=prev)
    assert report.verdict is Verdict.DEGRADED
    assert "突然 0 个" in report.describe()


def test_healthy_when_empty_and_no_history():
    """首轮就 0 个条目，且无历史 → 健康（可能确实没开卖 / 没挂单）。"""
    curr = make_snapshot(trains={}, when=_when())
    report = check_snapshot(curr, previous=None)
    assert report.verdict is Verdict.HEALTHY


def test_markers_are_consistent_with_models():
    """占位的席别名集合必须和文档承诺一致：不含「无」「售罄」这类真值。

    若有人把「无」误加进标记集，真没票的场次会被误判成解析失败，
    监控反过来漏报——这条断言就是挡这个的。
    """
    assert "无" not in SUSPICIOUS_SEAT_MARKERS
    assert "售罄" not in SUSPICIOUS_SEAT_MARKERS
    assert "未知" in SUSPICIOUS_SEAT_MARKERS
