"""Server酱（微信推送）。

配置：
    type: serverchan
    options:
      sendkey: ${SERVERCHAN_SENDKEY}
      # 可选，Server酱³ 用的是 <uid>.push.ft07.com 域名
      base_url: https://sctapi.ftqq.com
"""

from __future__ import annotations

import logging

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.serverchan")

DEFAULT_BASE = "https://sctapi.ftqq.com"


@register_notifier("serverchan")
class ServerChanNotifier(Notifier):
    name = "serverchan"
    requires = ("sendkey",)

    async def send(self, message: Message) -> None:
        sendkey = self._require("sendkey")
        base = str(self.options.get("base_url") or DEFAULT_BASE).rstrip("/")
        url = f"{base}/{sendkey}.send"

        desp = message.body
        if message.url:
            desp += f"\n\n[点此打开官方页面自行下单]({message.url})"

        resp = await self._http().post(url, data={"title": message.title, "desp": desp})
        resp.raise_for_status()

        try:
            payload = resp.json()
        except ValueError:
            return
        code = payload.get("code")
        if code not in (0, None):
            raise RuntimeError(f"Server酱返回错误 code={code}：{payload}")


__all__ = ["ServerChanNotifier"]
