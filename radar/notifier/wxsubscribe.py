"""微信小程序订阅消息——真正「进微信」的推送，个人主体即可用。

配置：
    type: wxsubscribe
    options:
      app_id: wx__________________
      app_secret: ${WX_APPSECRET}
      template_id: YnMb__________________
      open_id: o_____________________     # 推给谁（自己的 openid）
      # 可选：模板字段的 key，默认 thing1 / thing2，按你申请的模板实际字段改
      # field_title: thing1               # 模板里「演出/车票名称」那个字段
      # field_body: thing2                # 模板里「温馨提示」那个字段
      # 可选：点开消息进入的小程序页面
      # page: pages/index/index

与其他渠道的三点不同：

1. **一次性订阅**：用户在小程序里点一次「允许通知」，才换来一条推送额度。
   额度用完再发会得到 43101——这不是 bug，是微信的规则。
   监控场景的正确姿势：收到提醒后顺手再点一次授权，额度就续上了。
2. **字段是白名单制**：内容只能填进模板申请时勾好的字段里，
   ``thing`` 类型最多 20 字（超长会被截断），不支持 Markdown、不能带链接。
3. **access_token 两小时一换**：本渠道自动获取并缓存，失败会自动刷新重试一次，
   不需要用户操心。

errcode → 人话的对照表见 ``_CODE_HINTS``，出错时直接把排查方向写进异常信息。
"""

from __future__ import annotations

import logging
import time

import httpx

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.wxsubscribe")

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"

#: thing 类型字段的最大长度（微信硬限制，超长直接报 47003）
THING_MAX = 20

_CODE_HINTS: dict[int, str] = {
    40001: "app_secret 不对，或已被重置——去 mp.weixin.qq.com「开发设置」重新生成",
    40013: "app_id 不对，检查是否少了或多了字符",
    40037: "template_id 不对，去「订阅消息→我的模板」核对",
    41030: "page 路径不存在，必须是小程序里真实存在的页面",
    43101: "对方没有订阅额度了——一次性订阅 = 授权一次只能收一条，"
    "请重新打开小程序点一次「允许通知」",
    47003: "模板字段对不上——检查 field_title / field_body 的 key 是否与模板一致，"
    "以及内容是否超长",
    45009: "调用频率超限，稍等几分钟再试",
}


def _clip(value: str, limit: int = THING_MAX) -> str:
    """thing 字段超长截断。微信对超长是直接拒发，所以这里主动剪。"""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


@register_notifier("wxsubscribe")
class WxSubscribeNotifier(Notifier):
    """微信小程序订阅消息。个人主体可用，直达微信服务通知。"""

    name = "wxsubscribe"
    requires = ("app_id", "app_secret", "template_id", "open_id")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._token: str = ""
        self._token_expires_at: float = 0.0

    # --- access_token -----------------------------------------------------

    async def _get_token(self, force_refresh: bool = False) -> str:
        """获取并缓存 access_token（有效期 7200s，提前 5 分钟换新）。"""
        if not force_refresh and self._token and time.monotonic() < self._token_expires_at:
            return self._token

        app_id = self._require("app_id")
        app_secret = self._require("app_secret")
        resp = await self._http().get(
            TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            code = int(data.get("errcode", -1))
            hint = _CODE_HINTS.get(code, data.get("errmsg", ""))
            raise RuntimeError(f"微信 access_token 获取失败（errcode={code}）：{hint}")
        self._token = token
        self._token_expires_at = time.monotonic() + int(data.get("expires_in", 7200)) - 300
        return token

    # --- 发送 --------------------------------------------------------------

    async def send(self, message: Message) -> None:
        token = await self._get_token()
        # thing 字段只有 20 字，URL 根本放不下，所以不拼链接——
        # 用户点这条消息会直接跳到 options.page 指定的小程序页面。
        body = message.plain_text

        payload = {
            "touser": self._require("open_id"),
            "template_id": self._require("template_id"),
            "page": str(self.options.get("page") or "pages/index/index"),
            "data": {
                str(self.options.get("field_title") or "thing1"): {
                    "value": _clip(message.title),
                },
                str(self.options.get("field_body") or "thing2"): {
                    "value": _clip(body),
                },
            },
        }

        result = await self._post_send(token, payload)
        errcode = int(result.get("errcode", 0))

        # token 可能因服务端提前失效（返回 40001），强制刷新后重试一次
        if errcode == 40001:
            log.info("access_token 已失效，刷新后重试")
            token = await self._get_token(force_refresh=True)
            result = await self._post_send(token, payload)
            errcode = int(result.get("errcode", 0))

        if errcode != 0:
            hint = _CODE_HINTS.get(errcode, result.get("errmsg", ""))
            raise RuntimeError(
                f"微信订阅消息发送失败（errcode={errcode}）：{hint}"
            )

    async def _post_send(self, token: str, payload: dict) -> dict:
        resp = await self._http().post(
            SEND_URL, params={"access_token": token}, json=payload
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"errcode": -1, "errmsg": f"响应不是 JSON：{resp.text[:200]}"}


__all__ = ["WxSubscribeNotifier"]
