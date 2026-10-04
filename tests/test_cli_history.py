"""``radar history`` 的展示层测试。

入库统一用 UTC（跨机器、跨时区都不歧义），但给人看必须转本地时区。
以前这里是把 ISO 串直接截断，于是 UTC+8 的用户会看到一条写着 8 小时前的
「历史」，第一反应是程序记错了——所以这个转换值得钉住。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from radar.cli import _local_time

UTC_STAMP = "2026-09-29T15:03:26+00:00"


def test_local_time_converts_utc_to_local():
    """结果必须等同于「把同一时刻换算到本地时区」。

    这样断言与具体时区无关，在任何 CI 上跑都成立。
    """
    expected = datetime.fromisoformat(UTC_STAMP).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert _local_time(UTC_STAMP) == expected


def test_local_time_is_not_raw_utc():
    """回归保护：以前直接把 ISO 串截断，显示的是 UTC 时间本身。"""
    if datetime.now().astimezone().utcoffset() == timedelta(0):
        pytest.skip("本地时区就是 UTC，无法区分转了还是没转")
    assert _local_time(UTC_STAMP) != "2026-09-29 15:03:26"


def test_local_time_reads_naive_timestamp_as_utc():
    """老库里的时间戳可能不带时区，按 UTC 解释——这正是入库时的约定。"""
    assert _local_time("2026-09-29T15:03:26") == _local_time(UTC_STAMP)


def test_local_time_survives_garbage():
    """展示层不该因为一个脏时间戳直接崩掉。"""
    assert _local_time("not-a-time") == "not-a-time"
    assert _local_time("") == ""


def test_local_time_truncates_overlong_garbage():
    assert len(_local_time("x" * 60)) == 19
