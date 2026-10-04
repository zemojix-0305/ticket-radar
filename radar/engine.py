"""监控引擎：调度、限流、状态机、推送编排。

三个关键组件
------------
``PlatformRateLimiter``
    平台级串行限流。注意是**平台级**而不是任务级——就算你配了 10 个
    12306 任务，对 12306 的请求依然被串成一个队列，最少隔 min_interval
    秒才发一次。这是防止「多加几个任务就变成高频刷票」的关键阀门。

``ChangeDetector``
    无状态的纯函数 diff。输入上一轮和这一轮快照，输出变更事件列表。
    第一轮只建立基线、不产生任何事件——否则启动瞬间会推送一屏「有票」。

``Monitor``
    asyncio 编排。每个任务一个协程，出错指数退避，单任务挂掉不影响其他任务。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .adapters import Adapter, AdapterError, create_adapter, list_adapters
from .adapters.base import AuthError, ParseError, RateLimitedError, TransportError
from .config import AppConfig, TaskConfig
from .models import Change, EventKind, Snapshot, normalize_count, params_fingerprint
from .notifier import Message, Notifier, NotifierHub, build_notifiers
from .selfcheck import SelfCheckReport, Verdict, check_snapshot
from .store import Store

log = logging.getLogger("radar.engine")

#: 出错后的退避上限（秒）。超过这个值说明平台或配置有持续问题。
MAX_BACKOFF_SECONDS = 1800
BASE_BACKOFF_SECONDS = 30

#: 单次通知最多补查几个车次的票价。
#: 为什么要有上限：一场大规模放票会让所有关注车次**同时**从无票变有票，
#: 不设限的话一次通知就可能向平台打出几十个请求——那就从「低频只读」
#: 变成了本项目明确拒绝的高频行为。宁可少显示几个价格。
MAX_PRICE_LOOKUPS = 10

_KIND_LABEL = {
    EventKind.APPEARED: "余票出现",
    EventKind.INCREASED: "余票增加",
    EventKind.NEW_TRAIN: "加开车次",
    EventKind.SOLD_OUT: "已售罄",
    EventKind.DECREASED: "余票减少",
}


class PlatformRateLimiter:
    """保证同一平台在 min_interval 秒内最多发出一批请求。

    用「锁 + 上次请求时间戳」实现：拿到锁之后如果距离上次请求不足最小间隔，
    就 sleep 到够为止。简单、无依赖、行为容易推理。

    多任务共用同一个 limiter 实例，所以限流是跨任务生效的。
    """

    def __init__(self) -> None:
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, platform: str) -> asyncio.Lock:
        if platform not in self._locks:
            self._locks[platform] = asyncio.Lock()
        return self._locks[platform]

    async def acquire(self, platform: str, min_interval: float) -> float:
        """阻塞直到可以发请求。返回实际等待的秒数（便于日志观测）。"""
        async with self._lock(platform):
            now = time.monotonic()
            last = self._last_request.get(platform)
            waited = 0.0
            if last is not None:
                elapsed = now - last
                if elapsed < min_interval:
                    waited = min_interval - elapsed
                    await asyncio.sleep(waited)
            self._last_request[platform] = time.monotonic()
            return waited


class ChangeDetector:
    """对比两轮快照，产出变更事件。

    纯函数、无副作用，所以可以完整单测。
    """

    @staticmethod
    def diff(
        previous: Snapshot | None,
        current: Snapshot,
        rule: Any,
    ) -> list[Change]:
        """返回变更列表。

        ``previous`` 为 None 表示首轮，只建立基线，返回空列表——
        否则程序一启动就推一屏「有票」，那不是变化，是初始状态。

        ``rule`` 关注规则。有 ``matches_train`` / ``matches_seat`` 方法时会被
        用来先过滤，避免把不关心的席别波动也记成事件。
        """
        if previous is None:
            return []

        now = datetime.now(timezone.utc)
        changes: list[Change] = []
        min_count = getattr(rule, "min_count", 1)
        matches_train = getattr(rule, "matches_train", None)
        matches_seat = getattr(rule, "matches_seat", None)
        matches_depart = getattr(rule, "matches_depart_time", None)

        for code, train in current.trains.items():
            if matches_train is not None and not matches_train(code):
                continue
            # 时间窗在 diff 里就过掉，而不是等渲染时再筛：
            # 被排除的车次压根不该产生变更记录，否则 data/radar.db 里
            # 会堆满「你根本不关心的车次」的历史，日后没法用来看趋势。
            if matches_depart is not None and not matches_depart(train.depart_time):
                continue

            prev_train = previous.trains.get(code)

            # 上一轮不存在的车次 = 新出现。只要当前有票就报 NEW_TRAIN。
            if prev_train is None:
                for seat_type, seat in train.seats.items():
                    if matches_seat is not None and not matches_seat(seat_type):
                        continue
                    if seat.effective >= min_count:
                        changes.append(
                            Change(
                                task_id=current.task_id,
                                platform=current.platform,
                                kind=EventKind.NEW_TRAIN,
                                train_code=code,
                                seat_type=seat_type,
                                before=0,
                                after=normalize_count(seat.effective),
                                detected_at=now,
                            )
                        )
                continue

            # 席别取并集：上一轮有的、这一轮有的，都要看。
            for seat_type in set(prev_train.seats) | set(train.seats):
                if matches_seat is not None and not matches_seat(seat_type):
                    continue

                before = prev_train.effective_of(seat_type)
                after = train.effective_of(seat_type)

                if before == after:
                    continue

                if before == 0 and after >= min_count:
                    kind = EventKind.APPEARED
                elif before > 0 and after == 0:
                    kind = EventKind.SOLD_OUT
                elif after > before:
                    kind = EventKind.INCREASED
                else:
                    kind = EventKind.DECREASED

                changes.append(
                    Change(
                        task_id=current.task_id,
                        platform=current.platform,
                        kind=kind,
                        train_code=code,
                        seat_type=seat_type,
                        before=normalize_count(before),
                        after=normalize_count(after),
                        detected_at=now,
                    )
                )

        return changes


def _price_hint(snapshot: Snapshot, train_code: str, seat_type: str) -> str:
    """取该车次该席别的票价后缀，例如 ``　¥661``。没查到返回空串。"""
    train = snapshot.trains.get(train_code)
    seat = train.seats.get(seat_type) if train else None
    if seat is None or not seat.price_text:
        return ""
    return f"　{seat.price_text}"


def _classify_error(exc: AdapterError) -> tuple[str, str, str]:
    """把抓取异常归到故障分级的某一档，返回 (日志标签, 内部 kind, 给用户的可执行提示)。

    四档的设计意图：监控工具最危险的不是「抓不到」，而是「抓错了还报平安」。
    把错误分成「用户能修 / 平台限流 / 多半暂时 / 得维护者介入」，推送里才能
    给出**下一步**，而不是一句空洞的「抓取失败」。
    """
    if isinstance(exc, AuthError):
        return "登录态失效", "auth", "请重新登录：radar login <平台>"
    if isinstance(exc, RateLimitedError):
        return "被平台限流", "rate_limited", "引擎已自动退避，无需操作"
    if isinstance(exc, TransportError):
        return "网络异常", "transport", "引擎正在自动重试"
    if isinstance(exc, ParseError):
        return "解析失败", "parse", "多半是上游改了结构，需升级适配器"
    return "抓取失败", "adapter", "多半是平台结构变了，需维护者介入"


def format_auth_message(task: TaskConfig, detail: str) -> Message:
    """登录态失效时推的告警：用户唯一能亲手修的故障，必须主动叫人。"""
    return Message(
        title="【余票监控】需要你操作：登录已失效",
        body=(
            f"任务 **{task.display_name}** 的登录态失效了，监控它在**这一刻是瞎的**——\n"
            f"它不会自己恢复，也不会再报任何余票变化，直到你重新登录。\n\n"
            f"{detail}\n\n"
            "重新登录后关掉浏览器窗口即可，Cookie 会自动回写，无需碰 F12。\n"
            "其它任务不受影响。"
        ),
        url=task.link,
    )


def format_selfcheck_message(task: TaskConfig, report: SelfCheckReport) -> Message:
    """解析疑似失效时推的告警：监控抓到了东西，但抓得不可信。"""
    return Message(
        title="【余票监控】需要你关注：适配器可能失效",
        body=(
            f"任务 **{task.display_name}** 最近一轮抓到的数据自检未通过：\n\n"
            f"{report.describe()}\n\n"
            "这通常意味着上游改了接口结构，或者触发了风控被弹回登录页。\n"
            "在修复之前，这个任务的余票提醒可能不准——请留意升级适配器。\n"
            "其它任务不受影响。"
        ),
        url=task.link,
    )


def format_health_message(rows: list[HealthRow]) -> Message:
    """健康心跳：一条「我还活着，而且各任务状态如下」的汇总。

    设计目的：监控只在「变化」时开口，于是「长时间安静」到底代表
    「没变化」还是「程序挂了」就成了真问题。这条心跳把两者分开——
    **没收到心跳 = 真的挂了**；收到了 = 至少进程还活着、各任务也都正常。
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"健康检查　{stamp}\n"]
    for row in rows:
        status = row.status_line()
        lines.append(f"· {row.name}　{status}")
    lines.append("\n收到这条说明监控进程还活着。只有状态变化才会再主动提醒你。")
    healthy = all(r.verdict is not Verdict.BROKEN for r in rows)
    title = "【余票监控】健康检查" + ("" if healthy else "（有任务异常）")
    return Message(title=title, body="\n".join(lines))


@dataclass
class HealthRow:
    """单个任务的健康快照，供心跳 / ``radar health`` 展示。"""

    task_id: str
    name: str
    last_success: datetime | None
    verdict: Verdict | None
    items: int
    consecutive_errors: int
    last_error_kind: str | None
    last_error: str | None

    def status_line(self) -> str:
        if self.consecutive_errors:
            kind = {
                "auth": "登录失效",
                "rate_limited": "被限流",
                "transport": "网络异常",
                "parse": "解析失败",
                "adapter": "抓取失败",
                "unexpected": "未预期错误",
            }.get(self.last_error_kind or "", "异常")
            tail = self.last_error or ""
            if len(tail) > 40:
                tail = tail[:40] + "…"
            return f"[yellow]异常 {self.consecutive_errors} 次（{kind}）[/yellow]　{tail}"
        if self.last_success is None:
            # 从没成功抓过：既没有成功时间也没有报错，说「正常」是骗人。
            return "[yellow]尚未成功抓取[/yellow]"
        if self.verdict is Verdict.BROKEN:
            return "[red]解析疑似失效[/red]"
        if self.verdict is Verdict.DEGRADED:
            return "[yellow]可疑（与历史对不上）[/yellow]"
        when = self.last_success.astimezone().strftime("%H:%M") if self.last_success else "—"
        return f"[green]正常[/green]　最近 {when}　{self.items} 个条目"


#: 变化提醒里最多列几个车次的明细。
#:
#: 为什么必须有上限：实测一次「39 个车次同时放票」会渲染成 78 行，
#: 手机上一屏放不下，用户只会看到一屏车次号，**看不到结论**。
#: 而他真正要的信息是「有 39 个车次有变化，其中这些最早、最多」，
#: 明细是附属品。要全部随时可以 ``radar status``。
MAX_MESSAGE_TRAINS = 5

#: 变化类型的展示优先级。放票（新出现 / 加开列车）最值得看；
#: 「余票变少」「售罄」排到最后——它们通常是坏消息，抢在最前面会
#: 让用户以为错过了什么，其实只是提醒性质不同。
_KIND_RANK: dict[EventKind, int] = {
    EventKind.APPEARED: 0,
    EventKind.NEW_TRAIN: 0,
    EventKind.INCREASED: 1,
    EventKind.DECREASED: 2,
    EventKind.SOLD_OUT: 3,
}


def format_message(task: TaskConfig, changes: Sequence[Change], snapshot: Snapshot) -> Message:
    """把变更列表渲染成通知正文（Markdown，兼容微信/钉钉/Telegram/邮件）。"""
    by_train: dict[str, list[Change]] = {}
    for c in changes:
        by_train.setdefault(c.train_code, []).append(c)

    head_kind = _KIND_LABEL.get(changes[0].kind, "余票变化") if changes else "余票变化"

    params = task.params
    route = ""
    if params.get("from") and params.get("to"):
        route = f"{params['from']} → {params['to']}"
    date_hint = snapshot.captured_at.astimezone().strftime("%m-%d %H:%M")

    headline = f"**{route}**" if route else f"**{task.display_name}**"
    context_line = snapshot.context_line()
    if context_line:
        # 有明确的查询上下文（如乘车日期）时优先展示它。只写检测时间的话，
        # 同时盯多个日期的用户收到的推送长得一模一样，根本分不清是哪天的票。
        lines = [headline, context_line, ""]
    else:
        lines = [f"{headline}　{date_hint}".strip(), ""]

    # 先给结论：动了几个车次。这句话比下面任何一行明细都重要。
    if len(by_train) > 1:
        lines.insert(2, f"**{len(by_train)} 个车次有变化**　下面是最值得看的几个")

    # 排序 + 截断：好变化在前、余票多的在前。
    ranked = sorted(
        by_train.items(),
        key=lambda kv: (
            min(_KIND_RANK.get(c.kind, 9) for c in kv[1]),
            -max((c.after or 0) for c in kv[1]),
        ),
    )
    for code, items in ranked[:MAX_MESSAGE_TRAINS]:
        train = snapshot.trains.get(code)
        head = code
        if train and (train.depart_time or train.arrive_time):
            head += f"　{train.depart_time} → {train.arrive_time}"
        if train and train.duration:
            head += f"　{train.duration}"
        lines.append(head)
        for c in items:
            amount = Change.format_count(c.after)
            lines.append(
                f"　· {c.seat_type}　{amount}{_price_hint(snapshot, code, c.seat_type)}"
                f"　（{_KIND_LABEL.get(c.kind, c.kind.value)}）"
            )
        lines.append("")

    hidden = len(ranked) - MAX_MESSAGE_TRAINS
    if hidden > 0:
        lines.append(f"…… 还有 {hidden} 个车次有变化，跑 `radar status` 看全部")
        lines.append("")

    lines.append(f"任务：{task.display_name}")
    lines.append(f"检测时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    if task.link:
        lines.append("")
        lines.append(f"[前往官方渠道自行下单]({task.link})")

    title = f"【余票提醒】{route} {head_kind}" if route else f"【余票提醒】{head_kind}"
    return Message(title=title, body="\n".join(lines), url=task.link)


class Monitor:
    """监控编排器。"""

    def __init__(
        self,
        config: AppConfig,
        store: Store,
        *,
        client: httpx.AsyncClient | None = None,
        notifiers: Sequence[Notifier] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.limiter = PlatformRateLimiter()
        self._client = client
        self._owns_client = client is None
        self._notifiers = list(notifiers) if notifiers is not None else None
        self._hub: NotifierHub | None = None
        self._stats: dict[str, dict[str, Any]] = {}
        #: 预热那一轮的结果，留给紧接着的第一轮复用（见 :meth:`reuse_next`）。
        self._prefetched: dict[str, Snapshot] = {}

    # -- 资源管理 -----------------------------------------------------------

    async def __aenter__(self) -> Monitor:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
        if self._notifiers is None:
            self._notifiers = build_notifiers(self.config, self._client)
        self._hub = NotifierHub(self._notifiers)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._hub:
            await self._hub.aclose()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Monitor 尚未初始化，请用 `async with Monitor(...)`")
        return self._client

    @property
    def hub(self) -> NotifierHub:
        if self._hub is None:
            raise RuntimeError("Monitor 尚未初始化，请用 `async with Monitor(...)`")
        return self._hub

    @property
    def stats(self) -> dict[str, dict[str, Any]]:
        return self._stats

    def _stat(self, task_id: str) -> dict[str, Any]:
        return self._stats.setdefault(
            task_id,
            {
                "polls": 0,
                "errors": 0,
                "notified": 0,
                "last_error": None,
                "last_error_kind": None,
                "last_poll": None,
                "last_success": None,  # 最近一次「拿到可信快照」的时间
                "last_verdict": None,  # 最近一次自检结论（Verdict）
                "items": 0,  # 最近一次快照的条目数
                "consecutive_errors": 0,  # 连续失败次数（成功一次清零）
                "auth_alerted": False,  # 已就「登录失效」发过提醒（恢复后清零）
                "broken_alerted": False,  # 已就「解析疑似失效」发过提醒
            },
        )

    def record_failure(self, task_id: str, exc: BaseException) -> tuple[str, str, str]:
        """把一次失败记进健康统计，返回 (日志标签, kind, 给用户提示)。

        为什么要单独拎出来：故障统计原本只写在 :meth:`_poll_with_backoff` 里，
        但**预热**（`_warm_up`）为了「快」直接调 ``poll_once``，绕开了它——
        于是预热失败根本没进统计，``radar health`` 会把一个抓失败的任务
        显示成「正常　最近 —　0 个条目」。

        这正是本项目的招牌功能要消灭的那类故障：抓错了还报平安。
        所以把统计逻辑抽成公共方法，任何直接调 ``poll_once`` 的路径都得调它。
        """
        stat = self._stat(task_id)
        stat["errors"] += 1
        stat["consecutive_errors"] += 1
        if isinstance(exc, AdapterError):
            stat["last_error"] = str(exc)
            label, kind, hint = _classify_error(exc)
        else:
            stat["last_error"] = f"{type(exc).__name__}: {exc}"
            label, kind, hint = "未预期错误", "unexpected", f"{type(exc).__name__}: {exc}"
        stat["last_error_kind"] = kind
        return label, kind, hint

    def reuse_next(self, task_id: str, snapshot: Snapshot) -> None:
        """把这一轮的结果留给紧接着的下一次 :meth:`poll_once` 复用。

        只该由「预热」调用（启动播报要先知道现在有没有票），而且只留给
        紧跟着的那一轮：缓存是**一次性**的，``poll_once`` 取走即删。
        留久了就变成「跳过抓取直接拿旧数据」，那比多抓一次糟得多。
        """
        self._prefetched[task_id] = snapshot

    # -- 健康看板 -----------------------------------------------------------

    def health_summary(self) -> list[HealthRow]:
        """汇总各任务的健康状态，供心跳 / ``radar health`` 展示。

        数据来自每轮轮询时写进 ``_stats`` 的字段：最近一次成功时间、自检结论、
        连续失败次数、最近错误类型。这里只做聚合，不碰网络。
        """
        rows: list[HealthRow] = []
        for task_id, st in self._stats.items():
            try:
                name = self.config.task(task_id).display_name
            except KeyError:
                name = task_id
            rows.append(
                HealthRow(
                    task_id=task_id,
                    name=name,
                    last_success=st.get("last_success"),
                    verdict=st.get("last_verdict"),
                    items=st.get("items", 0),
                    consecutive_errors=st.get("consecutive_errors", 0),
                    last_error_kind=st.get("last_error_kind"),
                    last_error=st.get("last_error"),
                )
            )
        return rows

    async def _send_heartbeat(self) -> None:
        """推一条健康心跳（若没有任何可用渠道则静默跳过）。"""
        if self._hub is None:
            return
        rows = self.health_summary()
        if not rows:
            return
        try:
            await self.hub.send(format_health_message(rows))
        except Exception as exc:  # 心跳发不出不能拖垮监控本身
            log.warning("健康心跳推送失败：%s", exc)

    async def _heartbeat_loop(self) -> None:
        """常驻心跳：每隔 ``heartbeat_interval_seconds`` 推一条健康汇总。

        单独一条协程跑在 ``run`` 里，和任务轮询并行。被取消（Ctrl+C）时
        随 ``run`` 的 gather 一起退出，不会留下孤儿任务。
        """
        interval = self.config.heartbeat_interval_seconds
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            await self._send_heartbeat()

    # -- 单次轮询 -----------------------------------------------------------

    async def poll_once(self, task: TaskConfig, *, notify: bool = True) -> list[Change]:
        """跑一轮：限流 -> 抓取 -> diff -> 落库 -> 推送。

        返回本轮触发推送条件的变更（``notify=False`` 时返回全部变更，便于调试）。
        """
        adapter: Adapter = create_adapter(task.adapter, self.config.credentials_for(task))
        stat = self._stat(task.id)

        # 预热那一轮（启动播报要先知道「现在有没有票」）已经抓过并落库了，
        # 这一轮直接复用它的结果。重抓一次不只是浪费一次请求：
        # 平台限流是按平台计的，大麦的间隔是 300 秒，重抓会让**首轮整整晚 5 分钟**
        # 才出结果——用户看到的就成了「启动了，然后什么都没发生」。
        prefetched = self._prefetched.pop(task.id, None)
        if prefetched is None:
            waited = await self.limiter.acquire(task.adapter, adapter.min_interval)
            if waited > 1:
                log.debug("[%s] 平台限流等待 %.1fs", task.id, waited)
            snapshot = await adapter.fetch(task, self.client)
        else:
            log.debug("[%s] 复用预热结果，不重复请求", task.id)
            snapshot = prefetched

        stat["polls"] += 1
        stat["last_poll"] = datetime.now(timezone.utc)

        # Snapshot 是 frozen 的，打指纹要换一个新对象
        fingerprint = params_fingerprint(task.params)
        snapshot = dataclasses.replace(snapshot, params_fingerprint=fingerprint)

        previous = self.store.latest_snapshot(task.id)

        # 参数变了（最常见的是 `date: "+7"` 跨过午夜滚到新的一天）说明这一轮
        # 和上一轮抓的根本不是同一批货。此时旧快照只能丢掉重建基线，
        # 否则「换了一天」会被 diff 误报成「放票了」。
        if previous is not None and previous.params_fingerprint != fingerprint:
            log.info(
                "[%s] 任务参数已变化，快照基准不可比，本轮重建基线（不推送）",
                task.id,
            )
            previous = None

        changes = ChangeDetector.diff(previous, snapshot, task.watch)

        # 先落快照：即使后面推送失败，下一轮的对比基准也是对的。
        # 复用的那一轮不用再写：预热时已经落过库了，重复写只会在余票曲线上
        # 多出一个完全同值的点。
        if prefetched is None:
            self.store.save_snapshot(snapshot)

        # --- 语义自检 -----------------------------------------------------------
        # 抓到了 ≠ 抓对了。这一轮「结构上讲不讲得通」单独判一次，
        # 和 diff 正交：diff 关心「余票变多了没」，自检关心「这堆数据能信吗」。
        # 一个平时能解析出 20 个票档的适配器突然返回 0 条，如果只当成
        # 「这次没票」就会一直沉默——这正是要挡住的那类故障。
        report = check_snapshot(snapshot, previous, task)
        stat["last_success"] = datetime.now(timezone.utc)
        stat["last_verdict"] = report.verdict
        stat["items"] = len(snapshot.trains)
        stat["consecutive_errors"] = 0
        # 抓到了可信数据，说明登录态没坏、解析没坏——清掉两类「已提醒」标记，
        # 下次再坏时还能再提醒一次（否则只会响一次就永久沉默）。
        stat["auth_alerted"] = False
        if report.verdict is not Verdict.BROKEN:
            stat["broken_alerted"] = False

        if report.verdict is Verdict.BROKEN and notify and not stat["broken_alerted"]:
            # 几乎可以确定是解析坏了：主动推一条「适配器可能失效」，
            # 而不是把它和「没变化」一起沉默掉。只在「刚变成 BROKEN」时推，
            # 免得每一轮都来一条。
            try:
                await self.hub.send(format_selfcheck_message(task, report))
                stat["broken_alerted"] = True
            except Exception as exc:  # 自检告警发不出去不能拖垮监控
                log.warning("[%s] 自检告警推送失败：%s", task.id, exc)

        filtered = [c for c in changes if task.watch.should_notify(c.kind)]

        # 只有**真的推送成功**的变更才算「已推送」。这里有两个都容易踩的坑：
        #   1. 拿「有没有变更」当标记 —— 那 kind 为 increased 的也会被标上，
        #      可 notify_on 里可能只允许 appeared，它压根没进推送正文；
        #   2. 拿「过滤后的集合非空」当标记 —— 一次推多条时，
        #      只要有一条进了推送，其余没进的也全被标上。
        # `radar history` 的「已推送 ✓」列就是这个字段，标错等于说谎。
        notified: set[Change] = set()

        if filtered and notify:
            # 票价按车次单独查，所以只在真要发通知时补这一次。
            # 补不到也不影响余票提醒——enrich_prices 的契约就是不抛异常。
            codes = {c.train_code for c in filtered}
            if len(codes) > MAX_PRICE_LOOKUPS:
                log.info(
                    "[%s] %d 趟车同时命中，超过补价上限，只补前 %d 趟（余票提醒不受影响）",
                    task.id,
                    len(codes),
                    MAX_PRICE_LOOKUPS,
                )
                codes = set(sorted(codes)[:MAX_PRICE_LOOKUPS])
            try:
                snapshot = await adapter.enrich_prices(snapshot, task, self.client, codes)
            except Exception as exc:
                log.debug("[%s] 补查票价失败（不影响余票提醒）：%s", task.id, exc)

            message = format_message(task, filtered, snapshot)
            try:
                await self.hub.send(message)
                stat["notified"] += 1
                notified = set(filtered)
            except Exception as exc:  # 推送失败不能影响监控本身
                log.warning("[%s] 推送失败：%s", task.id, exc)

        if changes:
            self.store.record_changes(changes, notified=notified)

        return filtered if notify else changes

    async def _poll_with_backoff(
        self,
        task: TaskConfig,
        *,
        notify: bool = True,
        retries: int | None = None,
    ) -> None:
        """带指数退避的单轮轮询。抓取失败不放弃任务，只是拉长间隔。

        :param retries: 最多重试几次。``None`` = 一直重试（常驻监控要的就是这个）；
            ``0`` = 失败就算了，不重试。

            为什么要有这个开关：``radar run --once`` 的承诺是「每个任务只跑一轮
            就退出」。要是某个任务**永久**失败（最典型的就是 Cookie 过期），
            无限重试会让这条命令**永远不退出**——实测撞上过，看着日志里
            「30s 后重试」以为是慢，等了三分钟才明白它根本不会停。
            命令自己的承诺，得由命令自己兑现。
        """
        stat = self._stat(task.id)
        backoff = BASE_BACKOFF_SECONDS
        attempt = 0
        while True:
            try:
                await self.poll_once(task, notify=notify)
                return
            except asyncio.CancelledError:
                raise
            except AdapterError as err:
                # 注意：Python 3 里 `except ... as exc` 的 exc 在块结束后会被自动删除，
                # 块外再引用会 UnboundLocalError。所以这里显式绑到 exc，块外才够得到。
                exc = err
                label, kind, hint = self.record_failure(task.id, exc)
            except Exception as err:  # 未知错误也要退避，不能打爆日志
                exc = err
                label, kind, hint = self.record_failure(task.id, exc)

            # 故障分级里的「用户能修」这一档，要主动叫人：
            # 登录态失效是大麦 Cookie 过期那种——不叫，用户以为监控还活着，
            # 等到开场才发现早瞎了。只在「刚变失效」时推，恢复后清零再推。
            if isinstance(exc, AuthError) and notify and not stat["auth_alerted"]:
                try:
                    await self.hub.send(format_auth_message(task, str(exc)))
                    stat["auth_alerted"] = True
                except Exception as send_err:  # 告警发不出也不能拖垮监控
                    log.warning("[%s] 登录失效告警推送失败：%s", task.id, send_err)

            if retries is not None and attempt >= retries:
                # 措辞里不写「重试」，因为真的不会重试了——说重试是骗人
                log.warning("[%s] %s，本轮不再重试：%s", task.id, label, hint)
                return
            log.warning("[%s] %s（%.0fs 后重试）：%s", task.id, label, backoff, hint)
            await asyncio.sleep(backoff)
            attempt += 1
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    # -- 主循环 -------------------------------------------------------------

    async def run(self, *, once: bool = False, notify: bool = True) -> None:
        """运行所有启用的任务。

        ``once=True`` 时每个任务只跑一轮就退出（等价于 check）。
        这一轮**不重试**——承诺了「跑完就退出」，就不能因为某个任务失败而
        卡住不返回（详见 :meth:`_poll_with_backoff` 的 ``retries`` 说明）。
        """
        tasks = self.config.enabled_tasks
        if not tasks:
            log.warning("没有启用的任务，请检查配置里的 enabled 字段")
            return

        log.info(
            "启动监控：%d 个任务，适配器 %s",
            len(tasks),
            ", ".join(sorted({t.adapter for t in tasks})),
        )

        if once:
            await asyncio.gather(
                *(self._poll_with_backoff(t, notify=notify, retries=0) for t in tasks),
                return_exceptions=True,
            )
            return

        runners = [asyncio.create_task(self._loop(t, notify=notify), name=t.id) for t in tasks]
        # 健康心跳：单独一条协程，和任务轮询并行。没配通知渠道 / 间隔为 0 就不开。
        if notify and self.config.heartbeat_interval_seconds > 0 and self._hub is not None:
            runners.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        try:
            await asyncio.gather(*runners)
        except asyncio.CancelledError:
            for r in runners:
                r.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*runners, return_exceptions=True)
            raise

    async def _loop(self, task: TaskConfig, *, notify: bool) -> None:
        """单任务的常驻循环。间隔里加入抖动，避免多任务同刻并发。"""
        while True:
            await self._poll_with_backoff(task, notify=notify)
            delay = task.interval_seconds + random.uniform(0, task.jitter_seconds)
            log.debug("[%s] 下一轮 %.0fs 后", task.id, delay)
            await asyncio.sleep(delay)


__all__ = [
    "BASE_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "ChangeDetector",
    "Monitor",
    "PlatformRateLimiter",
    "format_message",
    "list_adapters",
]
