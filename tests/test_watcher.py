"""``radar watch`` 配置写入的测试。

这里的风险只有一个：**把用户已有的配置搞坏**。而 YAML 有个很坑的特性：

    yaml.safe_load → 改 → yaml.safe_dump

会把注释全部抹掉。所以本项目用**文本插入**，这些测试就是在盯住
「一个字节都不能动用户原有的东西」这条底线。

全部离线，不发任何请求。
"""

from __future__ import annotations

import yaml

from radar.target import Target
from radar.watcher import (
    append_task,
    build_task_block,
    existing_task_ids,
    find_tasks_insert_point,
    suggest_interval,
)
from radar.intent import parse_intent

SAMPLE = """# 我的配置，上面这段注释不能丢
storage:
  path: ./data/radar.db

credentials:
  damai:
    cookie: ${DAMAI_COOKIE}

tasks:
  # 铁路：每天早上的车
  - id: gz-cs-morning
    name: "广州南 → 长沙南"
    adapter: rail12306
    enabled: true
    interval_seconds: 300
    # 二等座，别的席别不看
    params:
      from: 广州南
      to: 长沙南
      date: "2026-10-06"
    watch:
      seat_types: ["二等座"]
      min_count: 1
      notify_on: ["appeared"]

  - id: dami-demo
    name: 大麦示例
    adapter: damai
    enabled: false
    params:
      item_id: "123"
    watch:
      seat_types: []
      min_count: 1

notify:
  - channel: ntfy
    topic: ${NTFY_TOPIC}
"""


def _target(**kw) -> Target:
    base = {
        "platform": "maoyan",
        "target_id": "501675",
        "label": '陈粒「一粒」十周年巡回演唱会-广州站',
        "params": {"performance_id": "501675"},
    }
    base.update(kw)
    return Target(**base)


# --- 生成的块本身必须是合法 YAML --------------------------------------------


def test_generated_block_parses_and_keeps_fields():
    """缩进错一位 YAML 就崩——`params:` 写成 2 空格会跑到列表外面去。"""
    block, task_id = build_task_block(_target(), link="https://show.maoyan.com/")
    parsed = yaml.safe_load("tasks:\n" + block)
    entry = parsed["tasks"][0]
    assert entry["id"] == task_id
    assert entry["adapter"] == "maoyan"
    assert entry["enabled"] is True
    assert entry["params"]["performance_id"] == "501675"
    assert entry["watch"]["notify_on"] == ["appeared"]


def test_generated_id_is_slugified():
    block, task_id = build_task_block(_target(target_id="6a2a/300f 941d"))
    assert "/" not in task_id and " " not in task_id
    assert yaml.safe_load("tasks:\n" + block)["tasks"][0]["id"] == task_id


def test_label_with_quotes_does_not_break_yaml():
    """演出名里带引号很常见（中文引号没问题，直引号会）。"""
    tricky = 'He said "hi" 陈粒'
    block, _ = build_task_block(_target(label=tricky))
    entry = yaml.safe_load("tasks:\n" + block)["tasks"][0]
    assert entry["name"] == tricky


# --- 插入位置 ---------------------------------------------------------------


def test_finds_insert_point_inside_tasks_list():
    start, end = find_tasks_insert_point(SAMPLE)
    lines = SAMPLE.splitlines()
    # 插入点必须落在 tasks: 之后、notify: 之前
    assert lines[start].strip().startswith("- id:")
    assert end < len(lines)
    assert lines[end].startswith("notify:"), f"插入点算错了：{lines[end]!r}"


def test_handles_config_without_tasks_section():
    assert find_tasks_insert_point("storage:\n  path: x\n") is None


# --- 写入不能破坏已有内容 ---------------------------------------------------


def test_append_preserves_everything_existing():
    """**核心断言**：原有任务、参数、注释一个都不能变。"""
    block, task_id = build_task_block(_target())
    result = append_task(SAMPLE, block, task_id)

    assert result.ok, result.reason
    before, after = yaml.safe_load(SAMPLE), yaml.safe_load(result.text)

    # 老任务原封不动
    assert after["tasks"][: len(before["tasks"])] == before["tasks"]
    # 新任务在最后
    assert after["tasks"][-1]["id"] == task_id
    # 其它顶层段没动
    assert after["storage"] == before["storage"]
    assert after["credentials"] == before["credentials"]
    assert after["notify"] == before["notify"]


def test_append_preserves_comments():
    """注释是配置的一部分。用 yaml 反序列化重写会把它们全抹掉。"""
    block, task_id = build_task_block(_target())
    result = append_task(SAMPLE, block, task_id)

    assert result.text.count("#") == SAMPLE.count("#")
    for line in ("# 我的配置，上面这段注释不能丢", "# 铁路：每天早上的车", "# 二等座，别的席别不看"):
        assert line in result.text, f"注释丢了：{line}"


def test_append_rejects_duplicate_id():
    """同一个任务配两遍会双份推送，而用户往往不会立刻发现。"""
    block, task_id = build_task_block(_target())
    once = append_task(SAMPLE, block, task_id)
    twice = append_task(once.text, block, task_id)

    assert once.ok
    assert not twice.ok
    assert "已经有" in twice.reason
    assert twice.line > 0, "要告诉用户同名任务在哪一行"


def test_duplicate_detection_reads_all_existing_ids():
    ids = existing_task_ids(SAMPLE)
    assert "gz-cs-morning" in ids and "dami-demo" in ids
    assert "不存在的任务" not in ids


# --- 间隔 -------------------------------------------------------------------


def test_interval_never_goes_below_platform_tolerance():
    """12306 实测 1 分钟内连抓两次就被 302，所以不给更激进的选项。"""
    assert suggest_interval(parse_intent("查票"), _target(platform="rail12306")) >= 300
    assert suggest_interval(parse_intent("查票"), _target(platform="maoyan")) >= 600
    assert suggest_interval(parse_intent("查票"), _target(platform="amadeus")) >= 900


def test_user_can_override_interval():
    block, _ = build_task_block(_target(), interval=1200)
    entry = yaml.safe_load("tasks:\n" + block)["tasks"][0]
    assert entry["interval_seconds"] == 1200
