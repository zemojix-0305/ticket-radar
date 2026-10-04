"""目标解析：把「一个需求」定位到「一个具体要盯的东西」。

:mod:`radar.intent` 回答「用户想盯什么」，这一层回答「**具体盯哪一个**」。

三种策略
--------

============  ==========================================================
``direct``    链接里已经有 id（大麦、猫眼），不用搜，直接定位
``route``     铁路：出发地 + 到达地 + 日期本身就是唯一目标，不用搜
``search``    只有演出名——搜平台，把候选列出来让人挑
============  ==========================================================

为什么要分这么清：搜索是**有代价**的（消耗平台请求配额、可能撞限流、
可能失败），能不用搜就不用。实测大麦 ``min_interval`` 是 5 分钟，
拿搜索去试探一个链接里就带着 id 的需求，纯属浪费。

搜索结果不可信，所以要验证
--------------------------
平台搜索索引**会滞后**。本项目实测踩过：同一时刻猫眼列表报「预售」，
详情页报「在售中」——详情才权威。所以 :func:`resolve` 搜出候选之后，
真正要盯的那个必须过一遍 :func:`verify`（用真实接口抓一次）。

**搜索是「可能对」，验证是「确实对」。** 把两者混为一谈，
用户会建出一个盯错对象的监控，而且要等下一次变化才发现。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import httpx

from .adapters import create_adapter
from .capability import TRAIN, Requirement, route
from .config import TaskConfig, WatchRule

# 城市匹配逻辑住在 radar.geo：moretickets 适配器按城市过滤场次时也要用，
# 而本模块已经 import 了 radar.adapters，反向引用会成环，所以往上抽一层。
# 下面保留 _ 前缀别名，只是为了让本模块内部少写几个字。
from .geo import city_matches as _city_matches
from .geo import normalize_city as _normalize_city
from .intent import PLATFORM_ID_KEY, ParsedIntent

log = logging.getLogger(__name__)

#: 搜索时每个平台取几条候选。多了手机上读不完，少了容易漏掉目标场次。
DEFAULT_CANDIDATES = 8


@dataclasses.dataclass
class Target:
    """一个具体可监控的目标。

    「可监控」的含义是：``params`` 里的东西直接抄进 tasks.yaml 就能跑。
    """

    platform: str
    target_id: str = ""
    id_key: str = ""
    label: str = ""
    place: str = ""
    city: str = ""                # 归一化后的城市，用于消歧
    when: str = ""
    price: str = ""
    status: str = ""
    params: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def title(self) -> str:
        bits = [b for b in (self.label, self.place, self.when) if b]
        return "　".join(bits) if bits else self.target_id or self.platform

    def summary(self) -> str:
        bits = [self.target_id or "(无 id)"]
        if self.status:
            bits.append(self.status)
        if self.price:
            bits.append(self.price)
        return "　".join(bits)


@dataclasses.dataclass
class ResolveResult:
    """定位结论。"""

    intent: ParsedIntent
    strategy: str = "none"          # direct / route / search / none
    targets: list[Target] = dataclasses.field(default_factory=list)
    searched: list[str] = dataclasses.field(default_factory=list)
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    #: 搜到了但被城市/演出名筛掉的。**要展示给用户看**——
    #: 「搜到过 7 条但都不是你要的」比「没找到」信息量大得多，
    #: 用户能据此判断是换个关键词还是这场真的没票
    rejected: list[Target] = dataclasses.field(default_factory=list)
    #: 城市/演出名筛过之后一条都不剩——调用方要显式提示，不能假装「找到了 N 个」
    city_miss: bool = False

    @property
    def unique(self) -> bool:
        """候选唯一吗——唯一就不用让用户挑。"""
        return len(self.targets) == 1

    def outcome(self) -> str:
        """结局，供 CLI 决定渲染方式。

        ``found`` 有候选 / ``unique`` 只有一个 / ``none`` 没找到 /
        ``too_many`` 候选太多需要缩小范围
        """
        if not self.targets:
            return "none"
        if len(self.targets) == 1:
            return "unique"
        if len(self.targets) > 12:
            return "too_many"
        return "found"


# --- 候选构造 ---------------------------------------------------------------


def _target_from_row(platform: str, row: dict[str, Any]) -> Target:
    """把平台搜索结果的一条原始记录变成 :class:`Target`。"""
    if platform == "maoyan":
        from .adapters.maoyan import status_label

        raw_status = row.get("ticketStatus")
        place = str(row.get("cityName") or "")
        return Target(
            platform=platform,
            target_id=str(row.get("performanceId") or ""),
            id_key=PLATFORM_ID_KEY["maoyan"],
            label=str(row.get("name") or ""),
            place=place,
            city=_normalize_city(place),
            when=str(row.get("showTimeRange") or ""),
            price=str(row.get("priceRange") or ""),
            status=status_label(raw_status) or str(raw_status or ""),
            params={"performance_id": str(row.get("performanceId") or "")},
        )

    price = row.get("price") if isinstance(row.get("price"), dict) else {}
    tour_id = str(row.get("tourId") or "")
    place = str(row.get("location") or "")
    return Target(
        platform=platform,
        target_id=tour_id,
        id_key=PLATFORM_ID_KEY["moretickets"],
        label=str(row.get("title") or row.get("showName") or ""),
        place=place,
        city=_normalize_city(place),
        when=str(row.get("showDate") or ""),
        price=str(price.get("minSalePrice") or ""),
        status=str(row.get("status") or ""),
        params={"tour_id": tour_id},
    )


# --- 定位 -------------------------------------------------------------------


async def _search_one(
    client: httpx.AsyncClient, platform: str, keyword: str, limit: int
) -> list[Target]:
    from .adapters.maoyan import search_performances
    from .adapters.moretickets import search_tours

    if platform == "maoyan":
        rows = await search_performances(client, keyword, size=limit)
    elif platform == "moretickets":
        rows = await search_tours(client, keyword, length=limit)
    else:
        return []
    return [t for t in (_target_from_row(platform, r) for r in rows) if t.target_id]


async def resolve(
    client: httpx.AsyncClient,
    intent: ParsedIntent,
    *,
    limit: int = DEFAULT_CANDIDATES,
    platforms: list[str] | None = None,
    need_search: bool | None = None,
) -> ResolveResult:
    """把需求定位到具体目标。**只在真的需要时才发请求。**"""
    result = ResolveResult(intent=intent)

    # 策略一：链接里已经有 id —— 不搜
    if intent.target_id and intent.platform:
        result.strategy = "direct"
        result.targets.append(
            Target(
                platform=intent.platform,
                target_id=intent.target_id,
                id_key=intent.id_key,
                label="（来自链接，尚未验证）",
                params={intent.id_key: intent.target_id},
            )
        )
        result.notes.append("链接里带场次 id，已直接定位，不用搜索")
        return result

    # 策略二：铁路 —— 出发地+到达地+日期本身就是目标
    if intent.route_from and intent.route_to:
        result.strategy = "route"
        params: dict[str, Any] = {"from": intent.route_from, "to": intent.route_to}
        if intent.date:
            params["date"] = intent.date
        result.targets.append(
            Target(
                platform="rail12306",
                label=f"{intent.route_from} → {intent.route_to}",
                when=intent.date,
                params=params,
            )
        )
        result.notes.append("铁路按线路 + 日期定位，不需要搜索")
        return result

    # 策略三：靠搜索
    wants_search = intent.needs_search if need_search is None else need_search
    if not wants_search or not intent.keyword:
        result.notes.append(
            "既没有链接里的 id，也没有线路或关键词，没法定位到具体目标"
        )
        return result

    if platforms is None:
        req: Requirement = intent.requirement()
        if intent.category == TRAIN:
            # 铁路不靠搜索，靠线路；走到这里说明线路没识别出来
            result.notes.append("这是铁路需求，但没解析出「A 到 B」的线路，请写清楚")
            return result
        platforms = [c.platform for c in route(req).candidates]
    platforms = [p for p in platforms if p in ("maoyan", "moretickets")]

    if not platforms:
        result.notes.append("当前没有支持关键词搜索的平台（猫眼 / 摩天轮）")
        return result

    result.strategy = "search"
    for platform in platforms:
        try:
            found = await _search_one(client, platform, intent.keyword, limit)
        except Exception as exc:  # 单个平台失败不该拖垮整个解析
            log.debug("搜索 %s 失败：%s", platform, exc)
            result.failed.append((platform, str(exc)))
            continue
        result.searched.append(platform)
        result.targets.extend(found)
        # 平台间串行 + 间隔，别把人家打爆（实测限流是真实存在的）
        await asyncio.sleep(0.4)

    # 城市消歧：用户说了城市就按城市筛。
    #
    # 不筛的后果实测过：搜「陈粒深圳场」返回 7 条，里面有陈粒在贵阳、三亚、
    # 广州、临沂的场次，**一条深圳的都没有**，另有香港的无关演出。
    # 列表里全是「看起来很像但就是不对」的东西，比明确说「没找到」糟得多。
    if intent.city and result.targets:
        wanted = intent.city
        narrowed = [t for t in result.targets if _city_matches(t.city, wanted)]
        if narrowed:
            result.notes.append(
                f"按城市「{wanted}」筛过：{len(result.targets)} → {len(narrowed)} 条"
            )
            result.targets = narrowed
        else:
            result.notes.append(
                f"{len(result.targets)} 条候选里没有一条在「{wanted}」"
            )
            result.rejected.extend(result.targets)
            result.targets = []
            result.city_miss = True

    # 演出名过滤：**这是准确性的最后一道闸**。
    #
    # 实测踩过：搜「陈粒深圳场」，城市筛完之后剩下 1 条——
    # 「Jordan Chan BIGMAN Concert Tour In Shenzhen」。城市对，演出名不对。
    # 平台搜索是模糊匹配，拿它当答案就等于给用户一个**盯错的监控**，
    # 而且要等下一次变化才会发现盯错了。
    #
    # 对不上的不删掉，挪进 ``rejected``：搜索确实搜到了东西，
    # 告诉用户「搜到了但都不是你要的」比只说「没找到」有用得多。
    if intent.keyword and result.targets:
        kw = intent.keyword.lower()
        named = [t for t in result.targets if kw in t.label.lower()]
        if named:
            if len(named) < len(result.targets):
                result.notes.append(
                    f"按演出名「{intent.keyword}」筛过："
                    f"{len(result.targets)} → {len(named)} 条"
                )
            result.targets = named
        else:
            result.notes.append(
                f"搜到的 {len(result.targets)} 条里，没有一条演出名含「{intent.keyword}」"
            )
            result.rejected.extend(result.targets)
            result.targets = []
            result.city_miss = True

    if not result.targets and result.failed:
        result.notes.append(
            "搜索失败：" + "；".join(f"{p}（{e[:60]}）" for p, e in result.failed)
        )
    elif not result.targets:
        result.notes.append(f"在 {'、'.join(result.searched)} 上没搜到「{intent.keyword}」")
    elif len(result.targets) > 12:
        result.notes.append(
            f"找到 {len(result.targets)} 个候选，太多了。往需求里加城市或日期能收窄范围"
        )
    else:
        result.notes.append(f"找到 {len(result.targets)} 个候选，确认一下是哪个场次")

    return result


# --- 验证 -------------------------------------------------------------------


async def verify(client: httpx.AsyncClient, target: Target) -> tuple[bool, str]:
    """用真实接口抓一次，确认这个目标**真的盯得到**。

    返回 ``(是否可用, 说明)``。

    为什么必须验证：搜索索引会滞后（本项目实测：列表报「预售」、详情报
    「在售中」），而且用户可能贴了个错的 id。**搜索是「可能对」，
    验证是「确实对」。**

    验证失败不抛异常——它只是一个「不行」的答案，调用方要照实显示。
    """
    task = TaskConfig(
        id="verify",
        adapter=target.platform,
        params=dict(target.params),
        watch=WatchRule(),
        link="",
    )
    try:
        adapter = create_adapter(target.platform)
        snapshot = await adapter.fetch(task, client)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:160]

    count = len(snapshot.trains)
    if count == 0:
        return False, "接口通了，但一条数据都没有——id 可能不对"
    first = next(iter(snapshot.trains.items()), None)
    detail = ""
    if first:
        code, train = first
        seats = [s for s in train.seats.values() if s.available]
        detail = f"，首条 [{code}] {train.from_station}→{train.to_station}"
        if seats:
            detail += f"　{'、'.join(f'{s.seat_type} {s.raw or s.count}' for s in seats[:2])}"
    return True, f"验证通过：抓到 {count} 个条目{detail}"


def to_yaml_fragment(target: Target, *, interval: int = 300) -> str:
    """渲染成可以直接粘进 tasks.yaml 的片段。

    故意**不生成完整文件**：用户通常已经有一份 tasks.yaml，
    给整份文件等于让他覆盖掉别的任务。
    """
    slug = (
        (target.target_id or target.label or "target")[:16]
        .replace(" ", "-")
        .replace("/", "-")
    )
    lines = [
        f"- id: {target.platform}-{slug}",
        f"  adapter: {target.platform}",
        "  enabled: true",
        f"  interval_seconds: {interval}",
        f"  display_name: {target.label or target.title}",
        "  params:",
    ]
    for key, value in target.params.items():
        lines.append(f'    {key}: "{value}"')
    lines.append("  watch:")
    lines.append("    seat_types: []")
    lines.append("    min_count: 1")
    lines.append('    notify_on: ["appeared"]')
    return "\n".join(lines)


async def resolve_and_verify(
    client: httpx.AsyncClient,
    intent: ParsedIntent,
    *,
    limit: int = DEFAULT_CANDIDATES,
) -> tuple[ResolveResult, Target | None, str]:
    """定位 → 选一个 → 验证。返回 ``(定位结果, 选中的目标, 验证结论)``。

    只有一个候选时自动选中它——**多数时候用户就想要那一个**，
    逼他选一遍是白添麻烦。多个候选时不替他选（可能选错场次，
    那比多问一句糟糕得多），交回调用方。
    """
    result = await resolve(client, intent, limit=limit)
    if not result.targets:
        return result, None, ""
    chosen = result.targets[0] if result.unique else None
    if chosen is None:
        return result, None, ""
    ok, note = await verify(client, chosen)
    return result, (chosen if ok else None), note


__all__ = [
    "DEFAULT_CANDIDATES",
    "ResolveResult",
    "Target",
    "resolve",
    "resolve_and_verify",
    "to_yaml_fragment",
    "verify",
]
