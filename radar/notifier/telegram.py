"""Telegram Bot 推送。

配置：
    type: telegram
    options:
      bot_token: ${TELEGRAM_BOT_TOKEN}
      chat_id: ${TELEGRAM_CHAT_ID}
      base_url: https://api.telegram.org    # 可选，自建反代时改这里
      silent: false                          # 可选，true 则静音推送

Bot token 找 @BotFather 创建，chat_id 找 @userinfobot 查。
国内直连 api.telegram.org 不通，需要自备代理或反代。
"""

from __future__ import annotations

import logging

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.telegram")

DEFAULT_BASE = "https://api.telegram.org"


@register_notifier("telegram")
class TelegramNotifier(Notifier):
    name = "telegram"
    requires = ("bot_token", "chat_id")

    async def send(self, message: Message) -> None:
        token = self._require("bot_token")
        chat_id = self._require("chat_id")
        base = str(self.options.get("base_url") or DEFAULT_BASE).rstrip("/")
        url = f"{base}/bot{token}/sendMessage"

        text = f"*{message.title}*\n\n{message.body}"

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
            "disable_notification": bool(self.options.get("silent", False)),
        }

        resp = await self._http().post(url, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram 返回 HTTP {resp.status_code}：{resp.text[:200]}")

        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram 返回错误：{body}")


__all__ = ["TelegramNotifier"]
