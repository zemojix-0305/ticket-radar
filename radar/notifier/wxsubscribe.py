"""微信小程序订阅消息——真正「进微信」的推送，个人主体即可用。

配置：
    type: wxsubscribe
    options:
      app_id: wx__________________
      app_secret: ${WX_APPSECRET}
      template_id: ${WX_TEMPLATE_ID}    # 「订阅消息→我的模板」里的 priTmplId
      open_id: ${WX_OPEN_ID}            # 推给谁（自己的 openid，`radar wxbind` 可自动取）
      # 字段映射：左边是模板字段名，右边是要填的内容（支持变量）
      fields:
        thing6: "{title}"               # 演出名称 / 车票名称
        thing8: "{task}"                # 场次名称
        time7: "{time}"                 # 放票时间（time 类型，自动按 YYYY-MM-DD HH:MM:SS 填）
        thing5: "radar"                 # 放票机构
        thing4: "{body}"                # 温馨提示（正文）
      # 不写 fields 时的简易写法：field_title: thing1 / field_body: thing2

与其他渠道的三点不同：

1. **一次性订阅**（type=2 模板）：在小程序里点一次「允许通知」，才换来一条推送额度。
   额度用完再发会得到 43101——这不是 bug，是微信的规则。
   监控场景的正确姿势：收到提醒后顺手再点一次授权，额度就续上了。
2. **字段是白名单制**：内容只能填进模板申请时勾好的字段里（thing6/thing8/time7…），
   ``thing`` 类型最多 20 字（超长会被截断，不截断微信直接拒发），
   不支持 Markdown、不能带链接。字段名和模板对不上会报 47003。
3. **access_token 两小时一换**：本渠道自动获取并缓存，失效会自动刷新重试一次，
   不需要用户操心。

errcode → 人话的对照表见 ``_CODE_HINTS``，出错时直接把排查方向写进异常信息。
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.wxsubscribe")

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"

#: thing / character 类型字段的最大长度（微信硬限制，超长直接报 47003）
THING_MAX = 20

_CODE_HINTS: dict[int, str] = {
    40001: "app_secret 不对，或已被重置——去 mp.weixin.qq.com「开发设置」重新生成",
    40013: "app_id 不对，检查是否少了或多了字符",
    40037: "template_id 不对，去「订阅消息→我的模板」核对（要填 priTmplId，不是模板标题）",
    41030: "page 路径不存在，必须是小程序里真实存在的页面",
    43101: "没有订阅额度了——一次性订阅 = 授权一次只能收一条，"
    "请重新打开小程序点一次「允许通知」",
    47003: "模板字段对不上——检查 fields 里的字段名是否与模板一致（thing6/time7 这种），"
    "以及内容是否超长、time 字段是否为 YYYY-MM-DD HH:MM:SS 格式",
    45009: "调用频率超限，稍等几分钟再试",
}


def _clip(value: str, limit: int = THING_MAX) -> str:
    """thing 字段超长截断。微信对超长是直接拒发，所以这里主动剪。"""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _render(template: str, ctx: dict[str, str]) -> str:
    """把 fields 值里的 {title} / {time} 之类占位符换成实际内容。"""
    out = str(template)
    for key, value in ctx.items():
        out = out.replace(f"{{{key}}}", value)
    return out


@register_notifier("wxsubscribe")
class WxSubscribeNotifier(Notifier):
    """微信小程序订阅消息。个人主体可用，直达微信「服务通知」。"""

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

    def _build_data(self, message: Message) -> dict[str, dict[str, str]]:
        """按模板字段映射拼出 data。

        这是本渠道最容易出错的地方：字段名必须与申请模板时勾的完全一致
        （thing6 / time7 这种，不是「演出名称」），time 类字段还得是
        ``YYYY-MM-DD HH:MM:SS`` 格式，否则微信一律 47003。
        """
        ctx = {
            "title": message.title,
            "body": message.plain_text,
            "task": str(message.extra.get("task_name") or message.extra.get("task_id") or ""),
            "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": message.url or "",
        }

        fields = self.options.get("fields")
        if isinstance(fields, dict) and fields:
            mapping = {str(k): str(v) for k, v in fields.items()}
        else:
            # 简易写法：只映射标题和正文两个字段
            mapping = {
                str(self.options.get("field_title") or "thing1"): "{title}",
                str(self.options.get("field_body") or "thing2"): "{body}",
            }

        data: dict[str, dict[str, str]] = {}
        for key, template in mapping.items():
            value = _render(template, ctx)
            # 微信约定：字段名以 time 开头的是时间类型，不能截断
            if not key.lower().startswith("time"):
                value = _clip(value)
            data[key] = {"value": value}
        return data

    async def send(self, message: Message) -> None:
        token = await self._get_token()
        payload = {
            "touser": self._require("open_id"),
            "template_id": self._require("template_id"),
            "page": str(self.options.get("page") or "pages/index/index"),
            "data": self._build_data(message),
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
            raise RuntimeError(f"微信订阅消息发送失败（errcode={errcode}）：{hint}")

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
