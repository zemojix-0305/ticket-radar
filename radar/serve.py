"""本地 Web 看板：把余票历史画成曲线，并点选车次生成监控配置。

为什么用标准库而不是 Flask / FastAPI
-------------------------------------
这个项目有一个已经成型的气质：**能用标准库解决的，不加依赖**。

而看板的运行条件比一般 web 服务宽松得多：

* 只服务 ``127.0.0.1``，默认不对外
* 单用户、单浏览器，并发量约等于 1
* 请求几乎全是读；只有「实时扫描」会往外发请求

``ThreadingHTTPServer`` 覆盖这些场景绰绰有余。多引一个 web 框架，
换来的是用户多装几个包、多一份 CVE 面、多一份和 Python 版本打架的机会
——而它换来的并发能力，这里根本用不上。

安全边界（读之前先读这段）
--------------------------
看板读的是**你自己的余票历史**，所以默认只绑 127.0.0.1，且**不做鉴权**。
``--host 0.0.0.0`` 是给「想让同一局域网里的手机也能看」准备的；
一旦这么用，同网段任何人都能读到你的查询配置。这是有意的取舍，
但它必须是被显式选择的，不能是默认值。

曲线为什么是阶梯而不是折线
--------------------------
余票在两次抓取之间不会自己变——它是「抓取那一刻的快照」，不是连续量。
画成斜线等于在宣称「这中间是线性变化的」，那是伪造出来的信息。
所以前端画阶梯图，而这里配合做**同值压缩**：一趟车余票一整天都是 0 时，
288 个采样点会被压成 1 个，传输量降两个数量级。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .adapters import create_adapter
from .config import AppConfig, TaskConfig
from .store import Store
from .view import build_snippet, snapshot_to_dict

log = logging.getLogger(__name__)

#: 看板页面与静态资源所在目录。
WEB_DIR = Path(__file__).with_name("web")

#: 曲线默认回溯多少条快照。按 5 分钟一轮算，300 条 ≈ 25 小时。
DEFAULT_SERIES_POINTS = 300


def seat_value(available: bool, count: int | None) -> int | None:
    """把三态余票压成一个可比较的值，供曲线使用。

    * ``0``    —— 无票
    * ``None`` —— 有票但数量未知（12306 返回「有」时就是这种）
    * ``n``    —— n 张

    ``None`` 刻意不等于 ``0``：前者是「充足」，后者是「没有」。
    这个区分搞反，整条曲线会完全反过来（项目早期真的踩过这个坑）。
    前端把 ``None`` 画在天花板上并标成「有票」。
    """
    return count if available else 0


@dataclass(eq=False)
class Board:
    """看板后端：持有配置、库，以及一个按「数据版本号」失效的曲线缓存。"""

    config: AppConfig
    store: Store
    config_path: Path = Path("tasks.yaml")
    points: int = DEFAULT_SERIES_POINTS
    _cache: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- 概览 ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """任务清单 + 库概况。看板首屏就用这一个请求把骨架搭起来。"""
        known = set(self.store.task_ids())
        tasks = [
            {
                "id": t.id,
                "name": t.display_name,
                "adapter": t.adapter,
                "enabled": t.enabled,
                "params": dict(t.params),
                "link": t.link,
                "interval_seconds": t.interval_seconds,
                "seat_types": list(t.watch.seat_types),
                "in_db": t.id in known,
            }
            for t in self.config.tasks
        ]
        # 配置里已经删掉、但历史还在的任务也要能翻到——否则用户会以为数据丢了
        for task_id in self.store.task_ids():
            if all(t["id"] != task_id for t in tasks):
                tasks.append(
                    {
                        "id": task_id,
                        "name": f"{task_id}（配置里已移除）",
                        "adapter": "",
                        "enabled": False,
                        "params": {},
                        "link": "",
                        "interval_seconds": 0,
                        "seat_types": [],
                        "in_db": True,
                    }
                )
        return {
            "config_path": str(self.config_path),
            "db_path": self.config.storage.path,
            "counts": self.store.counts(),
            "tasks": tasks,
        }

    # -- 曲线 ---------------------------------------------------------------

    def series(self, task_id: str) -> dict[str, Any]:
        """返回任务的余票时序，带缓存。

        缓存失效用「快照表的当前最大 id」而不是 TTL：监控停掉的时候，
        TTL 会让看板每隔几秒白白重算一遍几十 MB 的 JSON，而数据其实没变。
        """
        version = self.store.latest_snapshot_id()
        with self._lock:
            hit = self._cache.get(task_id)
            if hit is not None and hit[0] == version:
                return hit[1]

        data = self._build_series(task_id)
        with self._lock:
            self._cache[task_id] = (version, data)
        return data

    def _build_series(self, task_id: str) -> dict[str, Any]:
        snaps = self.store.recent_snapshots(task_id, self.points)
        first, last = self.store.snapshot_span(task_id)
        try:
            watch = self.config.task(task_id).watch
        except KeyError:
            # 任务已从配置里删掉，但历史还在。历史仍然可看，
            # 只是没有「监控范围」这个概念了——所以这里不报错，置空即可。
            watch = None

        buckets: dict[str, dict[str, Any]] = {}
        for snap in snaps:
            stamp = snap.captured_at.isoformat()
            for code, train in snap.trains.items():
                bucket = buckets.get(code)
                if bucket is None:
                    bucket = buckets[code] = {
                        "train_code": code,
                        "from": train.from_station,
                        "to": train.to_station,
                        "depart": train.depart_time,
                        "arrive": train.arrive_time,
                        "duration": train.duration,
                        "watched": False,
                        "seats": {},
                    }
                # 车次元信息取最新一轮：时刻会被铁路临时调整，
                # 用第一次抓到的旧时刻去展示会误导。
                if train.depart_time:
                    bucket["depart"] = train.depart_time
                    bucket["arrive"] = train.arrive_time
                    bucket["duration"] = train.duration

                for seat_type, seat in train.seats.items():
                    points = bucket["seats"].setdefault(seat_type, [])
                    value = seat_value(seat.available, seat.count)
                    # 同值压缩：值没变就不记新点。阶梯图只需要变化点，
                    # 所以这里丢掉的点前端一定能正确还原。
                    if points and points[-1]["v"] == value:
                        continue
                    points.append(
                        {
                            "t": stamp,
                            "v": value,
                            "raw": seat.raw,
                            "price": seat.price,
                        }
                    )

        for bucket in buckets.values():
            # watched 只判一次（用最新的车次元信息），和 view.snapshot_to_dict
            # 的口径保持一致：车次 / 时间窗 / **席别** 三者都命中才算覆盖。
            bucket["watched"] = bool(
                watch
                and watch.matches_train(bucket["train_code"])
                and watch.matches_depart_time(bucket["depart"])
                and any(watch.matches_seat(s) for s in bucket["seats"])
            )

        trains = sorted(buckets.values(), key=lambda b: (b["depart"] or "99:99", b["train_code"]))
        return {
            "task_id": task_id,
            "points": len(snaps),
            "from": first,
            "to": last,
            "trains": trains,
        }

    # -- 实时扫描 -----------------------------------------------------------

    def scan(self, task_id: str) -> dict[str, Any]:
        """现在就抓一次，返回与 ``radar check --json`` 同构的结果。

        这是看板里唯一会往外发请求的动作，所以它必须由用户点按钮触发，
        不能因为「打开页面」就自动跑一遍。
        """
        task = self.config.task(task_id)
        return asyncio.run(self._scan_async(task))

    async def _scan_async(self, task: TaskConfig) -> dict[str, Any]:
        adapter = create_adapter(task.adapter, self.config.credentials_for(task))
        # 超时与请求头跟 `radar check` 保持一致——两边抓到的东西不一样才是 bug
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            snapshot = await adapter.fetch(task, client)
        return snapshot_to_dict(task, snapshot)

    # -- 生成配置片段 --------------------------------------------------------

    def snippet(self, task_id: str, train_codes: list[str]) -> str:
        return build_snippet(self.config.task(task_id), train_codes)


def _first_task(board: Board) -> str:
    """没指定 task_id 时用哪个任务：优先库里最近活跃的，其次配置里第一个。"""
    known = board.store.task_ids()
    if known:
        return known[0]
    if board.config.tasks:
        return board.config.tasks[0].id
    raise KeyError("库里和配置里都没有任务")


def make_handler(board: Board) -> Callable[..., BaseHTTPRequestHandler]:
    """给指定的 Board 造一个绑定它的 handler 类。

    用闭包而不是给 handler 挂类属性：类属性是**全局**的，
    测试里建第二个 Board 时会把第一个的服务指向搞乱。
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "ticket-radar-board"
        protocol_version = "HTTP/1.1"

        # -- 响应助手 -------------------------------------------------------

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 看板数据每次都要最新的，缓存只会让「刚出的票看不到」
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _send_page(self) -> None:
            path = WEB_DIR / "index.html"
            if not path.exists():
                self._send_bytes(
                    f"看板页面缺失：{path}".encode(),
                    "text/plain; charset=utf-8",
                    500,
                )
                return
            self._send_bytes(path.read_bytes(), "text/html; charset=utf-8")

        def _task_id(self, query: dict[str, str]) -> str:
            return query.get("task_id") or _first_task(board)

        # -- 路由 -----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 规定的大小写
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path in ("/", "/index.html"):
                    self._send_page()
                elif parsed.path == "/api/status":
                    self._send_json(board.status())
                elif parsed.path == "/api/series":
                    self._send_json(board.series(self._task_id(query)))
                elif parsed.path == "/api/scan":
                    self._send_json(board.scan(self._task_id(query)))
                else:
                    self._send_json({"error": f"未知路径：{parsed.path}"}, status=404)
            except KeyError as exc:
                self._send_json({"error": f"未找到任务：{exc}"}, status=404)
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/snippet":
                self._send_json({"error": f"未知路径：{parsed.path}"}, status=404)
                return
            try:
                size = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(size) or b"{}")
                snippet = board.snippet(
                    str(payload.get("task_id", "")), list(payload.get("train_codes") or [])
                )
                self._send_json({"snippet": snippet})
            except KeyError as exc:
                self._send_json({"error": f"未找到任务：{exc}"}, status=404)
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        def log_message(self, fmt: str, *args: Any) -> None:
            """默认实现会往 stderr 打每一行请求，会把监控日志刷没。"""
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def build_server(board: Board, host: str, port: int) -> ThreadingHTTPServer:
    """建好但**不启动**的 HTTP 服务，交给调用方决定何时跑（测试要它）。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(board))
    # 关掉窗口时不要被还在跑的扫描线程拖住
    httpd.daemon_threads = True
    return httpd


__all__ = ["Board", "DEFAULT_SERIES_POINTS", "build_server", "make_handler", "seat_value"]
