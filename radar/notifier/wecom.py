"""企业微信群机器人。

配置：
    type: wecom
    options:
      webhook: ${WECOM_WEBHOOK}

注意：企业微信 markdown 只认有限的语法子集，``**加粗**`` 在部分客户端不渲染，
所以这里额外拼一段 ``<font color="warning">`` 的标题行提高可见度。
"""

from __future__ import annotations

import logging

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.wecom")


@register_notifier("wecom")
class WeComNotifier(Notifier):
    name = "wecom"
    requires = ("webhook",)

    async def send(self, message: Message) -> None:
        webhook = self._require("webhook")

        text = f'<font color="warning">{message.title}</font>\n\n{message.body}'
        if message.url:
            text += f"\n\n[点此打开官方页面自行下单]({message.url})"

        payload = {"msgtype": "markdown", "markdown": {"content": text}}

        resp = await self._http().post(webhook, json=payload)
        resp.raise_for_status()

        try:
            body = resp.json()
        except ValueError:
            return
        if body.get("errcode") not in (0, None):
            raise RuntimeError(f"企业微信返回错误：{body}")


__all__ = ["WeComNotifier"]
