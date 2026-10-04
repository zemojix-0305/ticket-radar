"""示例配置的集成测试。

存在的意义：新人拿到仓库的第一步就是 `cp tasks.example.yaml tasks.yaml`。
如果示例配置本身加载不了、或者里面的日期写法解析不了，第一步就卡住了。

这个文件里的测试曾经抓到过一个真实 bug：YAML 把不引号的 ``+7``
解析成整数，而 resolve_date 当时只认字符串 ``"+7"``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radar.adapters import available_adapters, create_adapter
from radar.adapters.rail12306 import resolve_date
from radar.config import MIN_INTERVAL_SECONDS, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "tasks.example.yaml"
ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_example_config_file_exists():
    assert EXAMPLE.exists(), "示例配置丢了，README 里的第一步就走不通"


def test_example_config_loads():
    """示例配置必须能直接加载，不能报任何校验错。"""
    cfg = load_config(EXAMPLE)
    assert cfg.tasks, "示例配置里至少应该有一个任务"
    assert cfg.storage.path


def test_every_task_has_a_registered_adapter():
    cfg = load_config(EXAMPLE)
    for task in cfg.tasks:
        assert task.adapter in available_adapters(), (
            f"任务 {task.id} 用了未注册的适配器 {task.adapter}"
        )
        create_adapter(task.adapter, cfg.credentials_for(task))


def test_every_task_date_parses():
    """示例里的 date 写法必须真的能解析——引号漏了就会被 YAML 吃成整数。"""
    cfg = load_config(EXAMPLE)
    for task in cfg.tasks:
        if "date" in task.params:
            resolve_date(task.params["date"])  # 不抛异常即通过


def test_example_intervals_respect_floor():
    cfg = load_config(EXAMPLE)
    for task in cfg.tasks:
        assert task.interval_seconds >= MIN_INTERVAL_SECONDS


def test_example_credentials_keys_resolve():
    """任务引用的 credentials 段必须存在（允许值为空串，只是没填密钥而已）。"""
    cfg = load_config(EXAMPLE)
    for task in cfg.tasks:
        if task.credentials:
            assert task.credentials in cfg.credentials, (
                f"任务 {task.id} 引用了不存在的 credentials：{task.credentials}"
            )


def test_env_example_exists_and_covers_referenced_vars():
    """.env.example 里应该提到 tasks.example.yaml 用到的每个环境变量。"""
    assert ENV_EXAMPLE.exists()
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    yaml_text = EXAMPLE.read_text(encoding="utf-8")

    import re

    # 先去掉整行注释，否则文档里举例写的 ${VAR} 会被误判成真实引用
    body = "\n".join(
        line for line in yaml_text.splitlines() if not line.lstrip().startswith("#")
    )
    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", body))
    assert referenced, "示例配置里应该用 ${VAR} 占位，而不是写明文密钥"

    missing = {name for name in referenced if name not in env_text}
    assert not missing, f"这些变量没在 .env.example 里说明：{sorted(missing)}"


@pytest.mark.parametrize("spec", ["+7", 7, "7"])
def test_yaml_unquoted_plus_n_is_tolerated(spec):
    """回归测试：裸整数也要能当「N 天后」解析。

    YAML 把不引号的 +7 读成 int 7，不处理的话示例配置第一步就报错。
    """
    import datetime as dt

    expected = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    assert resolve_date(spec) == expected
