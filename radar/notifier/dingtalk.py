"""钉钉群机器人。

配置：
    type: dingtalk
    options:
      webhook: ${DINGTALK_WEBHOOK}     # 安全设置选「自定义关键词」时只需这个
      secret: ${DINGTALK_SECRET}       # 安全设置选「加签」时必填
      keyword: 余票                    # 用关键词方式时的关键词（可选）

加签算法（钉钉官方文档）：
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = urlencode(base64(HMAC-SHA256(secret, string_to_sign)))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.dingtalk")


@register_notifier("dingtalk")
class DingTalkNotifier(Notifier):
    name = "dingtalk"
    requires = ("webhook",)

    def _signed_url(self, webhook: str) -> str:
        secret = str(self.options.get("secret") or "").strip()
        if not secret:
            return webhook
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        sep = "&" if "?" in webhook else "?"
        return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"

    async def send(self, message: Message) -> None:
        webhook = self._require("webhook")

        text = f"### {message.title}\n\n{message.body}"
        if message.url:
            text += f"\n\n[点此打开官方页面自行下单]({message.url})"

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": text},
        }

        resp = await self._http().post(self._signed_url(webhook), json=payload)
        resp.raise_for_status()

        try:
            body = resp.json()
        except ValueError:
            return
        if body.get("errcode") not in (0, None):
            raise RuntimeError(f"钉钉返回错误：{body}")


__all__ = ["DingTalkNotifier"]
