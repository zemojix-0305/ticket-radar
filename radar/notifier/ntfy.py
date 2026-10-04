"""ntfy —— 零注册、零成本的开源推送服务。本项目的**默认推荐渠道**。

为什么把它排在第一位
--------------------
其他渠道都要先注册账号、拿 token；ntfy 连账号都不需要。向一个
topic 发一条 HTTP POST，订阅了该 topic 的设备就能收到推送。
它是本项目里唯一「clone 下来改一行配置就能收到通知」的渠道，
对开源项目来说这比任何功能都重要。

官方公共实例 ``https://ntfy.sh`` 免费限 250 条/天（按 IP 计），
个人余票监控一天顶多几十条，够用。要更多或要隐私，可以自建：

    docker run -d -p 8080:80 binwiederhier/ntfy serve

配置：
    type: ntfy
    options:
      topic: ${NTFY_TOPIC}          # 必填。别人猜不到的名字，相当于密码
      # server: https://ntfy.sh     # 可选。自建时改成自己的域名
      # priority: high              # 可选。min/low/default/high/urgent
      # tags: rotating_light        # 可选。emoji 短代码，逗号分隔

两个实现上的坑
--------------
1. **标题不能走 HTTP header。** ntfy 支持 ``Title: xxx`` 头，但 HTTP 头
   按规范只能是 ASCII，而本项目的标题是「【余票提醒】北京南 → 上海虹桥」，
   含中文 → h11 会直接拒绝整个请求。所以这里统一走 ntfy 的 **JSON 发布**
   接口（``POST /`` + JSON body），中文没有任何问题。
2. **单条消息上限 4096 字节。** 京沪线 54 趟车的通知轻松超过这个数，
   ntfy 会把超长消息**自动转成附件**——手机上得点开附件才看得见，
   等于没提醒。所以这里主动按 UTF-8 边界截断，宁可少几行也不能变附件。
"""

from __future__ import annotations

import logging

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.ntfy")

DEFAULT_SERVER = "https://ntfy.sh"

#: ntfy 的服务端上限是 4096 字节，留出 JSON 包装的余量。
MAX_MESSAGE_BYTES = 3800

#: 优先级别名 → ntfy 的 1~5。允许用户写人话。
_PRIORITIES = {
    "min": 1,
    "low": 2,
    "default": 3,
    "high": 4,
    "urgent": 5,
    "max": 5,
}


def clip_utf8(text: str, limit: int = MAX_MESSAGE_BYTES) -> str:
    """按 UTF-8 字节上限截断，不切断多字节字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    # errors="ignore" 会丢掉被切断的那个残缺字节，剩下的一定是合法字符
    head = raw[:limit].decode("utf-8", errors="ignore")
    return f"{head}\n\n…（余票条目过多，已截断）"


@register_notifier("ntfy")
class NtfyNotifier(Notifier):
    """ntfy 推送。免费、开源、无需注册。"""

    name = "ntfy"
    requires = ("topic",)

    def _endpoint(self) -> str:
        server = str(self.options.get("server") or DEFAULT_SERVER).strip().rstrip("/")
        return f"{server}/"

    def _priority(self) -> int:
        raw = self.options.get("priority")
        if raw is None or str(raw).strip() == "":
            return _PRIORITIES["default"]
        if isinstance(raw, int):
            return max(1, min(5, raw))
        return _PRIORITIES.get(str(raw).strip().lower(), _PRIORITIES["default"])

    def _tags(self) -> list[str]:
        raw = self.options.get("tags")
        if not raw:
            return []
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        return [str(t).strip() for t in raw if str(t).strip()]

    async def send(self, message: Message) -> None:
        topic = self._require("topic")

        payload: dict[str, object] = {
            "topic": topic,
            "title": message.title,
            "message": clip_utf8(message.body),
            "priority": self._priority(),
            "markdown": True,
        }
        if message.url:
            # 点击通知直接跳官方页面——正好补上「只提醒不下单」的最后一跳
            payload["click"] = message.url
        tags = self._tags()
        if tags:
            payload["tags"] = tags

        resp = await self._http().post(self._endpoint(), json=payload)
        resp.raise_for_status()

        # ntfy 成功时返回 200 + 消息回执；失败用 HTTP 状态码表示。
        try:
            body = resp.json()
        except ValueError:
            return
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"ntfy 返回错误：{body.get('error')}")


__all__ = ["MAX_MESSAGE_BYTES", "NtfyNotifier", "clip_utf8"]
