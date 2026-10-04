"""适配器注册表。

用装饰器注册，避免维护一张手工映射表。新增平台零成本接入：
在实现模块上写 ``@register("damai")``，再在 ``__init__.py`` 里 import 即可。
"""

from __future__ import annotations

from collections.abc import Callable

from .base import Adapter

_REGISTRY: dict[str, type[Adapter]] = {}


def register(name: str) -> Callable[[type[Adapter]], type[Adapter]]:
    """把一个 Adapter 子类注册到指定名字下。"""

    def decorator(cls: type[Adapter]) -> type[Adapter]:
        if not name:
            raise ValueError("适配器名不能为空")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_adapter_class(name: str) -> type[Adapter]:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的适配器：{name!r}。可用：{sorted(_REGISTRY)}")
    return _REGISTRY[name]


def create_adapter(name: str, credentials: dict[str, str] | None = None) -> Adapter:
    """按名字实例化适配器。"""
    return get_adapter_class(name)(credentials)


def list_adapters() -> list[str]:
    return sorted(_REGISTRY)


# engine 里用了这个别名，保持一致
available_adapters = list_adapters


__all__ = [
    "available_adapters",
    "create_adapter",
    "get_adapter_class",
    "list_adapters",
    "register",
]
