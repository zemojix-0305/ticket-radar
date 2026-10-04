"""大麦（damai）演出票适配器 —— 含 mtop 签名基类。

为什么单独写
------------
大麦（以及所有淘宝系 H5）的接口不是普通 REST，而是 **mtop**：
请求要先算一个 ``sign``，公式是公开的客户端协议：

    sign = md5(token & t & appKey & data)

其中 ``token`` 从 Cookie 里的 ``_m_h5_tk`` 取（下划线前那一段），
``t`` 是毫秒时间戳，``data`` 是**未经 URL 编码**的请求体 JSON 字符串。

这是淘宝前端自己就在跑的协议，不是破解：我们只是按同样的格式发请求。
刻意**没有**做的事：不改设备指纹、不做滑块/验证码识别、不用代理池、
不轮换账号。遇到风控就退避报错。

我们到底能监控到哪一层
----------------------
这是大麦最反直觉的一点，实测（2026-09-30）结论如下：

* **PC 网页端已经不卖票了**。详情页返回的 ``buyButton.text`` 恒为
  「该渠道不支持购票」，``tip`` 是「请到大麦App购买」。所以**不要**指望
  在 Web 端读到「立即购买」这类按钮状态——它永远是这个值。
* **票档余量只在选座页，而选座页在 App 里**。详情页的 xhr/fetch 响应体里
  一个票档关键字都没有（页面上那些「缺货」字样来自前端 JS 文案）。
* 因此本项目对大麦的可行监控面是**项目级的售票状态**：
  ``data.guide.tour.projectList[].saleStatus``，取值如「热卖 / 缺货 / 预约」。
  它挂在「巡演各站」列表上，一站在售与否一目了然。粒度比票档粗，
  但足以回答「现在到底能不能买」——而这正是回流票监控要的那个信号。

接口名会变，所以它是配置项
--------------------------
``mtop.damai.item.detail.getdetail``（v1.0）是 2026-09-30 实测有效的接口名，
``appKey=12574478``（H5 端）。两者**都会随版本漂移**，所以都写成 ``params``
可覆盖的配置。接口变了照下面做，不用改代码：

1. 浏览器打开演出详情页 → F12 → Network → 过滤 ``mtop``
2. 找到返回演出数据的那个请求，复制它的 ``api`` / ``appKey`` 参数值
3. 写进 tasks.yaml 的 ``params.api`` / ``params.app_key``

顺带一提：旧的 ``mtop.damai.wireless.project.getprojectdetail`` 已经失效，
现在返回 ``FAIL_SYS_API_NOT_FOUNDED``；``appKey=23739456`` 也是错的。
这两个默认值曾经写错，是这次实测纠正过来的。

``radar probe`` 可以拿这个 URL 直接打印返回结构，确认字段路径。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from typing import Any

import httpx

from ..config import TaskConfig
from ..models import TrainState
from .base import (
    HEALTH_AUTH,
    HEALTH_OK,
    HEALTH_UNKNOWN,
    AdapterError,
    AuthError,
    Capability,
    ParseError,
    RateLimitedError,
    TransportError,
)
from .json_api import JsonApiAdapter, first_of, shape
from .registry import register

log = logging.getLogger("radar.adapters.damai")

MTOP_BASE = "https://mtop.damai.cn"

#: 大麦 H5 的 appKey（前端明文可见）。2026-09-30 实测 H5 端用的是 12574478，
#: 不是早先据移动端抓包抄来的 23739456——那个值配 getdetail 会签名不匹配。
DEFAULT_APP_KEY = "12574478"

#: 默认接口名。**会随版本变化**，改了就用 params.api 覆盖。
DEFAULT_API = "mtop.damai.item.detail.getdetail"
DEFAULT_VERSION = "1.0"
DEFAULT_JSV = "2.7.5"

#: 别指望靠换个 api 名拿到票档余量（2026-09-30 实测过，记下来省得再试一遍）。
#:
#: App 端用的是 ``mtop.alibaba.damai.detail.getdetail``，社区资料说它返回
#: 场次与票档（SkuId）状态。拿 Web 端签名去请求它：网关回
#: ``SUCCESS::调用成功``，业务层回 ``对不起，小二很忙，请稍后再试``。
#: 这是风控**软拒绝**——签名合法，但它认得出这不是 App 发出来的请求
#: （缺 ``x-mini-wua`` / ``x-sgext`` / 设备指纹等 App 专属请求头）。
#:
#: 要真拿到票档，只能复刻 App 的签名链（hook ``libmtguard.so`` /
#: ``libsgmain.so``，伪造设备指纹）。那属于对抗风控，本项目不做——
#: 详见 README「已知限制」。热门场次的余量数字本身也没什么用：
#: 票是秒没的，5 分钟一轮的监控看到的数字早就过期了。

#: 详情接口的 Referer 与 UA 都按 H5 端给。用 PC 的值实测也能通，
#: 但既然接口本身是 H5 的，就照着 H5 的来，减少被差异风控挑出来的概率。
DEFAULT_REFERER = "https://m.damai.cn/"
H5_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

#: 淘宝系 token 失效的 ret 标记，遇到就从响应 Cookie 里换新 token 重签一次
_TOKEN_EXPIRED_MARKERS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY", "令牌过期")


# ---------------------------------------------------------------------------
# 签名（纯函数，方便单测）
# ---------------------------------------------------------------------------


def parse_mtop_token(cookie: str) -> str:
    """从 Cookie 串里取出 ``_m_h5_tk`` 的 token 部分。

    Cookie 形如 ``_m_h5_tk=abc123def_1699999999999; ...``，
    签名只用下划线前那段。
    """
    for chunk in (cookie or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name.strip() == "_m_h5_tk" and value:
            return value.split("_")[0]
    return ""


def mtop_sign(token: str, timestamp_ms: int, app_key: str, data: str) -> str:
    """计算 mtop 签名：``md5(token&t&appKey&data)``。

    ``data`` 必须是未 URL 编码的原始 JSON 字符串——顺序和空格都会影响结果。
    """
    raw = f"{token}&{timestamp_ms}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def is_token_expired(payload: Any) -> bool:
    """判断 mtop 响应是否表示 token 失效。"""
    if not isinstance(payload, dict):
        return False
    ret = payload.get("ret")
    if isinstance(ret, str):
        ret = [ret]
    if not isinstance(ret, list):
        return False
    joined = " ".join(str(r) for r in ret)
    return any(marker in joined for marker in _TOKEN_EXPIRED_MARKERS)


# ---------------------------------------------------------------------------
# 解析（纯函数）
# ---------------------------------------------------------------------------

#: 场次数组的候选路径（相对 mtop 的 data.result）
PERFORM_PATHS = ["performBases", "performs", "performList", "performInfoList"]
#: 票档数组的候选路径
SKU_PATHS = ["skuList", "skus", "skuInfoList", "ticketList", "priceList"]
#: mtop 结果体的候选路径（相对整个响应）
RESULT_PATHS = ["data.result", "data.data", "data", "result"]


# ---------------------------------------------------------------------------
# 巡演各站的售票状态（详情接口里唯一可用的「能不能买」信号）
# ---------------------------------------------------------------------------

#: 巡演站列表在详情响应里的候选路径。
#: 注意 mtop 的响应是 ``{"api":..., "data":{...}, "ret":[...]}`` 包的，
#: 业务数据在 ``data`` 下面，所以第一候选带 ``data.`` 前缀；
#: 后面几条不带前缀的写法是为了兼容「调用方已经剥掉外层」的场景。
TOUR_LIST_PATHS = [
    "data.guide.tour.projectList",   # 实测（2026-09-30）走这条
    "guide.tour.projectList",
    "data.tour.projectList",
    "tour.projectList",
    "data.guide.tour.cityList",
    "guide.tour.cityList",
]

#: 站点状态里表示「能买」的值。
#: 「热卖」是实测里出现的（广州站 / 临沂站），「在售」等是同类平台的常见写法。
TOUR_AVAILABLE_VALUES = (
    "热卖", "在售", "售票中", "有票", "可售", "可购买", "立即购买", "即将开售",
)
#: 站点状态里表示「买不了」的值。
#: 「预约」= 还没开票、「缺货」= 卖光了，两者对「现在能不能买」是同一个答案，
#: 所以都归到不可购。这样它们变成「热卖」时能正确地触发一次提醒。
TOUR_SOLD_OUT_VALUES = (
    "缺货", "售罄", "售完", "已售完", "停售", "不可售", "暂不可售",
    "预约", "待开售", "未开售", "已结束", "已取消", "已下线",
)

#: 站点状态的席别名。**必须是固定值**——若拿 saleStatus 原文当 key，
#: 状态一变 key 就跟着变，diff 会把「改名」误读成「没了又来了」。
TOUR_SEAT_NAME = "售票状态"

#: 站点列表里表示「站名」的候选字段
TOUR_CITY_PATHS = ["cityName", "city", "name", "stationName"]
#: 站点列表里表示「演出日期」的候选字段
TOUR_TIME_PATHS = ["showTime", "showDate", "performTime", "date"]
#: 站点列表里表示「状态」的候选字段
TOUR_STATUS_PATHS = ["saleStatus", "saleStatusDesc", "status", "statusDesc"]
#: 站点列表里表示「项目 id」的候选字段
TOUR_ITEM_ID_PATHS = ["itemId", "itemID", "projectId", "id"]


def parse_damai_tour(
    data: Any,
    *,
    item_id: str = "",
    city: str = "",
) -> dict[str, TrainState]:
    """把详情响应里的「巡演各站在售状态」解析成 ``{站名: TrainState}``。

    这是大麦这条线上**唯一稳定可用**的「能不能买」信号（原因见模块 docstring）。

    :param item_id: 只保留这个 id 对应的那一站。留空则返回全部站。
        用途：一个巡演项目下十几个城市，用户通常只关心其中一站；
        不过滤的话，别的城市售罄也会推一条，纯噪音。
    :param city: 只保留站名含该字串的项（模糊匹配），作为 ``item_id``
        之外的兜底——有些接口不返回 itemId。

    状态取值看不懂的站会被**跳过**（不猜、不误报），符合本项目的一贯原则。
    """
    rows = first_of(data, TOUR_LIST_PATHS, None)
    if not isinstance(rows, list):
        return {}

    from .json_api import coerce_availability

    trains: dict[str, TrainState] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        name = str(first_of(row, TOUR_CITY_PATHS, "") or "").strip()
        if not name:
            continue

        row_id = str(first_of(row, TOUR_ITEM_ID_PATHS, "") or "").strip()
        if item_id and row_id and row_id != str(item_id).strip():
            continue
        if city and city not in name:
            continue

        raw_status = first_of(row, TOUR_STATUS_PATHS, None)
        seat = coerce_availability(
            TOUR_SEAT_NAME,
            raw_status,
            available_values=TOUR_AVAILABLE_VALUES,
            sold_out_values=TOUR_SOLD_OUT_VALUES,
        )
        if seat is None:
            log.debug("大麦站点状态看不懂，跳过：站=%s raw=%r", name, raw_status)
            continue

        trains[name] = TrainState(
            train_code=name,
            depart_time=str(first_of(row, TOUR_TIME_PATHS, "") or ""),
            seats={TOUR_SEAT_NAME: seat},
            extra={"item_id": row_id} if row_id else {},
        )
    return trains


def parse_damai_payload(data: Any) -> dict[str, TrainState]:
    """把大麦详情响应解析成 ``{场次ID: TrainState}``。

    大麦的结构是「场次列表 + 票档列表，靠 performId 关联」，
    所以这里先把票档按 performId 分组，再挂到对应场次上。

    票档名用 ``priceName``（如「380元看台」）——演出票没有车次那种统一席别，
    票档名就是最自然的区分维度。
    """
    result = first_of(data, RESULT_PATHS, None)
    if not isinstance(result, dict):
        raise AdapterError(
            f"mtop 响应里找不到 data.result。\n实际结构：\n{shape(data)}\n"
            "如果 ret 字段里是 FAIL_SYS_* 的报错，通常是 Cookie 过期或接口名变了。"
        )

    performs = first_of(result, PERFORM_PATHS, None)
    skus = first_of(result, SKU_PATHS, None)
    if not isinstance(performs, list):
        performs = []
    if not isinstance(skus, list):
        skus = []

    if not performs and not skus:
        ret = data.get("ret") if isinstance(data, dict) else None
        raise AdapterError(
            "大麦响应里既没有场次（performBases）也没有票档（skuList）。\n"
            f"ret = {ret!r}\n"
            f"实际结构：\n{shape(data)}\n"
            "ret 里若是 FAIL_SYS_* 开头，说明 Cookie 失效或接口名变了——"
            "Cookie 去 .env 换一条，接口名用 params.api 覆盖。"
        )

    trains: dict[str, TrainState] = {}
    project_name = str(first_of(result, ["projectName", "itemName", "name"], "") or "")

    # 1) 先建场次骨架
    for index, perform in enumerate(performs):
        if not isinstance(perform, dict):
            continue
        pid = str(first_of(perform, ["performId", "performID", "id"], "") or "").strip()
        if not pid:
            pid = f"perform#{index}"
        trains[pid] = TrainState(
            train_code=pid,
            from_station=str(first_of(perform, ["performName", "name"], "") or ""),
            depart_time=str(
                first_of(perform, ["performTime", "showTime", "startTime"], "") or ""
            ),
            duration=str(first_of(perform, ["duration"], "") or ""),
            seats={},
        )

    # 2) 再把票档挂上去。
    #    票档自带 performId 时就以它为准——不是所有接口都会返回场次列表，
    #    只给一张打平的 skuList 也要能正确分组。
    orphan: list[tuple[str, Any]] = []
    for sku in skus:
        if not isinstance(sku, dict):
            continue
        pid = str(first_of(sku, ["performId", "performID", "sessionId"], "") or "").strip()
        name = str(first_of(sku, ["priceName", "skuName", "name"], "") or "").strip()
        if not name:
            continue
        if pid:
            holder = trains.get(pid)
            if holder is None:
                holder = TrainState(train_code=pid, from_station=project_name, seats={})
                trains[pid] = holder
            _put_seat(holder, name, sku)
        else:
            orphan.append((name, sku))

    # 3) 票档完全没带场次信息时，收拢成一个「全部票档」单元，而不是丢掉
    if orphan:
        holder = trains.setdefault(
            "ALL",
            TrainState(train_code="ALL", from_station=project_name, seats={}),
        )
        for name, sku in orphan:
            _put_seat(holder, name, sku)
        if not holder.seats:
            trains.pop("ALL", None)

    return {code: t for code, t in trains.items() if t.seats}


def _put_seat(train: TrainState, name: str, sku: dict[str, Any]) -> None:
    """把一条票档写进 TrainState。同一票档重复出现时保留更「有票」的那个。"""
    status = first_of(sku, ["status", "remainNum", "stock", "canBuy", "sellStatus"], None)
    seat = _coerce(sku, name, status)
    if seat is None:
        return
    current = train.seats.get(name)
    if current is None or seat.effective > current.effective:
        train.seats[name] = seat


def _coerce(sku: dict[str, Any], name: str, status: Any):
    from .json_api import coerce_availability

    # 大麦的 status 语义：1 可售 / 0 售罄；也有接口直接给 remainNum 张数。
    # coerce_availability 已经能同时处理数字与词表，直接用。
    return coerce_availability(name, status)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


@register("damai")
class DamaiAdapter(JsonApiAdapter):
    """大麦演出票余票。需登录 Cookie（含 ``_m_h5_tk``）。"""

    name = "damai"
    #: 演出票平台比铁路更敏感，间隔放到 5 分钟起
    min_interval = 300.0
    requires_credentials = True
    base_url = MTOP_BASE

    capability = Capability(
        category="show",
        summary="大麦演出票（mtop 签名，需登录 Cookie）",
        can_search=False,
        can_resolve_link=False,
        seat_level=False,
        regions=("CN",),
        limitation="Web 端拿不到票档级库存，只能看项目级状态（如「热卖」）；"
        "不支持关键词搜索，需要你自己给场次 ID；登录 Cookie 有效期只有几小时。",
    )

    defaults: dict[str, Any] = {
        "api": DEFAULT_API,
        "version": DEFAULT_VERSION,
        "app_key": DEFAULT_APP_KEY,
        "jsv": DEFAULT_JSV,
        # 兜底路径：结构不认识时交给通用解析器按这条找数组
        "items_path": "data.guide.tour.projectList",
    }

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._token = parse_mtop_token(credentials.get("cookie", "") if credentials else "")

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活大麦 Cookie。

        **不发任何请求**，纯本地判断——这既是省事，也是必须的：大麦的
        ``min_interval`` 是 5 分钟，体检不该消耗用户的限流额度。

        原理：``_m_h5_tk`` 的格式是 ``{token}_{过期时间戳毫秒}``，那个时间戳
        就是 Cookie 的到期时刻。所以 Cookie 有没有过期、还剩多久，看一眼就知道，
        不必真去撞一次「令牌过期」的错误。

        这一点很重要：本项目的 Cookie 只有几小时寿命，如果每次体检都靠
        ``fetch`` 撞错误，用户会频繁看到红色告警却什么也没做——**告警疲劳
        等于没有告警**。提前告知「还剩 2 小时，去登录」才是有用的。

        注意：这里读的是 ``.env`` 里的 Cookie。如果 tasks.yaml 用
        ``params.cookie`` 覆盖了它，结论会不一致——那种配置本来就该改。
        """
        cookie = str(self.credentials.get("cookie") or "")
        raw = ""
        for chunk in cookie.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name.strip() == "_m_h5_tk" and value:
                raw = value
                break

        if not raw:
            return HEALTH_AUTH, (
                "Cookie 里没有 _m_h5_tk，算不出 mtop 签名。"
                "跑一次 radar login damai 重新登录"
            )

        parts = raw.split("_")
        if len(parts) < 2 or not parts[-1].isdigit():
            # 不认识就不猜。格式变了就如实说「不认识」，让人实测，别报一个
            # 看起来很确定的错误时间。
            return HEALTH_UNKNOWN, (
                "Cookie 里 _m_h5_tk 的格式不认识（预期 {token}_{时间戳}），"
                "无法本地判断是否过期；用 radar check 实测一次"
            )

        ts = int(parts[-1])
        ts = ts / 1000 if ts > 1e11 else ts  # 兼容秒级时间戳
        left = ts - time.time()
        if left <= 0:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            return HEALTH_AUTH, (
                f"Cookie 已于 {when} 过期（超期 {abs(left) / 3600:.0f} 小时）。"
                "跑一次 radar login damai 重新登录"
            )
        return HEALTH_OK, f"Cookie 有效，还剩 {left / 3600:.1f} 小时（本地推算，未联网）"

    # -- 请求 ---------------------------------------------------------------

    async def build_request(
        self, task: TaskConfig, client: httpx.AsyncClient
    ) -> tuple[str, str, dict[str, Any]]:
        params = task.params
        cookie = str(params.get("cookie") or self.credentials.get("cookie") or "").strip()
        # 刷新过的新 token 优先。Cookie 里那条可能正是失效的那条——
        # 如果这里写成 cookie 优先，重试时会拿同一个过期 token 再签一次，白试。
        token = self._token or parse_mtop_token(cookie)

        api = str(self.param(params, "api", DEFAULT_API))
        version = str(self.param(params, "version", DEFAULT_VERSION))
        app_key = str(self.param(params, "app_key", DEFAULT_APP_KEY))
        jsv = str(self.param(params, "jsv", "2.6.1"))

        payload = self.param(params, "data", None)
        if not isinstance(payload, dict):
            payload = {}
            # 允许用 item_id / project_id 直接拼最常见的入参
            for key in ("item_id", "project_id", "itemId", "projectId"):
                if params.get(key):
                    payload["itemId"] = str(params[key])
                    break
        # mtop 的 data 必须紧凑序列化：空格会影响 sign
        data_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        timestamp = int(dt.datetime.now().timestamp() * 1000)
        if not token:
            raise AuthError(
                "大麦接口需要登录 Cookie 里的 _m_h5_tk 才能计算签名。\n"
                "做法：浏览器登录大麦 → F12 → Application → Cookies → 复制 "
                "_m_h5_tk 与 _m_h5_tk_enc 等整条 Cookie，填进 .env 的 DAMAI_COOKIE。\n"
                "（本工具不会代你登录，也不会处理验证码。）"
            )
        sign = mtop_sign(token, timestamp, app_key, data_str)

        query = {
            "jsv": jsv,
            "appKey": app_key,
            "t": str(timestamp),
            "sign": sign,
            "api": api,
            "v": version,
            "type": "originaljson",
            "dataType": "json",
            "data": data_str,
        }
        extra = params.get("query")
        if isinstance(extra, dict):
            query.update({str(k): str(v) for k, v in extra.items()})

        headers = {
            "Referer": str(self.param(params, "referer", DEFAULT_REFERER)),
            "Cookie": cookie,
            "Accept": "application/json",
            "User-Agent": H5_USER_AGENT,
        }
        headers.update(
            {str(k): str(v) for k, v in (params.get("headers") or {}).items()}
        )

        url = f"{MTOP_BASE}/h5/{api}/{version}/"
        return "GET", url, {"params": query, "headers": headers}

    async def fetch_raw(self, task: TaskConfig, client: httpx.AsyncClient) -> Any:
        """请求一次；token 失效时用响应下发的新 token 重签一次。"""
        last: Any = None
        for attempt in range(2):
            method, url, kwargs = await self.build_request(task, client)
            try:
                resp = await client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise TransportError(f"大麦请求失败：{exc}") from exc
            if resp.status_code == 429:
                raise RateLimitedError("大麦返回 HTTP 429，触发了限流。引擎会自动退避后重试。")
            if resp.status_code != 200:
                raise AdapterError(
                    f"大麦接口返回 HTTP {resp.status_code}。"
                    "遇到 403/412 多为风控拦截，请降低频率或稍后再试，不要尝试绕过。"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise ParseError(
                    f"大麦返回的不是 JSON（前 200 字符：{resp.text[:200]!r}）"
                ) from exc

            if is_token_expired(data):
                if attempt == 0:
                    fresh = resp.cookies.get("_m_h5_tk") or client.cookies.get("_m_h5_tk")
                    new_token = parse_mtop_token(f"_m_h5_tk={fresh}" if fresh else "")
                    if new_token:
                        log.debug("大麦 token 失效，换用响应下发的新 token 重试一次")
                        self._token = new_token
                        continue
                # 重试过一次仍然失效：登录态是真的过期了，用户得重新登录
                raise AuthError(
                    "大麦登录态已失效（令牌过期）。请重新登录："
                    "radar login damai —— 登录完关掉浏览器窗口即可，Cookie 自动回写。"
                )
            last = data
            break
        return last

    # -- 解析 ---------------------------------------------------------------

    def parse_payload(self, data: Any, task: TaskConfig) -> dict[str, TrainState]:
        params = task.params
        # 1) 优先按「巡演各站在售状态」解析——详情接口里唯一稳定的可购信号
        tour = parse_damai_tour(
            data,
            item_id=str(params.get("item_id") or params.get("project_id") or ""),
            city=str(params.get("city") or ""),
        )
        if tour:
            return tour
        # 2) 退回「场次 + 票档」结构（接口漂回旧形态时仍能用）
        trains = parse_damai_payload(data)
        if trains:
            return trains
        # 3) 结构完全不认识时退回通用解析，让用户可以纯靠 items_path 配置接上
        return super().parse_payload(data, task)


__all__ = [
    "DEFAULT_API",
    "DEFAULT_APP_KEY",
    "MTOP_BASE",
    "PERFORM_PATHS",
    "SKU_PATHS",
    "DamaiAdapter",
    "is_token_expired",
    "mtop_sign",
    "parse_damai_payload",
    "parse_mtop_token",
]
