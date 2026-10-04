"""需求路由：把「用户想要什么」对上「哪个平台能办」。

这个模块存在的理由只有一个：**让「做不到」也能给出答案**。

监控工具面对一个需求，通常只有两种反应：办到了，或者沉默。第二种最坑——
用户以为在监控，其实那个平台从头就没被支持过。

所以这里的 :func:`route` 不返回「能用的平台」就完事，而是把**每个没被
选中的平台为什么不行**一并记录下来。加起来的保证是：

    任何一个需求进来，必定落到三种结局之一——
    办到了（有候选）、需要用户补一步（要登录 / 要自己找 ID）、
    或者明确告诉他做不到 + 做不到在哪。

这就是 README 里写的「100% 有归宿」。它不承诺什么都办得成，
但承诺**不会石沉大海**。
"""

from __future__ import annotations

import dataclasses

from .adapters import get_adapter_class, list_adapters
from .adapters.base import Capability

# -- 需求类别 ---------------------------------------------------------------

TRAIN = "train"
SHOW = "show"
FLIGHT = "flight"
GENERIC = "generic"

CATEGORIES: tuple[str, ...] = (TRAIN, SHOW, FLIGHT, GENERIC)

#: 展示用中文名。写路由逻辑时用常量，写给人看时用这个
CATEGORY_LABELS: dict[str, str] = {
    TRAIN: "交通票务",
    SHOW: "演出票务",
    FLIGHT: "航班",
    GENERIC: "任意接口",
}


def category_label(category: str) -> str:
    """类别的中文名。未知类别原样返回，不猜；空串表示「不限类别」。"""
    if not category:
        return "不限类别"
    return CATEGORY_LABELS.get(category, category)


# -- 数据结构 ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Requirement:
    """一个已经结构化的需求。

    第一期由调用方手工构造；第二期做完意图解析后，由解析器从用户
    的一句话里产出这个结构。**路由逻辑不关心它是怎么来的**，所以现在
    就能写、现在就能测，不用等意图解析完成。

    :param category: 需求类别，见模块顶部常量。**空串 = 不限类别**——
        解析器判不出「演出还是火车」时传空串，路由会把所有可用平台都列出来
        供用户挑，而不是一律拒绝。判不出类别不等于什么都干不了。
    :param need_search: 用户只给了模糊描述（如「周杰伦深圳场」），
        需要我们自己搜到具体场次。``True`` 时会淘汰不会搜索的平台
    :param region: 地区代码（``CN`` / ``HK`` / ``JP`` ...），空串表示不限
    :param needs_seat_level: 是否要求票档级库存。False 表示「看项目级
        状态就够了」——这样大麦也能进候选，不会因为拿不到票档被一票否决
    """

    category: str = ""
    need_search: bool = True
    region: str = ""
    needs_seat_level: bool = False
    text: str = ""


@dataclasses.dataclass(frozen=True)
class Candidate:
    """一个能接这个需求的平台。"""

    platform: str
    capability: Capability
    requires_credentials: bool
    score: int

    @property
    def needs_login(self) -> bool:
        return self.requires_credentials


@dataclasses.dataclass(frozen=True)
class Rejection:
    """一个被排除的平台，以及**为什么**被排除。

    ``reason`` 是要直接显示给用户看的人话，不是日志。
    """

    platform: str
    reason: str
    capability: Capability


@dataclasses.dataclass(frozen=True)
class RouteResult:
    """路由结论：能用的 + 不能用的（附理由）。"""

    requirement: Requirement
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    @property
    def best(self) -> Candidate | None:
        """最合适的一个。没有候选时为 None——调用方必须处理这种情况。"""
        return self.candidates[0] if self.candidates else None

    @property
    def outcome(self) -> str:
        """结局分类，供 CLI 决定怎么渲染。

        * ``ok``       有候选，可以开工
        * ``need_login`` 只有需要登录的候选，得用户先登录
        * ``unsupported`` 一个候选都没有——明确做不到
        """
        if not self.candidates:
            return "unsupported"
        if all(c.needs_login for c in self.candidates):
            return "need_login"
        return "ok"

    def explain(self) -> str:
        """把结论写成一段人话。**无候选时也必须说得出话来。**"""
        what = self.requirement.text.strip()
        head = f"需求「{what}」" if what else "这个需求"
        cat = self.requirement.category
        # category 为空 = 解析器没判断出类别，这时候**不提类别**。
        # 说「需求属于不限类别」是句废话，不如直接说能用哪些平台。
        prefix = f"{head}属于{category_label(cat)}，" if cat else f"{head}"

        if self.candidates:
            names = "、".join(c.platform for c in self.candidates)
            tail = "（这些平台都需要先登录）" if self.outcome == "need_login" else ""
            return f"{prefix}可以用：{names}{tail}"

        # 一个都没有——把每个平台为什么不行摆出来，而不是干巴巴说不支持
        lines = [f"{prefix}当前没有平台能处理。原因："]
        for rej in self.rejections:
            lines.append(f"  · {rej.platform}（{category_label(rej.capability.category)}）：{rej.reason}")
        if not self.rejections:
            lines.append("  · 没有任何适配器注册")
        return "\n".join(lines)


# -- 路由 -------------------------------------------------------------------


def _region_matches(cap: Capability, region: str) -> bool:
    """地区是否匹配。两边任一为空都算不限。"""
    if not region or not cap.regions:
        return True
    return region.strip().upper() in {r.strip().upper() for r in cap.regions}


def _reject_reason(cap: Capability, req: Requirement) -> str | None:
    """给出排除理由；返回 None 表示**不该排除**。

    判定顺序是有讲究的：先说「平台根本接不了」这种硬事实，
    再说「类别不对」，最后才是「能力差一点」。用户看到的第一个理由
    应该是最根本的那个，而不是最容易补救的那个。
    """
    if not cap.available():
        return cap.limitation or "平台层面接不了（无可用网页接口）"

    if cap.category != req.category and req.category:
        # req.category 为空 =「我没判断出你要哪一类」，这时不过滤类别，
        # 把能用的平台都摆出来让用户挑。全盘拒绝等于把用户推到门外，
        # 而他明明只是想盯个东西。
        return (
            f"它是{category_label(cap.category)}平台，"
            f"处理不了{category_label(req.category)}需求"
        )

    if req.need_search and not cap.can_search:
        return "不支持关键词搜索，需要你自己提供场次 ID 或链接"

    if not _region_matches(cap, req.region):
        return f"不覆盖 {req.region} 地区"

    if req.needs_seat_level and not cap.seat_level:
        return cap.limitation or "拿不到票档级库存，只能看项目级状态"

    return None


def _score(cap: Capability, req: Requirement, requires_credentials: bool) -> int:
    """给候选打分，高的排前面。

    三条偏好，都指向「让用户少做一步」：
    * 不需要登录的优先——登录是最大的上手障碍
    * 能自己搜的优先——用户不用去找 ID
    * 能满足票档要求的优先——信息更全
    """
    score = 0
    if not requires_credentials:
        score += 4
    if req.need_search and cap.can_search:
        score += 2
    if req.needs_seat_level and cap.seat_level:
        score += 2
    if cap.can_resolve_link:
        score += 1
    return score


def capabilities() -> dict[str, Capability]:
    """所有已注册平台的能力声明。"""
    out: dict[str, Capability] = {}
    for name in list_adapters():
        cls = get_adapter_class(name)
        cap = getattr(cls, "capability", None)
        if isinstance(cap, Capability):
            out[name] = cap
    return out


def route(req: Requirement) -> RouteResult:
    """把需求对上平台。**永远返回完整结论**，不会因为没候选就少给信息。"""
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []

    for name in sorted(capabilities()):
        cls = get_adapter_class(name)
        cap = cls.capability

        reason = _reject_reason(cap, req)
        if reason is not None:
            rejections.append(Rejection(platform=name, reason=reason, capability=cap))
            continue

        candidates.append(
            Candidate(
                platform=name,
                capability=cap,
                requires_credentials=bool(getattr(cls, "requires_credentials", False)),
                score=_score(cap, req, getattr(cls, "requires_credentials", False)),
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.platform))
    return RouteResult(
        requirement=req,
        candidates=tuple(candidates),
        rejections=tuple(rejections),
    )


def supported_platforms(category: str | None = None) -> list[str]:
    """当前真正可用的平台（排除 unsupported）。按名字排序。"""
    out = []
    for name, cap in capabilities().items():
        if not cap.available():
            continue
        if category is not None and cap.category != category:
            continue
        out.append(name)
    return sorted(out)


__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "FLIGHT",
    "GENERIC",
    "SHOW",
    "TRAIN",
    "Candidate",
    "Capability",
    "Rejection",
    "Requirement",
    "RouteResult",
    "capabilities",
    "category_label",
    "route",
    "supported_platforms",
]
