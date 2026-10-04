"""适配器抽象基类。

写一个新适配器只需要三步：
1. 继承 ``Adapter``，声明 ``name`` 和 ``min_interval``
2. 实现 ``async def fetch(task, client) -> Snapshot``
3. 在 ``adapters/__init__.py`` 里 import 一下，完成注册

硬性契约（写在基类里，不是建议）
--------------------------------
* ``fetch`` 必须是**只读**的。不许调用下单、候补、提交订单接口。
  不许携带乘车人身份信息。模型层也没有表达这些的字段。
* ``min_interval`` 代表这个平台最少多久查一次。想调小之前先读 README。
* 不要在校验码、风控、设备指纹上做对抗。遇到风控就退避、报错、降低频率，
  而不是想办法绕过去——「绕过平台防御机制」在司法实践里是加重情节。
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

import httpx

from ..models import Snapshot

if TYPE_CHECKING:
    from ..config import TaskConfig


# -- 探活状态常量 -----------------------------------------------------------
# 「没检查」和「检查过了没问题」必须能区分开。体检报告里把前者显示成
# 正常，等于用体检报告掩盖盲区——这跟「没消息就是没问题」是同一种错误。

#: 探活通过
HEALTH_OK = "ok"
#: 凭据没填或已失效。**用户能亲手修**，所以必须单独成档
HEALTH_AUTH = "auth"
#: 平台连不上、或返回结构变了。得看具体情况，不是一句「失败」
HEALTH_BROKEN = "broken"
#: 平台层面接不了（纷玩岛那种）
HEALTH_UNSUPPORTED = "unsupported"
#: 该适配器没实现探活。**不等于正常**
HEALTH_UNKNOWN = "unknown"

HEALTH_LABELS: dict[str, str] = {
    HEALTH_OK: "正常",
    HEALTH_AUTH: "凭据问题",
    HEALTH_BROKEN: "接口异常",
    HEALTH_UNSUPPORTED: "平台不支持",
    HEALTH_UNKNOWN: "未检查",
}


@dataclasses.dataclass(frozen=True)
class Capability:
    """适配器对外声明「我能处理什么样的需求」。

    为什么要有这一层，而不是在路由代码里写 ``if platform == "damai"``：

    * **加平台不改路由**。声明挂在适配器自己身上，路由只是按字段过滤。
      现在项目里每加一个平台都要手改若干处判断，正是缺了这层。
    * **「做不到」时能说出为什么**。``limitation`` 由平台自己写清楚，
      用户问「能不能盯 XX」时照搬给他，而不是回一句笼统的「暂不支持」。

    字段只包含**跟需求路由有关**的事。``min_interval``、``needs_auth``
    这些沿用 :class:`Adapter` 上已有的类属性，不在这里重复声明——
    两处都写就会不一致。
    """

    #: 需求类别：``train`` 交通票务 / ``show`` 演出票务 / ``generic`` 任意接口
    category: str = "generic"

    #: 一句话说明这个平台是什么。和 ``limitation`` 互补：
    #: 一个说「是什么」，一个说「做不到什么」
    summary: str = ""

    #: 能否按关键词搜索。False 意味着「用户只说一句需求」时定位不到目标，
    #: 必须用户自己提供 ID 或链接
    can_search: bool = False

    #: 能否从一条分享链接里提取目标 ID（粘贴即监控的前提）
    can_resolve_link: bool = False

    #: 能否拿到票档级库存。大麦只能到「项目级状态」，这里是 False
    seat_level: bool = False

    #: 覆盖的地区，空元组表示不限。用于「我要盯日本的演出」这种需求
    regions: tuple[str, ...] = ()

    #: 做不到什么。**会原样显示给用户**，所以写具体：
    #: 写「拿不到票档，只能看项目级状态」，不要写「功能受限」
    limitation: str = ""

    #: 平台可用性，路由层据此决定要不要把它放进候选：
    #: ``healthy`` 可用 / ``degraded`` 部分可用 /
    #: ``unsupported`` 平台层面接不了（纷玩岛就是这种——原因是平台的，不是代码的）
    status: str = "healthy"

    def available(self) -> bool:
        """能不能接受新需求。``unsupported`` 一律不考虑。"""
        return self.status != "unsupported"


class AdapterError(RuntimeError):
    """抓取失败基类。engine 会捕获并触发指数退避，不会中断其他任务。

    下面几个子类用来做**故障分级**——监控工具最危险的不是「抓不到」，
    而是「抓错了还告诉你一切正常」。把错误分成「用户能修」「平台在限流」
    「多半是暂时的」「得维护者介入」四档，才能在推送里给出**可执行的下一步**，
    而不是一句笼统的「抓取失败」。
    """


class AuthError(AdapterError):
    """登录态失效：Cookie 过期 / 被踢。

    这是用户**唯一能亲手修**的故障——跑一次 ``radar login <平台>`` 就行。
    所以它应该单独成类，被引擎识别后主动推一条「请重新登录」，
    而不是和别的错误混在一起被忽略（这正是本项目踩过的坑：
    大麦 Cookie 过期后静默失效，用户以为监控还活着）。
    """


class RateLimitedError(AdapterError):
    """被平台限流（HTTP 429 / Retry-After 等）。

    用户**不需要做任何事**——引擎会自动退避重发。单独成类是为了
    不让这种「正常的摩擦」淹没真正的故障告警。
    """


class TransportError(AdapterError):
    """网络层失败（DNS / 超时 / 连接重置 / 代理抽风）。

    多半是暂时的，引擎自动重试即可。和 ``ParseError`` 区分开：
    连响应都没拿到，跟「接口结构变了」是两码事。
    """


class ParseError(AdapterError):
    """接口返回了，但结构变了，解析不出来。

    典型信号是上游改了字段名 / 改了 JSON 层级 / 换成了 HTML 风控页。
    这种**用户修不了、得维护者介入**——所以它该触发一条
    「适配器可能失效，请升级」的告警，而不是被当成普通抓取失败沉默掉。
    """


class Adapter(abc.ABC):
    """所有平台适配器的基类。"""

    #: 适配器名，配置里 ``adapter:`` 字段用这个名字引用
    name: str = "base"

    #: 平台级最小请求间隔（秒）。engine 的限流器会强制不低于此值，
    #: 且是**跨任务共享**的——同平台多个任务也不会低于这个间隔。
    min_interval: float = 60.0

    #: 是否需要登录凭据
    requires_credentials: bool = False

    #: 该适配器会访问的域名前缀（仅用于日志和文档）
    base_url: str = ""

    #: 能力声明。子类必须覆盖——它决定了「用户的一句需求该不该交给这个平台」。
    #: 留空等于什么都不声明，路由层会把这个平台当成「不接受新需求」处理。
    capability: Capability = Capability()

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self.credentials = credentials or {}

    @abc.abstractmethod
    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        """抓取一次余票快照。

        只读操作。不得调用任何会改变服务端状态的接口。
        """

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """轻量探活：**不抓具体数据**，只回答「这个平台现在连得上吗」。

        返回 ``(状态, 说明)``。状态取 :data:`HEALTH_*` 里的常量。

        为什么需要它：``radar onboard`` 只看「凭据填了没」，可 Cookie
        过期之后那一栏照样显示「✓ 已填写」——**体检报告��说谎**。
        真要证明能用，得发一个请求试试。

        与 :meth:`fetch` 的分工：
        * ``fetch`` 需要任务参数（哪趟车、哪场演出），没有配置就跑不了
        * ``doctor`` 只探平台可达性，**没有任务也能跑**

        所以代价必须压到最低：请求体要小、不能触发限流、绝不下单。
        默认实现返回「未实现」，各适配器按需覆盖——没覆盖的平台在体检表里
        显示「不检查」而不是「正常」，两者不能混为一谈。
        """
        return HEALTH_UNKNOWN, "该适配器没有实现探活，不做检查"

    async def enrich_prices(
        self,
        snapshot: Snapshot,
        task: TaskConfig,
        client: httpx.AsyncClient,
        train_codes: Collection[str],
    ) -> Snapshot:
        """为指定条目补票价。**可选**，默认什么都不做。

        为什么是独立钩子而不是塞进 :meth:`fetch`：票价在 12306 这类平台上要
        **按车次**单独请求一次（54 趟车 = 54 次请求）。常态轮询必须守住
        「每轮 1 次请求」这条线，所以只有在**确实要发通知**时才补，
        由 engine 在渲染通知前调用。

        实现约定：

        * 补不到就原样返回，**不要抛异常**——不能因为拿不到票价就丢掉余票提醒。
        * 只查 ``train_codes`` 里点名的条目，不要顺手全查。
        """
        return snapshot

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "min_interval": self.min_interval,
            "requires_credentials": self.requires_credentials,
            "base_url": self.base_url,
            "capability": dataclasses.asdict(self.capability),
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = [
    "HEALTH_AUTH",
    "HEALTH_BROKEN",
    "HEALTH_LABELS",
    "HEALTH_OK",
    "HEALTH_UNSUPPORTED",
    "HEALTH_UNKNOWN",
    "Adapter",
    "AdapterError",
    "AuthError",
    "Capability",
    "ParseError",
    "RateLimitedError",
    "TransportError",
]
