"""纷玩岛（livelab）——**当前接不了**，这个文件只剩它一个。

历史说明（别被老代码误导）
--------------------------
这里原来还放着猫眼和摩天轮的「占位档案」，当时的判断是「演出票平台都
需要登录态、都有反爬、接口名随时变，所以只给默认配置，让用户自己填」。

2026-09-30 的实测推翻了这个判断：**猫眼和摩天轮的关键接口都是公开的，
不需要 Cookie、不需要签名**。它们现在各自有了真适配器：

* :mod:`radar.adapters.maoyan`       猫眼演出（网关在 m.dianping.com/myshow）
* :mod:`radar.adapters.moretickets`  摩天轮票务（网关 unify.moretickets.com）

留下来的是纷玩岛——它是**真的**接不了，而且原因是平台的，不是代码的：
它只在 App 和微信小程序里卖票，网页版是个下载落地页，一个数据请求都不发。

纷玩岛
------
实地查证（2026-09）：

- 官网 ``www.fenwandao.com`` 的 HTTPS 证书域名不匹配，HTTP 直接连接重置，
  浏览器和 curl 都打不开；
- 真实域名是 ``livelab.com.cn``（主体：上海名辉文化发展有限公司，
  包名 ``cn.com.livelab``）。能打开的 ``m.livelab.com.cn`` 是个**静态下载落地页**，
  页脚写着「纷玩岛 © 2021」，点「演唱会」页面不跳转、一个 xhr 都不发；
- 小程序接口带 ``wx`` 系签名参数，浏览器里拿不到可复用的 URL。

本项目**不做**小程序签名逆向——那属于对抗平台防护，与「只读提醒」的定位冲突，
也和「不做验证码识别 / 不做设备指纹伪装」是同一条红线。

所以这个类保留下来，等它哪天上了网页版票务页：接口是普通 HTTP + Cookie，
配置方式和猫眼一致，``radar probe`` 探测一下就能用。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import HEALTH_UNSUPPORTED, Capability
from .json_api import JsonApiAdapter
from .registry import register


class ShowPlatformAdapter(JsonApiAdapter):
    """演出票平台通用档案：多一步「场次 → 车次」的语义说明。

    复用列车模型是有意的：一场演出 = 一个 TrainState，一个票档 = 一个席别。
    ``train_code`` 位放场次 ID，``seat_type`` 位放票档名（如「380元看台」）。
    这样状态机、限流、通知格式、存储全部不用改——演出票和车票的
    「有无余量」本质是同一个问题。

    字段说明（都可在 tasks.yaml 里覆盖）：

    ``items_path``
        从哪取场次数组。点分路径，如 ``data.result.performList``。
    ``item_id`` / ``item_label``
        场次 ID（作为去重键）与展示名。给多个候选路径时取第一个非空的。
    ``item_fields``
        展示字段映射，支持 ``depart_time`` / ``arrive_time`` / ``duration``
        / ``from_station`` / ``to_station``。
    ``seats_path``
        票档数组相对每个场次的路径；留空表示「每个场次一条记录、自身即票档」
        （有些接口是按票档打平的平铺结构）。
    ``seat_name`` / ``seat_status``
        票档名与状态字段的候选路径。
    """

    requires_credentials = True
    min_interval = 300.0

    #: 平台档案的公共默认：票档名与状态字段的候选名在同类平台里高度相似
    defaults: dict[str, Any] = {
        "item_id": ["performId", "sessionId", "id"],
        "item_label": ["performName", "sessionName", "name"],
        "item_fields": {"depart_time": "showTime"},
        "seats_path": "skuList",
        "seat_name": ["priceName", "skuName", "ticketName", "name"],
        "seat_status": ["status", "remainNum", "stock", "sellStatus", "canBuy"],
    }

    #: 该平台需要用户去哪个页面复制 URL（写进报错提示，省得来回翻文档）
    find_url_hint: str = "打开目标页面 → F12 → Network → 找返回票档/场次的 XHR"


@register("fenwandao")
class FenWanDaoAdapter(ShowPlatformAdapter):
    """纷玩岛余票——**当前接不了**，这是平台的限制，不是代码的缺口。

    详细的实地查证记录见模块 docstring 的「纷玩岛」一节。
    """

    name = "fenwandao"
    # 官方域名，目前不可达——留着等它恢复，不是在用
    base_url = "https://www.fenwandao.com"

    capability = Capability(
        category="show",
        summary="纷玩岛（票只在 App/小程序里卖，网页端接不了）",
        status="unsupported",
        regions=("CN",),
        limitation="纷玩岛只在 App 和微信小程序里卖票，网页版是个下载落地页，"
        "一个数据请求都不发；小程序接口带 wx 系签名，本项目不做签名逆向。"
        "等它上了网页版票务页就能接。",
    )
    find_url_hint = (
        "纷玩岛目前没有网页端购票入口（网页版只是 App 下载页），接不了。"
        "它上了网页版之后：F12 → Network → 找返回场次/票档的请求；"
        "小程序接口带 wx 加密参数，本项目不做签名逆向"
    )

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：明确报「不支持」，而且**不发请求**。

        平台层面接不了还去请求它，纯属白耗一次连接。更要紧的是，体检报告里
        必须把「不支持」和「没查」分开——混成一句「异常」等于让用户去
        修一个根本修不了的东西。
        """
        return HEALTH_UNSUPPORTED, self.capability.limitation
    defaults = {
        **ShowPlatformAdapter.defaults,
        "items_path": "data.list",
        "seat_name": ["priceName", "skuName", "name"],
        "seat_status": ["status", "stock", "remainNum"],
    }


__all__ = [
    "FenWanDaoAdapter",
    "ShowPlatformAdapter",
]
