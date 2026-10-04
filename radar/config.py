"""配置加载与校验。

三个设计要点
------------
1. **自动读取 .env**：``load_config`` 会先加载配置文件同目录（或当前工作
   目录）的 ``.env``，再插值。用户按 README 把密钥填进 ``.env`` 就能生效，
   不需要每次手动 ``export``。已存在的环境变量**优先**，不被文件覆盖。

2. **环境变量插值**：配置里写 ``${VAR}``，加载时替换为同名环境变量。
   这样 tasks.yaml 本身不含明文密钥，可以放心提交到公开仓库。

3. **合规下限硬校验**：``interval_seconds`` 不得低于 60 秒。
   这是本项目最重要的一条约束，把它写成 Pydantic validator 而不是
   文档里的一句话——写小了直接抛异常，不给「不小心调成 5 秒」的机会。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import EventKind

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 单任务轮询间隔的合规下限（秒）。低于此值会被拒绝加载。
MIN_INTERVAL_SECONDS = 60

#: 自动加载的密钥文件名。
DOTENV_FILENAME = ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """解析 ``.env`` 文本，返回键值对。

    只支持最常见的 ``KEY=VALUE`` 形态（兼容 ``export KEY=VALUE``、引号值、
    行尾 ``# 注释``）。刻意不引入 python-dotenv：这个项目的密钥读取路径
    应该短到能一眼看穿。
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _DOTENV_KEY.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # 引号包裹：内容原样保留，允许里面出现 # 和空格
            value = value[1:-1]
        else:
            # 未加引号：`` #`` 之后视为注释（要求前置空白，避免截断 C:\a#b 这类值）
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[key] = value
    return result


def find_dotenv(*directories: str | Path) -> Path | None:
    """按给定顺序找到第一个存在的 ``.env``。"""
    for directory in directories:
        candidate = Path(directory) / DOTENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: str | Path, *, override: bool = False) -> int:
    """把 ``.env`` 读进 ``os.environ``，返回实际写入的键数。

    ``override=False``（默认）时不覆盖已存在的环境变量——CI 注入的值、
    或临时 ``SERVERCHAN_SENDKEY=xxx radar run`` 都优先于文件。
    """
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        # utf-8-sig：吃掉 Windows 记事本保存时可能带上的 BOM，
        # 否则第一个键会变成 "\ufeffNTFY_TOPIC" 而静默失效。
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return 0

    loaded = 0
    for key, value in parse_dotenv(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def bootstrap_dotenv(config_path: str | Path | None = None) -> Path | None:
    """找到并加载 ``.env``，返回生效的文件路径（没有则 ``None``）。

    查找顺序：配置文件所在目录 → 当前工作目录。
    这样无论从项目根跑 ``radar -c tasks.yaml``，还是从别处跑
    ``radar -c D:\\somewhere\\tasks.yaml``，都能找到对应的密钥文件。
    """
    directories: list[Path] = []
    if config_path is not None:
        directories.append(Path(config_path).resolve().parent)
    directories.append(Path.cwd())

    found = find_dotenv(*directories)
    if found is not None:
        load_dotenv(found)
    return found


def _interpolate(value: Any) -> Any:
    """递归把 ``${VAR}`` 替换成环境变量值；变量不存在时替换为空串。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


#: ``HH:MM``，小时允许 1 位（``6:00`` 归一成 ``06:00``）
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class WatchRule(BaseModel):
    """关注规则：决定「哪些变化值得推送」。"""

    seat_types: list[str] = Field(
        default_factory=list, description="关注的席别；空列表表示全部"
    )
    min_count: int = Field(default=1, ge=1, description="至少多少张才提醒")
    train_codes: list[str] = Field(
        default_factory=list, description="关注的车次；空列表表示全部。支持 'G1*' 前缀通配"
    )
    depart_after: str | None = Field(
        default=None,
        description="只看发车时刻不早于这个点的车（含），如 '06:00'。留空表示不限",
    )
    depart_before: str | None = Field(
        default=None,
        description="只看发车时刻早于这个点的车（不含），如 '12:00'。留空表示不限",
    )
    notify_on: list[EventKind] = Field(
        default_factory=lambda: [EventKind.APPEARED],
        description="触发推送的事件类型",
    )

    def matches_train(self, train_code: str) -> bool:
        if not self.train_codes:
            return True
        code = train_code.upper()
        for pattern in self.train_codes:
            p = pattern.upper()
            if p.endswith("*"):
                if code.startswith(p[:-1]):
                    return True
            elif code == p:
                return True
        return False

    def matches_seat(self, seat_type: str) -> bool:
        return not self.seat_types or seat_type in self.seat_types

    def matches_depart_time(self, depart_time: str) -> bool:
        """发车时刻是否落在配置的时间窗内。

        取半开区间 ``[depart_after, depart_before)``，
        所以 ``depart_after: '06:00'`` + ``depart_before: '12:00'`` 正好是「整个上午」，
        而不用去纠结 12:00 整算不算上午。

        拿不到发车时刻时**放行**：时间窗是用来收窄 12306 这类有明确时刻的数据的。
        对本来就没有时刻概念的适配器（演出票、二手票），卡死只会让监控静默失效——
        宁可真推多了，也不要一条都不响。
        """
        if not depart_time:
            return True
        if self.depart_after and depart_time < self.depart_after:
            return False
        return not (self.depart_before and depart_time >= self.depart_before)

    @field_validator("depart_after", "depart_before", mode="before")
    @classmethod
    def _normalize_hhmm(cls, v: Any) -> Any:
        """校验并归一化成 ``HH:MM``。

        时间窗是靠**字符串比较**实现的（``'06:00' <= '10:30' < '12:00'``），
        这只有在等宽零填充时才成立：``'6:00' > '10:30'``，
        一个没补零的写法就能让上午的车全部漏掉，且不报错。
        所以这里直接拒绝并归一化，不留静默出错的空间。
        """
        if v is None or v == "":
            return None
        text = str(v).strip()
        m = HHMM_RE.match(text)
        if not m:
            raise ValueError(f"发车时刻要写成 HH:MM（例如 '06:00'），收到 {v!r}")
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    def should_notify(self, kind: EventKind) -> bool:
        return kind in self.notify_on

    @field_validator("notify_on", mode="before")
    @classmethod
    def _coerce_events(cls, v: Any) -> Any:
        if v is None:
            return [EventKind.APPEARED]
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            return [EventKind(x) if isinstance(x, str) else x for x in v]
        return v


class TaskConfig(BaseModel):
    """一个监控任务。"""

    id: str
    name: str = ""
    adapter: str
    enabled: bool = True
    interval_seconds: int = 300
    jitter_seconds: int = Field(default=15, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)
    watch: WatchRule = Field(default_factory=WatchRule)
    credentials: str | None = None
    link: str | None = Field(
        default=None, description="推送里附带的官方链接，供用户自己跳转下单"
    )

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @field_validator("interval_seconds")
    @classmethod
    def _enforce_floor(cls, v: int) -> int:
        if v < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval_seconds={v} 低于合规下限 {MIN_INTERVAL_SECONDS} 秒。\n"
                "本项目的定位是低频只读的余票信息聚合，不是高频抢票工具。\n"
                "把轮询压到秒级、再加并发和账号池，性质就变成了自动刷票——\n"
                "这正是 2026 年 4 月中央网信办与国铁集团联合约谈第三方平台时\n"
                "所禁止的行为。请勿下调此下限。"
            )
        return v


class NotifyConfig(BaseModel):
    """一条通知渠道配置。"""

    # 按「推荐优先级」排：前两个零注册、永久免费，是开源项目的默认选择。
    # 新增渠道时**必须**同步这里与 notifier/registry 的注册表——
    # tests/test_notifier.py 有一条测试专门盯这个，漏改会立刻红灯。
    type: Literal[
        "ntfy",
        "bark",
        "wecom",
        "dingtalk",
        "pushplus",
        "serverchan",
        "telegram",
        "smtp",
    ]
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class StorageConfig(BaseModel):
    path: str = "./data/radar.db"


class AppConfig(BaseModel):
    """顶层配置。"""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    tasks: list[TaskConfig] = Field(default_factory=list)
    notify: list[NotifyConfig] = Field(default_factory=list)
    credentials: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: 健康心跳间隔（秒）。常驻监控每隔这么久推一条「各任务都还好」的汇总，
    #: 把「安静 = 没变化」和「安静 = 程序挂了」这两件事分开。设 0 关闭。
    #: 默认 6 小时：足够证明进程活着，又不至于刷屏。CLI 里 ``radar health``
    #: 可以随时手动要一条，不必为它调小这个间隔。
    heartbeat_interval_seconds: int = Field(default=21600, ge=0)

    @property
    def enabled_tasks(self) -> list[TaskConfig]:
        return [t for t in self.tasks if t.enabled]

    def task(self, task_id: str) -> TaskConfig:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(f"未找到任务：{task_id}")

    def credentials_for(self, task: TaskConfig) -> dict[str, str]:
        if not task.credentials:
            return {}
        return dict(self.credentials.get(task.credentials, {}))

    def notifiers_enabled(self) -> list[NotifyConfig]:
        return [n for n in self.notify if n.enabled]


def load_config(path: str | Path, *, dotenv: bool = True) -> AppConfig:
    """读取 YAML 配置，先加载 ``.env``，再插值环境变量后校验。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在：{p}")
    if dotenv:
        bootstrap_dotenv(p)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(_interpolate(raw))


def dump_example_config(path: str | Path) -> Path:
    """把示例配置写到指定路径。"""
    p = Path(path)
    template = Path(__file__).resolve().parent.parent / "tasks.example.yaml"
    if template.exists():
        p.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return p


__all__ = [
    "DOTENV_FILENAME",
    "MIN_INTERVAL_SECONDS",
    "AppConfig",
    "NotifyConfig",
    "StorageConfig",
    "TaskConfig",
    "WatchRule",
    "bootstrap_dotenv",
    "dump_example_config",
    "find_dotenv",
    "load_config",
    "load_dotenv",
    "parse_dotenv",
]
