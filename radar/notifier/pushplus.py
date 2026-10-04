"""PushPlus（推送加）—— 微信推送，实名后免费额度 200 条/天。

配置：
    type: pushplus
    options:
      token: ${PUSHPLUS_TOKEN}
      # 可选，默认 markdown（本项目的正文本来就是 Markdown）
      # template: markdown
      # 可选，默认 wechat（微信服务号）。其他：webhook / cp / mail / app / extension / qq
      # channel: wechat
      # 可选，走自建代理时才需要覆盖
      # endpoint: https://www.pushplus.plus/send

两个必须先知道的点：

- 平台自 2024-08 起要求**实名认证**，未实名调用会返回 905，消息发不出去。
- 接口是**异步**的：返回 code=200 只代表平台收下了请求，不保证已经送达手机。
"""

from __future__ import annotations

import logging

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.pushplus")

DEFAULT_ENDPOINT = "https://www.pushplus.plus/send"

#: 平台业务码 → 人话。只丢一句「code=903」用户没法自己排查。
_CODE_HINTS: dict[str, str] = {
    "302": "未登录，token 可能已失效",
    "401": "请求未授权",
    "403": "请求 IP 未授权",
    "500": "PushPlus 服务端异常，稍后重试",
    "600": "数据异常",
    "888": "积分不足",
    "900": "请求次数过多，账号被临时限流",
    "903": "token 无效，请到个人中心重新复制",
    "905": "账号未实名认证，PushPlus 要求实名后才能发送消息",
    "999": "服务端验证错误",
}


@register_notifier("pushplus")
class PushPlusNotifier(Notifier):
    """PushPlus 微信推送。免费额度 200 条/天，是目前最划算的微信直达渠道。"""

    name = "pushplus"
    requires = ("token",)

    async def send(self, message: Message) -> None:
        token = self._require("token")
        endpoint = str(self.options.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")

        content = message.body
        if message.url:
            content += f"\n\n[点此打开官方页面自行下单]({message.url})"

        resp = await self._http().post(
            endpoint,
            json={
                "token": token,
                "title": message.title,
                "content": content,
                "template": str(self.options.get("template") or "markdown"),
                "channel": str(self.options.get("channel") or "wechat"),
            },
        )
        resp.raise_for_status()

        try:
            result = resp.json()
        except ValueError:
            return

        # PushPlus 用**业务码**而不是 HTTP 状态码表示结果
        code = result.get("code")
        if code is None or str(code).strip() in {"200", "0"}:
            return

        log.debug("PushPlus 原始返回：%s", result)
        hint = _CODE_HINTS.get(str(code).strip())
        detail = result.get("msg") or result.get("data") or ""
        suffix = f"（{hint}）" if hint else ""
        raise RuntimeError(f"PushPlus 返回 code={code}{suffix}：{detail}")


__all__ = ["PushPlusNotifier"]
