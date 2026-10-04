"""配置层测试：环境变量插值、合规下限、凭据解析。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from radar.config import (
    MIN_INTERVAL_SECONDS,
    AppConfig,
    TaskConfig,
    WatchRule,
    load_config,
)
from radar.models import EventKind

SAMPLE = """
storage:
  path: ./data/test.db

notify:
  - type: serverchan
    enabled: true
    options:
      sendkey: ${TEST_SENDKEY}

credentials:
  rail:
    cookie: ${TEST_COOKIE}

tasks:
  - id: t1
    adapter: rail12306
    interval_seconds: 120
    credentials: rail
    params:
      from: 北京南
      to: 上海虹桥
      date: +7
    watch:
      seat_types: ["二等座"]
      min_count: 2
      train_codes: ["G1"]
      notify_on: ["appeared", "increased"]
"""


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "tasks.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_env_interpolation(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SENDKEY", "SCT123")
    monkeypatch.setenv("TEST_COOKIE", "JSESSIONID=abc")

    cfg = load_config(config_file)

    assert cfg.notify[0].options["sendkey"] == "SCT123"
    assert cfg.credentials["rail"]["cookie"] == "JSESSIONID=abc"


def test_missing_env_becomes_empty_string(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TEST_SENDKEY", raising=False)
    monkeypatch.delenv("TEST_COOKIE", raising=False)

    cfg = load_config(config_file)

    # 变量不存在时替换成空串，而不是留着 "${TEST_SENDKEY}" 字面量
    assert cfg.notify[0].options["sendkey"] == ""


def test_task_parsed(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SENDKEY", "x")
    monkeypatch.setenv("TEST_COOKIE", "y")

    cfg = load_config(config_file)
    task = cfg.task("t1")

    assert task.adapter == "rail12306"
    assert task.params["from"] == "北京南"
    assert task.interval_seconds == 120
    assert task.watch.seat_types == ["二等座"]
    assert task.watch.min_count == 2
    assert task.watch.notify_on == [EventKind.APPEARED, EventKind.INCREASED]
    assert cfg.credentials_for(task) == {"cookie": "y"}


def test_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


# --- 合规下限：本项目最重要的一条测试 -------------------------------------


@pytest.mark.parametrize("seconds", [1, 5, 30, 59])
def test_interval_below_floor_is_rejected(seconds: int):
    """低于 60 秒的轮询间隔必须直接拒绝加载。

    这条测试是项目的合规护栏。破坏它等于把这个仓库变成抢票工具。
    """
    with pytest.raises(ValidationError) as exc:
        TaskConfig(id="t", adapter="rail12306", interval_seconds=seconds)
    assert "合规下限" in str(exc.value)


def test_interval_at_floor_is_allowed():
    task = TaskConfig(id="t", adapter="rail12306", interval_seconds=MIN_INTERVAL_SECONDS)
    assert task.interval_seconds == 60


def test_default_interval_is_conservative():
    """默认值应该偏保守，而不是踩在红线上。"""
    task = TaskConfig(id="t", adapter="rail12306")
    assert task.interval_seconds >= 300


# --- 关注规则 --------------------------------------------------------------


def test_watch_rule_defaults():
    rule = WatchRule()
    assert rule.seat_types == []
    assert rule.min_count == 1
    assert rule.notify_on == [EventKind.APPEARED]
    assert rule.matches_train("任意车次") is True
    assert rule.matches_seat("任意席别") is True


def test_watch_rule_notify_on_accepts_bare_string():
    """YAML 里写 notify_on: appeared 也要能解析。"""
    rule = WatchRule(notify_on="appeared")
    assert rule.notify_on == [EventKind.APPEARED]


def test_watch_rule_rejects_unknown_event():
    with pytest.raises(ValidationError):
        WatchRule(notify_on=["not_a_real_event"])


def test_enabled_tasks_filter():
    cfg = AppConfig(
        tasks=[
            TaskConfig(id="on", adapter="rail12306", enabled=True),
            TaskConfig(id="off", adapter="rail12306", enabled=False),
        ]
    )
    assert [t.id for t in cfg.enabled_tasks] == ["on"]


def test_credentials_for_unknown_key_is_empty():
    cfg = AppConfig(credentials={})
    task = TaskConfig(id="t", adapter="rail12306", credentials="missing")
    assert cfg.credentials_for(task) == {}


def test_credentials_for_none_is_empty():
    cfg = AppConfig()
    task = TaskConfig(id="t", adapter="rail12306")
    assert cfg.credentials_for(task) == {}


# --- 发车时间窗：「我只关心上午出发的车」-----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06:00", "06:00"),
        ("6:00", "06:00"),  # 不补零也能写，但内部一定会归一化
        ("00:00", "00:00"),
        ("23:59", "23:59"),
        (" 12:30 ", "12:30"),  # YAML 里手写常带空格，顺手容忍
        (None, None),
        ("", None),
    ],
)
def test_depart_bound_normalizes_to_hhmm(raw, expected):
    """时间窗靠字符串比较，`'6:00' > '10:30'` 会让上午的车全部漏掉。

    所以补零不是风格问题，是正确性问题——这里钉死它一定会被归一化。
    """
    assert WatchRule(depart_after=raw).depart_after == expected
    assert WatchRule(depart_before=raw).depart_before == expected


@pytest.mark.parametrize("bad", ["25:00", "12:60", "0600", "12点", "上午", "12:5", "9"])
def test_depart_bound_rejects_malformed(bad):
    """写错就直接报错，不给「静默筛不出车」的机会。"""
    with pytest.raises(ValidationError):
        WatchRule(depart_after=bad)


def test_matches_depart_time_window_is_half_open():
    """区间是 [after, before)：06:00 算在内，12:00 不算。

    半开区间让「上午」可以直白地写成 06:00 ~ 12:00，
    不用去纠结 12:00 整到底算不算上午。
    """
    rule = WatchRule(depart_after="06:00", depart_before="12:00")

    assert rule.matches_depart_time("06:00") is True
    assert rule.matches_depart_time("11:59") is True
    assert rule.matches_depart_time("05:59") is False
    assert rule.matches_depart_time("12:00") is False
    assert rule.matches_depart_time("23:00") is False


def test_matches_depart_time_boundaries_are_optional():
    assert WatchRule(depart_after="09:00").matches_depart_time("06:00") is False
    assert WatchRule(depart_after="09:00").matches_depart_time("13:00") is True
    assert WatchRule(depart_before="09:00").matches_depart_time("08:00") is True
    assert WatchRule(depart_before="09:00").matches_depart_time("10:00") is False


def test_matches_depart_time_without_window_allows_everything():
    assert WatchRule().matches_depart_time("03:00") is True


def test_matches_depart_time_passes_when_time_unknown():
    """演出票这类适配器给不出发车时刻；卡死会让监控静默失效，所以放行。

    宁可多推一条，也不要一条都不响——静默失效比噪音难查得多。
    """
    rule = WatchRule(depart_after="06:00", depart_before="12:00")
    assert rule.matches_depart_time("") is True
