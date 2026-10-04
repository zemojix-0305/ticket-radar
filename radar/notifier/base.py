"""通知基类。

新增一个渠道只需：继承 ``Notifier``、实现 ``send``、写 ``@register_notifier("名字")``、
在 ``notifier/__init__.py`` 里 import 一下。

所有渠道都必须支持 Markdown 正文的降级——Telegram 认 Markdown，
钉钉认 Markdown，邮件当 HTML 要转义，Server酱也认 Markdown。
基类提供 ``plain_text`` 属性做降级。

「未配置」与「配置错误」的区别
------------------------------
开源项目 clone 下来时，示例配置里往往开着七八个渠道、凭据全是空的。
这不是错误，是**正常状态**——用户只是还没选渠道。

所以 ``_validate`` 抛的是 ``NotConfiguredError``（``ValueError`` 的子类），
由 ``registry.build_notifiers`` 静默跳过；而 options 写了值但格式不对
（比如 webhook 不是 URL），抛的是普通异常，会被记成警告。
两者的日志级别不同，用户一眼能看出自己到底是「没配」还是「配错了」。
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")


class NotConfiguredError(ValueError):
    """渠道未填写必填凭据。

    这不是故障——用户可能根本没打算用这个渠道。
    调用方应当静默跳过，而不是当作错误上报。
    """


@dataclass(frozen=True)
class Message:
    """一条待推送的通知。"""

    title: str
    body: str
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def plain_text(self) -> str:
        """去掉 Markdown 记号后的纯文本，供不支持富文本的渠道降级使用。"""
        text = _MD_LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", self.body)
        text = _MD_BOLD.sub(r"\1", text)
        return text


class Notifier(abc.ABC):
    """通知渠道基类。"""

    name: str = "base"

    #: 必填的 options 字段名，构造时校验
    requires: tuple[str, ...] = ()

    def __init__(self, options: dict[str, Any] | None = None, client: httpx.AsyncClient | None = None):
        self.options = dict(options or {})
        self._client = client
        self._validate()

    def _validate(self) -> None:
        missing = [k for k in self.requires if not str(self.options.get(k, "")).strip()]
        if missing:
            raise NotConfiguredError(
                f"通知渠道 {self.name!r} 缺少必填配置：{', '.join(missing)}。"
                "请检查 tasks.yaml 的 notify 段，以及对应的环境变量是否已设置。"
            )

    def _require(self, key: str) -> str:
        value = str(self.options.get(key, "")).strip()
        if not value:
            raise NotConfiguredError(f"通知渠道 {self.name!r} 的 options.{key} 为空")
        return value

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(f"{self.name} 未注入 httpx 客户端")
        return self._client

    @abc.abstractmethod
    async def send(self, message: Message) -> None:
        """投递消息。失败请抛异常，engine 会记录但不中断监控。"""

    async def aclose(self) -> None:  # noqa: B027  （故意留空：只有持长连接的渠道需要覆盖）
        """释放资源。默认无操作，需要清理连接的渠道自行覆盖。"""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = ["Message", "NotConfiguredError", "Notifier"]
