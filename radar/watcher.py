"""``radar watch`` 的配置写入。

为什么单独成模块：这里的难点不是「生成 YAML」，而是**别把用户已有的
配置搞坏**。而 yaml 这件事有个很现实的坑：

    yaml.safe_load(text) → 修改 → yaml.safe_dump(text)

**会把用户写的注释全部抹掉。** 而本项目的 ``tasks.yaml`` 之所以长成那样，
就是因为每个任务旁边都写了「为什么这么填」「哪些字段能改」——那些注释
是配置的一部分，丢掉它等于把用户重新扔回「读文档才能配」的原点。

所以这里用**文本插入**：解析出 ``tasks:`` 列表的范围，把新任务块插到末尾，
其余部分一个字节都不动。
"""

from __future__ import annotations

import dataclasses
import re

from .intent import ParsedIntent
from .target import Target

#: 任务默认间隔。演出票平台敏感，600 秒起；铁路另说（它自己有平台下限）。
DEFAULT_INTERVAL = 600


@dataclasses.dataclass
class WriteResult:
    """写入结果。要把「发生了什么」如实说清楚，不能只说成功。"""

    ok: bool
    task_id: str = ""
    path: str = ""
    line: int = 0                 # 插入位置（1-indexed）
    reason: str = ""              # 没写成时的原因
    text: str = ""                # 新文件内容


def _yaml_str(value: str) -> str:
    """安全地写进 YAML 的双引号字符串。"""
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ")
    return f'"{escaped}"'


def build_task_block(
    target: Target,
    *,
    task_id: str | None = None,
    interval: int = DEFAULT_INTERVAL,
    display_name: str | None = None,
    link: str = "",
) -> tuple[str, str]:
    """生成一个任务块，返回 ``(yaml 文本, task id)``。"""
    if task_id is None:
        base = target.target_id or target.label or target.platform
        slug = re.sub(r"[^0-9a-zA-Z]+", "-", str(base)).strip("-").lower()[:24]
        task_id = f"{target.platform}-{slug}" if slug else target.platform

    name = display_name or target.label or target.title
    lines = [
        f"  - id: {task_id}",
        f"    name: {_yaml_str(name)}",
        f"    adapter: {target.platform}",
        "    enabled: true",
        f"    interval_seconds: {interval}",
        f"    jitter_seconds: {min(60, max(10, interval // 5))}",
    ]
    if link:
        lines.append(f"    link: {link}")
    # 注意缩进：这些键必须和 adapter / enabled 同级（4 空格）。
    # 写成 2 空格会让它们变成 tasks 列表里的新列表项，YAML 直接解析失败。
    lines.append("    params:")
    for key, value in target.params.items():
        lines.append(f"      {key}: {_yaml_str(str(value))}")
    lines.append("    watch:")
    lines.append("      seat_types: []")
    lines.append("      min_count: 1")
    lines.append('      notify_on: ["appeared"]')
    return "\n".join(lines) + "\n", task_id


def find_tasks_insert_point(text: str) -> tuple[int, int] | None:
    """找出 ``tasks:`` 列表里最后一个列表项的结束位置。

    返回 ``(起始行, 结束行)``，都是 0-indexed、**不含**结束行。
    找不到就返回 ``None``（调用方负责新建整个 ``tasks:`` 段）。
    """
    lines = text.splitlines()
    tasks_at = next(
        (i for i, line in enumerate(lines) if re.match(r"^tasks\s*:", line)),
        None,
    )
    if tasks_at is None:
        return None

    # 先划出 tasks: 这一段的右边界（下一个顶层 key）。
    #
    # 这一步不能省：`- xxx` 这种列表项在 notify: 段里也有（`- channel: ntfy`），
    # 不划边界的话会把它当成「最后一个任务」，新任务就插到 notify 段里去了。
    # 症状很隐蔽——YAML 照样能解析，只是多了一个诡异的顶层键。
    section_end = len(lines)
    for i in range(tasks_at + 1, len(lines)):
        if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i]):
            section_end = i
            break

    item_starts = [
        i
        for i in range(tasks_at + 1, section_end)
        if re.match(r"^\s*-\s+\S", lines[i]) and not lines[i].lstrip().startswith("#")
    ]
    if not item_starts:
        return (tasks_at + 1, tasks_at + 1)

    last = item_starts[-1]
    item_indent = len(lines[last]) - len(lines[last].lstrip())
    end = len(lines)
    for i in range(last + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent < item_indent:
            end = i
            break
    return (last, end)


def existing_task_ids(text: str) -> set[str]:
    """读出已有的 task id，用来判重。"""
    return set(re.findall(r"^\s*-\s+id:\s*(\S+)", text, re.M))


def append_task(
    text: str,
    block: str,
    task_id: str,
    *,
    path: str = "tasks.yaml",
) -> WriteResult:
    """把任务块追加到 ``tasks:`` 列表末尾，**保留原文所有注释**。

    重复 id 直接拒绝写入——同一个任务配两遍，监控会双份推送，
    而用户往往不会立刻发现。
    """
    if task_id in existing_task_ids(text):
        near = [i for i in text.splitlines() if task_id in i]
        return WriteResult(
            ok=False,
            task_id=task_id,
            path=path,
            line=(text[: text.index(near[0])].count("\n") + 1) if near else 0,
            reason="配置里已经有这个 id 了。改配置的话直接编辑原文件，别重复添加",
        )

    spot = find_tasks_insert_point(text)
    if spot is None:
        # 没有 tasks: 段就整体补一个
        addition = "\ntasks:\n" + block
        return WriteResult(ok=True, task_id=task_id, path=path,
                           line=text.count("\n") + 2, text=text.rstrip("\n") + addition)

    start, end = spot
    lines = text.splitlines(keepends=True)
    new_text = "".join(lines[:end]) + block + "".join(lines[end:])
    return WriteResult(ok=True, task_id=task_id, path=path, line=end + 1, text=new_text)


def suggest_interval(intent: ParsedIntent, target: Target) -> int:
    """按平台给一个稳妥的轮询间隔。

    低于平台能承受的频率不但没意义，还会撞限流——12306 实测 1 分钟内
    连抓两次就被 302。所以这里不提供「更激进」的选项。
    """
    if target.platform == "rail12306":
        return 300
    if target.platform == "amadeus":
        return 900
    return DEFAULT_INTERVAL


__all__ = [
    "DEFAULT_INTERVAL",
    "WriteResult",
    "append_task",
    "build_task_block",
    "existing_task_ids",
    "find_tasks_insert_point",
    "suggest_interval",
]
