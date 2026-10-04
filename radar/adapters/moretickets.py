"""摩天轮票务（MoreTickets）余票适配器。

**同样不需要登录，也没有签名。** 三个接口都是 ``POST + JSON``，
网关域名是 ``unify.moretickets.com``，路径前缀带 ``pub`` 的就是公开的。

接口是怎么找到的
----------------
``moretickets.com`` 前台是普通 SPA，网络面板里一眼就能看到全部请求：

======================================  ==========================================
演出搜索（**注意没有 pub**）               ``POST /user/foundation/show/search/v1``
                                          ``{"keyword":"...","offset":0,"length":20}``
演出列表                                  ``POST .../pub/show/list/v1``
                                          ``{"sorting":"HOT_WEIGHT","offset":0,"length":20}``
巡演各站场次（含余票）                     ``POST .../pub/session/city/list/v1``
                                          ``{"tourId":"..."}``
======================================  ==========================================

``moretickets.com`` 只认 ``POST + application/json``。同一个地址用 GET 会返回
一个 111 字节的空壳（``{"statusCode":12123,"message":"An unknown error..."}``）
——那个 12123 不是业务状态，是「你请求的姿势不对」，别拿它当平台故障。

「有没有票」看哪个字段
----------------------
场次对象里的 ``hasTicket`` 是布尔值，这是**最干净的余票信号**：没有数字、
没有文案歧义。同时还有 ``sessionStatusDesc``（``"Sold Out"`` / 空串）作为
人类可读的旁证，但判定只认 ``hasTicket``——文案会随语言变（站点有中/英/繁三套），
布尔值不会。

``hasTicket`` 缺失时**跳过该场次**，不猜。读不到就当没这一场，
符合本项目「宁可漏报不误报」的一贯取舍。

这是二手票平台
--------------
摩天轮的「票」是第三方卖家挂单，不是官方直售。所以：

* 挂单会随卖家上下架剧烈波动，建议 ``notify_on`` 只留 ``["appeared"]``；
* 一个巡演通常只有部分城市/场次挂得上票，「某站没挂单」是常态，不是故障；
* 价格含卖家溢价，买之前自己判断风险。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from ..config import TaskConfig
from ..models import SeatAvailability, Snapshot, TrainState
from ..geo import city_matches
from .base import HEALTH_BROKEN, HEALTH_OK, Adapter, AdapterError, Capability
from .registry import register

log = logging.getLogger("radar.adapters.moretickets")

#: 网关根。所有业务接口都在 ``/user/foundation`` 下面。
UNIFY_BASE = "https://unify.moretickets.com/user/foundation"
#: 公开前缀。搜索接口是个例外——它**没有** ``pub``，但同样匿名可用。
PUBLIC = "/pub"
WEB_BASE = "https://moretickets.com"

#: 席别名。**必须是固定值**，理由同 damai / maoyan：拿状态原文当 key，
#: 状态一变 key 就变，diff 会把「改名」误读成「没了又来了」。
SEAT_NAME = "售票状态"
#: 判定「卖光了」的文案（小写比较）。只在 ``hasTicket`` 缺失时兜底。
SOLD_OUT_TEXTS = ("sold out", "售罄", "已售完", "缺货")

#: 每个请求都要带的头。UA 用浏览器的——默认的 ``python-httpx/x.y``
#: 对任何网关都是「我是脚本」，能省则省。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
    "Origin": WEB_BASE,
    "Referer": WEB_BASE + "/",
}


# ---------------------------------------------------------------------------
# 请求 / 解析（纯函数）
# ---------------------------------------------------------------------------


def search_path() -> str:
    """演出搜索路径。刻意不走 ``PUBLIC`` —— 它真的没有那段前缀。"""
    return "/show/search/v1"


def list_path() -> str:
    return f"{PUBLIC}/show/list/v1"


def session_path() -> str:
    return f"{PUBLIC}/session/city/list/v1"


def unwrap(data: Any, *, what: str) -> Any:
    """拆信封。非 200 就报错，别把 12123 那种「姿势不对」当空数据。"""
    if not isinstance(data, dict):
        raise AdapterError(f"摩天轮{what}返回的不是对象（{type(data).__name__}）。")
    code = data.get("statusCode")
    if code != 200:
        raise AdapterError(
            f"摩天轮{what}失败：statusCode={code!r} {data.get('message')!r}。\n"
            "常见原因：接口改用 GET 调了（它只认 POST）、tourId 过期、"
            "或网关临时不可用。"
        )
    return data


def parse_search_items(data: Any) -> list[dict[str, Any]]:
    """搜索/列表响应 → 候选行。"""
    payload = unwrap(data, what="搜索")
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("tourId")]


def parse_sessions(
    data: Any,
    *,
    city: str = "",
) -> dict[str, TrainState]:
    """场次响应 → ``{场次键: TrainState}``。

    :param city: 只保留城市名/区域名含该字串的场次。巡演常横跨十几个城市，
        不过滤的话每个城市售罄都会推一条，纯噪音。
    """
    payload = unwrap(data, what="场次")
    body = payload.get("data")
    if not isinstance(body, dict):
        raise AdapterError(
            "摩天轮场次响应里没有 data 对象。\n"
            f"响应键：{sorted(payload)[:12]}"
        )

    groups = body.get("sessionGroupList")
    if not isinstance(groups, list):
        raise AdapterError(
            "摩天轮场次响应里没有 sessionGroupList —— 结构变了，需要重新核对字段。\n"
            f"data 键：{sorted(body)[:12]}"
        )

    trains: dict[str, TrainState] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        region = str(group.get("regionName") or "").strip()
        sessions = group.get("sessionList")
        if not isinstance(sessions, list):
            continue
        for row in sessions:
            if not isinstance(row, dict):
                continue

            # 两个「地点」字段的含义完全不同，别只用其中一个：
            #   regionName → 真实城市，形如 "Guangzhou, CN" / "Melbourne, AU"
            #   cityName   → 国家级，形如 "China" / "Australia"
            # 拿 cityName 当键，整个中国巡演会挤在一个 "China" 下面，
            # 也没法按城市过滤。所以取更具体的 regionName，country 兜底。
            country = str(row.get("cityName") or "").strip()
            place = region or country
            # 用 geo.city_matches 而不是 `city in part`：平台返回的是
            # "Guangzhou, CN"，用户说的是「广州」，子串匹配永远不成立——
            # 那样配了 city 参数的任务会一条场次都抓不到，而且不报错。
            if city and not city_matches(place, city):
                continue

            seat = _seat_of(row, place)
            if seat is None:
                continue

            session_id = str(row.get("sessionId") or "").strip()
            when = str(row.get("sessionName") or "").strip()
            if not session_id or not when:
                continue

            # 场次键带上地点：同一个巡演里两地同点开场是常态，
            # 只用时刻当键会把两场并成一场。
            code = f"{place} {when}".strip() if place else when
            trains[code] = TrainState(
                train_code=code,
                from_station=str(row.get("showName") or "").strip(),
                to_station=str(row.get("venueName") or "").strip(),
                # depart_time 留空：时刻已经在 code 里了，再填一次通知里会重复。
                seats={SEAT_NAME: seat},
                extra={"session_id": session_id, "region": region},
            )
    return trains


def _seat_of(row: dict[str, Any], place: str) -> SeatAvailability | None:
    """从场次行里读出一个席别状态。读不出来返回 ``None``（跳过，不猜）。"""
    has_ticket = row.get("hasTicket")
    desc = str(row.get("sessionStatusDesc") or "").strip()
    price = _price_of(row)
    currency = str(row.get("currencySymbol") or "").strip() or "¥"

    if isinstance(has_ticket, bool):
        return SeatAvailability(
            seat_type=SEAT_NAME,
            # 保留平台原话当 raw：站点自己写的就是「Sold Out」，
            # 换成「无票」反而丢了语言信息（它还有中/繁两套）。
            raw=desc or ("有票" if has_ticket else "无票"),
            count=None if has_ticket else 0,
            available=has_ticket,
            price=price,
            currency=currency,
        )

    # hasTicket 缺失时退一步看文案，仍然看不懂就跳过。
    if desc:
        if desc.lower() in SOLD_OUT_TEXTS:
            return SeatAvailability(SEAT_NAME, desc, 0, False, price, currency)
        log.debug("摩天轮场次状态看不懂，跳过：place=%s desc=%r", place, desc)
    return None


def _price_of(row: dict[str, Any]) -> float | None:
    """取该场次的最低挂单价。拿不到返回 ``None``（不参与 diff，只用于展示）。"""
    price = row.get("price")
    if not isinstance(price, dict):
        return None
    raw = price.get("minSalePrice")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def describe_item(row: dict[str, Any]) -> str:
    """候选行 → 一行摘要（``radar find`` 用）。"""
    price = row.get("price") if isinstance(row.get("price"), dict) else {}
    return (
        f"{row.get('tourId')}\t"
        f"{row.get('title') or row.get('showName')}\t"
        f"{row.get('location') or ''}\t"
        f"{row.get('showDate') or ''}\t"
        f"{row.get('status') or ''}\t"
        f"{price.get('minSalePrice') or ''}"
    )


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


@register("moretickets")
class MoreTicketsAdapter(Adapter):
    """摩天轮票务余票。公开接口，无需凭据。"""

    name = "moretickets"
    requires_credentials = False
    base_url = UNIFY_BASE

    capability = Capability(
        category="show",
        summary="摩天轮票务（二手票挂单，公开接口，无需登录）",
        can_search=True,
        seat_level=True,
        regions=("CN", "HK", "TW", "JP", "KR", "SG", "MY", "TH", "US", "GB", "AU"),
        limitation="二手票平台，库存随卖家挂单波动，价格与票档都不稳定；"
        "没有卖家挂单的演出查不到。",
    )
    #: 二手挂单变化比官方票快，但本项目只做低频只读——5 分钟一轮是刻意的上限。
    min_interval = 300.0

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：最小规模搜一次。摩天轮全部是公开端点，不需要凭据。"""
        try:
            rows = await search_tours(client, "演唱会", length=1)
        except AdapterError as exc:
            return HEALTH_BROKEN, str(exc)
        if not rows:
            return HEALTH_BROKEN, "搜索接口通，但一条结果都没返回，搜索参数可能已失效"
        return HEALTH_OK, f"搜索接口可达（拿到 {len(rows)} 条样本），无需登录"

    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        params = task.params
        tour_id = str(params.get("tour_id") or params.get("tourId") or "").strip()
        keyword = str(params.get("keyword") or "").strip()

        if not tour_id:
            if not keyword:
                raise AdapterError(
                    f"任务 {task.id} 既没给 tour_id 也没给 keyword。\n"
                    "两种写法选一个：\n"
                    "  params: {tour_id: \"6a2a300f941d1b00014a8828\"}   # 推荐\n"
                    "  params: {keyword: \"Jay Chou\"}                  # 自动搜\n"
                    "不知道 tour_id 就先跑：radar find moretickets Jay Chou"
                )
            picked = await self.resolve(client, keyword)
            tour_id = str(picked["tourId"])
            log.info("[%s] 关键词 %r 解析到 tourId=%s", task.id, keyword, tour_id)

        payload = await self.post_json(client, session_path(), {"tourId": tour_id})
        trains = parse_sessions(payload, city=str(params.get("city") or "").strip())
        if not trains:
            city_hint = f"（city={params.get('city')!r} 过滤后为空）" if params.get("city") else ""
            log.warning("[%s] 摩天轮 tourId=%s 没解析出任何场次%s", task.id, tour_id, city_hint)

        show_name = next((t.from_station for t in trains.values() if t.from_station), "")
        return Snapshot(
            task_id=task.id,
            platform=self.name,
            captured_at=dt.datetime.now(dt.timezone.utc),
            context={"演出": show_name or tour_id},
            trains=trains,
        )

    async def resolve(self, client: httpx.AsyncClient, keyword: str) -> dict[str, Any]:
        """关键词 → 第一个候选巡演。"""
        rows = await search_tours(client, keyword)
        if not rows:
            raise AdapterError(
                f"摩天轮搜不到「{keyword}」。摩天轮是二手票平台，"
                "冷门或纯内地场次很可能根本没有挂单——这不是配置问题。"
            )
        return rows[0]

    async def post_json(
        self, client: httpx.AsyncClient, path: str, body: dict[str, Any]
    ) -> Any:
        """POST 一个 JSON。**必须 POST** —— GET 会拿到 12123 空壳。"""
        url = UNIFY_BASE + path
        try:
            resp = await client.post(url, json=body, headers=DEFAULT_HEADERS)
        except httpx.HTTPError as exc:
            raise AdapterError(f"摩天轮请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise AdapterError(
                f"摩天轮 {url} 返回 HTTP {resp.status_code}。"
                "403/412 一般是风控拦截，降低频率或稍后再试，不要尝试绕过。"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(
                f"摩天轮 {url} 返回的不是 JSON（前 200 字符：{resp.text[:200]!r}）。"
            ) from exc


async def search_tours(
    client: httpx.AsyncClient,
    keyword: str,
    *,
    length: int = 20,
) -> list[dict[str, Any]]:
    """按关键词搜巡演，返回候选行。供 ``radar find`` 与适配器共用。"""
    adapter = MoreTicketsAdapter()
    payload = await adapter.post_json(
        client,
        search_path(),
        {"keyword": keyword, "homeUiExperiment": True, "offset": 0, "length": length},
    )
    return parse_search_items(payload)


__all__ = [
    "DEFAULT_HEADERS",
    "MoreTicketsAdapter",
    "PUBLIC",
    "SEAT_NAME",
    "SOLD_OUT_TEXTS",
    "UNIFY_BASE",
    "WEB_BASE",
    "describe_item",
    "list_path",
    "parse_search_items",
    "parse_sessions",
    "search_path",
    "search_tours",
    "session_path",
    "unwrap",
]
