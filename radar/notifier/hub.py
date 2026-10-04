"""通知扇出：一条消息推给所有渠道，单个渠道失败不影响其他渠道。

设计取舍
--------
* 用 ``asyncio.gather(return_exceptions=True)`` 并发推送，而不是串行——
  邮件 SMTP 握手可能要几秒，串行会让 Telegram 白等。
* 全部渠道都失败才抛异常；部分失败只记 warning。
  这样「微信推送挂了但邮件通了」不会让 engine 误判为整轮失败。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from .base import Message, Notifier

log = logging.getLogger("radar.notifier.hub")


class NotifierHub:
    """把消息扇出到多个渠道。"""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self.notifiers = list(notifiers)

    def __bool__(self) -> bool:
        return bool(self.notifiers)

    def __len__(self) -> int:
        return len(self.notifiers)

    async def send(self, message: Message) -> dict[str, str]:
        """推送到所有渠道。

        返回 ``{渠道名: 错误信息}``，只包含失败的渠道。
        全部失败时抛 ``RuntimeError``。
        """
        if not self.notifiers:
            log.debug("没有配置通知渠道，跳过推送：%s", message.title)
            return {}

        results = await asyncio.gather(
            *(n.send(message) for n in self.notifiers),
            return_exceptions=True,
        )

        failures: list[tuple[str, str]] = []
        for notifier, result in zip(self.notifiers, results, strict=True):
            if isinstance(result, BaseException):
                failures.append((notifier.name, f"{type(result).__name__}: {result}"))
                log.warning("渠道 %s 推送失败：%s", notifier.name, result)
            else:
                log.info("渠道 %s 推送成功：%s", notifier.name, message.title)

        # 按失败条数判断，而不是按去重后的渠道名——两个同名渠道会算漏
        if failures and len(failures) == len(self.notifiers):
            raise RuntimeError(f"所有通知渠道均失败：{dict(failures)}")
        return dict(failures)

    async def send_raw(self, title: str, body: str, url: str | None = None) -> dict[str, str]:
        return await self.send(Message(title=title, body=body, url=url))

    async def aclose(self) -> None:
        for notifier in self.notifiers:
            try:
                await notifier.aclose()
            except Exception as exc:  # 关闭失败不影响退出
                log.debug("关闭渠道 %s 时出错：%s", notifier.name, exc)


__all__ = ["NotifierHub"]
