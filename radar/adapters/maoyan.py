"""猫眼演出（格瓦拉）余票适配器。

**这个平台不需要登录，也不需要任何签名。** 这是实测结论，不是推测。

接口是怎么找到的
----------------
猫眼的演出网页版是 ``show.maoyan.com``（Next.js 服务端渲染，页面上那个
"格瓦拉生活网"就是它）。页面本身**不发任何数据请求**——内容由服务端渲染好
塞进 ``<script>__NEXT_DATA__ = {...}</script>``，所以从浏览器里抓不到接口。

真正的接口地址藏在打包好的 JS 里：前端模块里写了

    var c = "https://m.dianping.com/myshow";
    ... o.a.get(c + t + n + "sellChannel=" + 7) ...

也就是说演出数据的网关挂在**大众点评**域下，路径是 ``/myshow/ajax/...``。
它只认一个 ``sellChannel=7`` 参数，没有签名、没有 token、没有 Cookie。

用到的三个接口
--------------
======================  ====================================================
搜索演出                ``GET /ajax/performances/<分类>;st=0;k=<词>;p=<页>;s=<条>;tft=0?cityId=<城>&sellChannel=7``
演出详情                ``GET /ajax/performance/<performanceId>?sellChannel=7``
城市列表                ``GET /ajax/city/allMyCity?sellChannel=7``
======================  ====================================================

状态词表（从站点 JS 里挖出的官方原文，不是猜的）
----------------------------------------------
===  ==========
1    即将开售
2    预售
3    在售中
4    已售罄
5    已结束
11   即将预售
12   演出延期
===  ==========

「能不能买」的划分：``预售`` / ``在售中`` 算能买，其余算不能买。
把「即将开售」划到**不能买**是有意的——这样它开售的那一刻会产生一次
``appeared`` 事件，正好是用户最想收到的那条提醒。

一个必须知道的坑：列表和详情的状态会打架
----------------------------------------
实测（2026-09-30，陈粒广州站 501675）**同一时刻**：

* 搜索接口的列表项 → ``ticketStatus: 2``（预售）
* 详情接口        → ``ticketStatus: 3``（在售中）

详情是权威的（站点详情页自己就用它）。列表那份是搜索索引里的旧值。
所以本适配器**只信详情接口**：即使配置里给了关键词，也要先从列表拿到 id，
再回头请求一次详情。多一次请求是值得的，读旧状态会让整条提醒链失效。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from urllib.parse import quote

import httpx

from ..config import TaskConfig
from ..models import SeatAvailability, Snapshot, TrainState
from .base import HEALTH_BROKEN, HEALTH_OK, Adapter, AdapterError, Capability
from .registry import register

log = logging.getLogger("radar.adapters.maoyan")

#: 演出数据网关。注意：**在 m.dianping.com 下**，不在 maoyan.com 下。
MYSHOW_BASE = "https://m.dianping.com/myshow"
#: 前端固定带的渠道号，缺了会被网关退回。
DEFAULT_SELL_CHANNEL = 7
#: 站点默认城市（10 = 上海）。关键词搜索一般够用，城市只影响候选排序。
DEFAULT_CITY_ID = 10
#: 分类：0 = 全部。搜索时不筛分类，因为「陈粒」这类词可能横跨演唱会/音乐节。
ALL_CATEGORIES = 0
SEARCH_PAGE_SIZE = 20

#: ``ticketStatus`` → 官方文案。原文来自站点 JS 里的映射表。
TICKET_STATUS_LABELS: dict[int, str] = {
    1: "即将开售",
    2: "预售",
    3: "在售中",
    4: "已售罄",
    5: "已结束",
    11: "即将预售",
    12: "演出延期",
}

#: 算「能买到」的状态文案。切换成这两者时不会互相触发提醒（都是能买）。
AVAILABLE_LABELS = ("预售", "在售中")
#: 算「买不到」的状态文案。注意把「即将开售」也算进来——
#: 它变成「在售中」时正好推一条，那才是用户真正想等的。
UNAVAILABLE_LABELS = ("即将开售", "即将预售", "已售罄", "已结束", "演出延期")

#: 席别名。**必须是固定值**：拿状态原文当 key，状态一变 key 就变，
#: diff 会把「改名」误读成「没了又来了」。这条和 damai 适配器同源。
SEAT_NAME = "售票状态"

#: 站点分享链接前缀，用于 404 时提示用户手动核对。
WEB_DETAIL_URL = "https://show.maoyan.com/detail/{performance_id}"

#: 每个请求都要带的头。**UA 不能省**：实测缺了浏览器 UA 网关直接回 403，
#: 而 httpx 默认的 UA 是 ``python-httpx/x.y``，一眼就能被认出来。
#: 这一条是踩过才加的——手工探测时带了 UA 就通，接进适配器忘了带就 403。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://show.maoyan.com/",
}


# ---------------------------------------------------------------------------
# 请求构造（纯函数，便于单测）
# ---------------------------------------------------------------------------


def build_search_path(
    keyword: str,
    *,
    category_id: int = ALL_CATEGORIES,
    page: int = 1,
    size: int = SEARCH_PAGE_SIZE,
    city_id: int = DEFAULT_CITY_ID,
    sell_channel: int = DEFAULT_SELL_CHANNEL,
) -> str:
    """拼出搜索路径。

    参数是塞在**路径段**里的（``/1;st=0;k=xx;p=1``），不是查询串——
    这是网关自己的写法，改不了。``/`` 和 ``;`` 必须原样保留，
    只有关键词要 URL 编码。
    """
    path = f"/ajax/performances/{category_id};st=0;"
    if keyword.strip():
        path += f"k={quote(keyword.strip())};"
    path += f"p={page};s={size};tft=0"
    return f"{path}?cityId={city_id}&sellChannel={sell_channel}"


def build_detail_path(
    performance_id: str | int,
    *,
    sell_channel: int = DEFAULT_SELL_CHANNEL,
) -> str:
    return f"/ajax/performance/{performance_id}?sellChannel={sell_channel}"


def build_city_path(*, sell_channel: int = DEFAULT_SELL_CHANNEL) -> str:
    return f"/ajax/city/allMyCity?sellChannel={sell_channel}"


def unwrap(data: Any, *, what: str) -> Any:
    """拆开 ``{"code":200,"data":...}`` 这层信封，非 200 直接报错。

    网关出错时 ``code`` 不是 200、``msg`` 里带原因，HTTP 却仍是 200——
    不查这一层就会把错误体当成业务数据，然后在上层报一个莫名其妙的解析失败。
    """
    if not isinstance(data, dict):
        raise AdapterError(f"猫眼{what}返回的不是对象（{type(data).__name__}）。")
    code = data.get("code")
    if code != 200:
        raise AdapterError(
            f"猫眼{what}失败：code={code!r} msg={data.get('msg')!r}。\n"
            "code 非 200 通常是接口参数变了或网关临时不可用，稍后再试。"
        )
    return data.get("data")


# ---------------------------------------------------------------------------
# 解析（纯函数）
# ---------------------------------------------------------------------------


def status_label(raw: Any) -> str | None:
    """把 ``ticketStatus`` 数字翻成官方文案。看不懂返回 ``None``。"""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return TICKET_STATUS_LABELS.get(int(raw))
    except (TypeError, ValueError):
        return None


def parse_search_items(data: Any) -> list[dict[str, Any]]:
    """把搜索响应解析成候选列表，按相关度保留原顺序。"""
    items = unwrap(data, what="搜索")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        pid = row.get("performanceId")
        if pid in (None, ""):
            continue
        out.append(row)
    return out


def parse_detail(data: Any) -> TrainState:
    """把详情响应解析成一个 ``TrainState``。

    用 ``TrainState`` 表达一场演出是刻意的复用：状态机、限流、通知、存储
    全部不用改——「有没有票」在列车和演出上是同一个问题。
    """
    row = unwrap(data, what="详情")
    if not isinstance(row, dict):
        raise AdapterError(f"猫眼详情 data 不是对象（{type(row).__name__}）。")

    pid = row.get("performanceId")
    if pid in (None, ""):
        raise AdapterError(
            "猫眼详情里没有 performanceId，返回的可能是错误页。\n"
            f"响应键：{sorted(row)[:20]}"
        )

    label = status_label(row.get("ticketStatus"))
    if label is None:
        raise AdapterError(
            f"猫眼详情的 ticketStatus={row.get('ticketStatus')!r} 看不懂，"
            "不猜、按失败处理。\n"
            f"已知映射：{TICKET_STATUS_LABELS}\n"
            "如果猫眼新增了状态值，把官方文案补进 TICKET_STATUS_LABELS 即可。"
        )

    available = label in AVAILABLE_LABELS
    seat = SeatAvailability(
        seat_type=SEAT_NAME,
        raw=label,
        count=None if available else 0,
        available=available,
    )

    name = str(row.get("name") or "").strip()
    city = str(row.get("cityName") or "").strip()
    venue = str(row.get("shopName") or "").strip()
    return TrainState(
        # 用城市当去重键，而不是 performanceId：通知和状态播报里直接渲染
        # ``train_code``，一串数字对人不构成信息。城市名在同一次任务里是稳定的，
        # 不会引发「改名 = 没了又来了」的误判。
        train_code=city or name or str(pid),
        from_station=name,
        to_station=venue,
        depart_time=str(row.get("showTimeRange") or "").strip(),
        seats={SEAT_NAME: seat},
        extra={
            "performance_id": str(pid),
            # 票价区间和缺货登记开关都不参与 diff，只用于展示与排查。
            "price_range": str(row.get("priceRange") or ""),
            "stock_out_register": str(row.get("stockOutRegister") or ""),
        },
    )


def describe_item(row: dict[str, Any]) -> str:
    """把候选行渲染成一行给人看的摘要（``radar find`` 用）。"""
    return (
        f"{row.get('performanceId')}\t"
        f"{row.get('name')}\t"
        f"{row.get('cityName') or ''}\t"
        f"{row.get('showTimeRange') or ''}\t"
        f"{row.get('priceRange') or ''}"
    )


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


@register("maoyan")
class MaoyanAdapter(Adapter):
    """猫眼演出余票。公开接口，无需凭据。"""

    name = "maoyan"
    #: 需要登录的说法在这里是**错的**——实测匿名可用。不用凭据。
    requires_credentials = False
    base_url = MYSHOW_BASE

    capability = Capability(
        category="show",
        summary="猫眼演出票（公开接口，无需登录）",
        can_search=True,
        seat_level=True,
        regions=("CN",),
        limitation="只覆盖猫眼/大众点评演出频道在售的场次；"
        "已结束或仅线下渠道销售的场次查不到。",
    )
    #: 演出页面的余票是分钟级变化的，5 分钟一轮足够；再快也没有意义，
    #: 而且这个项目本来就只做低频只读。
    min_interval = 300.0

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：最小规模搜一次。猫眼无需凭据，所以「能不能用」全看接口通不通。"""
        try:
            rows = await search_performances(client, "演唱会", size=1)
        except AdapterError as exc:
            return HEALTH_BROKEN, str(exc)
        if not rows:
            # 搜得到接口但没结果 ≠ 坏了。但猫眼全站不可能没有「演唱会」，
            # 所以这更可能是搜索参数被平台改了——归到异常并说明。
            return HEALTH_BROKEN, "搜索接口通，但一条结果都没返回，搜索参数可能已失效"
        return HEALTH_OK, f"搜索接口可达（拿到 {len(rows)} 条样本），无需登录"

    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        params = task.params
        performance_id = str(
            params.get("performance_id") or params.get("id") or ""
        ).strip()
        keyword = str(params.get("keyword") or "").strip()

        if not performance_id:
            if not keyword:
                raise AdapterError(
                    f"任务 {task.id} 既没给 performance_id 也没给 keyword。\n"
                    "两种写法选一个：\n"
                    "  params: {performance_id: \"501675\"}   # 最省请求，推荐\n"
                    "  params: {keyword: \"陈粒\"}            # 自动搜，多一次请求\n"
                    "不知道 id 就先跑：radar find maoyan 陈粒"
                )
            picked = await self.resolve(task, client, keyword)
            performance_id = str(picked["performanceId"])
            log.info("[%s] 关键词 %r 解析到 %s", task.id, keyword, performance_id)

        data = await self.get_json(client, build_detail_path(performance_id))
        train = parse_detail(data)
        log.info("[%s] 猫眼 %s → %s", task.id, train.to_station or train.train_code,
                 train.seats[SEAT_NAME].raw)

        return Snapshot(
            task_id=task.id,
            platform=self.name,
            captured_at=dt.datetime.now(dt.timezone.utc),
            context={"演出": train.from_station or train.train_code},
            trains={train.train_code: train},
        )

    async def resolve(
        self, task: TaskConfig, client: httpx.AsyncClient, keyword: str
    ) -> dict[str, Any]:
        """用关键词搜出 performanceId。城市/分类可用 params 收窄。"""
        params = task.params
        path = build_search_path(
            keyword,
            category_id=int(params.get("category_id") or ALL_CATEGORIES),
            size=int(params.get("search_size") or SEARCH_PAGE_SIZE),
            city_id=int(params.get("city_id") or DEFAULT_CITY_ID),
            sell_channel=int(params.get("sell_channel") or DEFAULT_SELL_CHANNEL),
        )
        rows = parse_search_items(await self.get_json(client, path))
        if not rows:
            raise AdapterError(
                f"猫眼搜不到「{keyword}」。换个更短的关键词试试，"
                f"或用 radar find maoyan {keyword} 手工挑 id。"
            )

        city = str(params.get("city") or "").strip()
        if city:
            narrowed = [r for r in rows if city in str(r.get("cityName") or "")]
            if narrowed:
                rows = narrowed
        return rows[0]

    async def get_json(self, client: httpx.AsyncClient, path: str) -> Any:
        url = MYSHOW_BASE + path
        try:
            resp = await client.get(url, headers=DEFAULT_HEADERS)
        except httpx.HTTPError as exc:
            raise AdapterError(f"猫眼请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise AdapterError(
                f"猫眼 {url} 返回 HTTP {resp.status_code}。\n"
                "403/412 一般是风控拦截，降低频率或稍后再试，不要尝试绕过。"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(
                f"猫眼 {url} 返回的不是 JSON（前 200 字符：{resp.text[:200]!r}）。"
            ) from exc


async def search_performances(
    client: httpx.AsyncClient,
    keyword: str,
    *,
    category_id: int = ALL_CATEGORIES,
    size: int = SEARCH_PAGE_SIZE,
    city_id: int = DEFAULT_CITY_ID,
) -> list[dict[str, Any]]:
    """按关键词搜演出，返回候选行。供 ``radar find`` 使用。

    抽成模块级函数而不是只放在类里，是为了能在不构造任务的情况下调用——
    用户第一次接平台时手上还没有任务配置，这条路径必须走得通。
    """
    adapter = MaoyanAdapter()
    path = build_search_path(
        keyword, category_id=category_id, size=size, city_id=city_id
    )
    return parse_search_items(await adapter.get_json(client, path))


__all__ = [
    "ALL_CATEGORIES",
    "AVAILABLE_LABELS",
    "DEFAULT_CITY_ID",
    "DEFAULT_HEADERS",
    "DEFAULT_SELL_CHANNEL",
    "MYSHOW_BASE",
    "MaoyanAdapter",
    "SEAT_NAME",
    "TICKET_STATUS_LABELS",
    "UNAVAILABLE_LABELS",
    "WEB_DETAIL_URL",
    "build_city_path",
    "build_detail_path",
    "build_search_path",
    "describe_item",
    "parse_detail",
    "parse_search_items",
    "search_performances",
    "status_label",
    "unwrap",
]
