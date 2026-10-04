"""Bark —— iOS 专属的免费开源推送（走 APNs，比 ntfy 更即时）。

适用人群：只用 iPhone 的人。Bark 通过苹果 APNs 下发，无需后台常驻，
能做到真正的秒级到达，还支持「重要警告」——可以穿透专注模式，
这对「余票刚放出来」这种抢时间的场景很关键。

非 iOS 用户请用 ntfy。

配置：
    type: bark
    options:
      key: ${BARK_KEY}             # Bark App 首页那一串设备码
      # server: https://api.day.app # 可选，自建时改成自己的域名
      # group: 余票                   # 可选，通知分组名，便于归类
      # sound: alarm                 # 可选，提示音
      # level: timeSensitive         # 可选，active/timeSensitive/critical

``key`` 允许直接粘贴 Bark App 首页的完整推送地址
（``https://api.day.app/xxxxxxxx``），会自动拆出 server 和 key，
省得用户自己去分哪一段是设备码。

同样走 JSON 接口（``POST /push``）而不是 URL 路径拼接：
路径拼接要把中文标题做 URL 编码，出错概率高、也不好看。
"""

from __future__ import annotations

import logging
import urllib.parse

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.bark")

DEFAULT_SERVER = "https://api.day.app"


def parse_key(raw: str) -> tuple[str, str | None]:
    """从用户输入里拆出 ``(key, server)``。

    允许两种写法：
    * ``xxxxxxxx``                          → 纯设备码
    * ``https://api.day.app/xxxxxxxx``      → 完整推送地址
    * ``https://bark.example.com/xxxxxxxx`` → 自建服务器地址

    自建用户往往会直接复制 App 里的完整 URL，这里替他把 server 也识别出来。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return "", None

    if "://" not in text:
        return text, None

    parts = urllib.parse.urlsplit(text)
    server = f"{parts.scheme}://{parts.netloc}"
    key = parts.path.strip("/")

    # 常见的完整地址形如 /xxxxxxxx/，也可能带上 /push 后缀
    if key.endswith("/push"):
        key = key[: -len("/push")].strip("/")
    return key, server


@register_notifier("bark")
class BarkNotifier(Notifier):
    """Bark 推送（iOS）。免费、开源、走 APNs。"""

    name = "bark"
    requires = ("key",)

    def _endpoint(self) -> str:
        server = str(self.options.get("server") or "").strip().rstrip("/")
        return f"{server or DEFAULT_SERVER}/push"

    def _resolve(self) -> tuple[str, str]:
        raw = self._require("key")
        key, inferred = parse_key(raw)
        if not key:
            raise ValueError(f"通知渠道 {self.name!r} 无法从 {raw!r} 解析出设备码")
        server = str(self.options.get("server") or "").strip().rstrip("/") or inferred
        return key, server or DEFAULT_SERVER

    async def send(self, message: Message) -> None:
        key, server = self._resolve()

        # Bark 不认 Markdown，用纯文本降级
        payload: dict[str, object] = {
            "device_key": key,
            "title": message.title,
            "body": message.plain_text,
        }
        if message.url:
            payload["url"] = message.url
        for src, dst in (("group", "group"), ("sound", "sound"), ("level", "level")):
            value = str(self.options.get(src) or "").strip()
            if value:
                payload[dst] = value

        resp = await self._http().post(f"{server}/push", json=payload)
        resp.raise_for_status()

        try:
            body = resp.json()
        except ValueError:
            return
        # Bark 用 code=200 表示受理成功
        if isinstance(body, dict) and body.get("code") not in (200, None, "200"):
            hint = body.get("message") or body.get("msg") or ""
            raise RuntimeError(f"Bark 返回 code={body.get('code')}：{hint}")


__all__ = ["DEFAULT_SERVER", "BarkNotifier", "parse_key"]
