"""通知渠道注册表与工厂。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from .base import NotConfiguredError, Notifier

if TYPE_CHECKING:
    from ..config import AppConfig

log = logging.getLogger("radar.notifier")

_REGISTRY: dict[str, type[Notifier]] = {}

#: 零注册、零成本、永久免费的渠道。README 与 `radar channels` 都引用这个顺序。
RECOMMENDED_FREE: tuple[str, ...] = ("ntfy", "bark", "wecom", "dingtalk")


def register_notifier(name: str) -> Callable[[type[Notifier]], type[Notifier]]:
    def decorator(cls: type[Notifier]) -> type[Notifier]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def list_notifiers() -> list[str]:
    return sorted(_REGISTRY)


def create_notifier(
    name: str, options: dict | None = None, client: httpx.AsyncClient | None = None
) -> Notifier:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的通知渠道：{name!r}。可用：{sorted(_REGISTRY)}")
    return _REGISTRY[name](options, client)


def build_notifiers(config: AppConfig, client: httpx.AsyncClient) -> list[Notifier]:
    """按配置构造所有启用的渠道。

    - 凭据为空 → 静默跳过（``NotConfiguredError``），这是开源项目的常态：
      用户 clone 下来时示例配置开着好几个渠道，但只填了其中一个。
    - 配置写错（比如 webhook 不是 URL）→ 记警告，但只跳过这一个渠道。
    - 一个都没配成 → 给一次汇总提示，告诉用户怎么用最省事的方式起步。
    """
    notifiers: list[Notifier] = []
    unconfigured: list[str] = []

    for item in config.notifiers_enabled():
        try:
            notifiers.append(create_notifier(item.type, item.options, client))
        except NotConfiguredError:
            unconfigured.append(item.type)
        except Exception as exc:
            log.warning("跳过通知渠道 %s：%s", item.type, exc)

    for notifier in notifiers:
        log.info("已启用通知渠道：%s", notifier.name)

    if unconfigured:
        if notifiers:
            log.debug("这些渠道没填凭据，已跳过：%s", ", ".join(unconfigured))
        else:
            log.warning(
                "所有通知渠道都还没填凭据，余票变化只会写进本地数据库、不会推送。\n"
                "最省事的起步方式是 ntfy：不用注册、不用 API Key，"
                "在 tasks.yaml 里给 ntfy 的 topic 填一个别人猜不到的名字就行。\n"
                "详见 README 的「通知渠道」一节（`radar channels` 可列出全部渠道）。"
            )

    return notifiers


__all__ = [
    "RECOMMENDED_FREE",
    "build_notifiers",
    "create_notifier",
    "list_notifiers",
    "register_notifier",
]
