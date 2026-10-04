"""配置驱动的通用 JSON 余票适配器。

为什么需要它
------------
演出票平台（大麦 / 猫眼 / 摩天轮 / 纷玩岛）、机票、校园选课……它们的接口
形态高度一致：**一次 HTTP 请求返回 JSON，里面有若干「可售单元」和余量**。
差异只在三处：URL、字段名、状态取值怎么写。

把这三点抽成配置，就不必为每个平台重写一遍「请求 → 定位数组 → 取字段 →
归一状态」的流程。加新平台的成本从「写一个类」降到「贴一段 YAML」——
这是本项目能持续接平台的真正原因。

配置示例
--------
    - id: my-show
      adapter: json-api
      params:
        # {xxx} 占位符会被 params 里的同名键展开
        url: https://api.example.com/shows/{item_id}/skus
        method: GET                  # GET / POST
        query:                       # 查询参数
          date: "+7"
        json_body:                   # POST 时用
          pageSize: 50
        headers:
          Referer: https://example.com/
        # ---- 从哪里取「场次/车次」数组（点分路径，.0 或 [0] 取数组元素）----
        items_path: data.result.performBases
        item_id: [performId, id]              # 候选路径，取第一个非空
        item_label: [performName, name]
        item_fields:                          # 展示用字段
          depart_time: performTime
        # ---- 票档：相对每个 item 的路径；留空表示 item 本身就是一个票档 ----
        seats_path: ""
        seat_name: [priceName, skuName, name]
        seat_status: [status, remainNum, stock, canBuy]
        # ---- 状态词表（可选，覆盖内置默认）----
        available_values: ["可购买", "有票"]
        sold_out_values: ["售罄", "缺货"]
        # ---- 认证（可选，覆盖 credentials 里的默认值）----
        cookie: "${SOME_COOKIE}"

设计取舍
--------
1. **不认识的状态一律跳过，不猜。** 宁可漏报也不误报——误报会让人白跑一趟，
   漏报只是少一条提醒。未知取值会写进 debug 日志，配合 ``radar probe`` 排查。
2. **路径全部可覆盖。** 平台改字段名时改配置即可，不用动代码、不用重新发版。
3. **报错信息自带结构提示。** 路径找不到时直接把实际响应的 key 树打出来，
   否则用户只看到「解析失败」四个字，还得自己去翻文档。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from ..config import TaskConfig
from ..models import SeatAvailability, Snapshot, TrainState
from .base import HEALTH_UNKNOWN, Adapter, AdapterError, Capability
from .registry import register

log = logging.getLogger("radar.adapters.json_api")

#: 路径取不到值时的哨兵。用它是为了区分「取到了 None」和「压根没这个键」。
MISSING: Any = object()

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 判定「无票」的词（小写比较）
SOLD_OUT_MARKERS = {
    "0", "无", "无票", "售罄", "售完", "已售完", "缺货", "无货",
    "不可售", "不可购买", "停售", "false", "no", "none", "soldout", "sold_out",
}
#: 判定「有票但数量未知」的词
AVAILABLE_MARKERS = {
    "有", "有票", "充足", "充足库存", "可购买", "可售", "在售", "可买",
    "true", "yes", "y", "available", "onsale", "on_sale",
}


# ---------------------------------------------------------------------------
# 路径工具（纯函数，方便单测）
# ---------------------------------------------------------------------------


def split_path(path: Any) -> list[str]:
    """把 ``a.b[0].c`` / ``a.0.c`` / ``[0].c`` 拆成路径段。"""
    s = str(path or "").strip()
    if not s or s == "$":
        return []
    s = s.replace("[", ".").replace("]", ".")
    return [seg for seg in s.split(".") if seg]


def dig(obj: Any, path: Any) -> Any:
    """按点分路径取值；取不到返回 ``MISSING``。

    支持 dict 键与 list 下标（``data.0.name`` 或 ``data[0].name``）。
    """
    segments = split_path(path)
    if not segments:
        return obj
    cur = obj
    for seg in segments:
        if cur is MISSING:
            return MISSING
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
            else:
                return MISSING
        elif isinstance(cur, (list, tuple)):
            if seg.isdigit() and int(seg) < len(cur):
                cur = cur[int(seg)]
            else:
                return MISSING
        else:
            return MISSING
    return cur


def _as_paths(value: Any) -> list[str]:
    """把「候选路径」的几种写法统一成列表。

    支持 ``"a.b"``、``["a.b", "c.d"]``、``"a.b|c.d"``（配置里用竖线分隔更省字数）。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    raw = str(value)
    parts = [p.strip() for chunk in raw.split("|") for p in chunk.split(",")]
    return [p for p in parts if p]


def first_of(obj: Any, paths: Any, default: Any = None) -> Any:
    """按候选路径顺序取第一个「有内容」的值。"""
    for path in _as_paths(paths):
        val = dig(obj, path)
        if val is MISSING or val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return val
    return default


def shape(obj: Any, *, depth: int = 3, max_items: int = 8) -> str:
    """把 JSON 结构渲染成缩进的 key 树，用于报错提示和 ``radar probe``。

    不打印全部数据，只打印结构——响应体可能很大，全量打印没有意义。
    """
    lines: list[str] = []

    def walk(node: Any, prefix: str, level: int) -> None:
        if level > depth:
            return
        if isinstance(node, dict):
            for i, (key, val) in enumerate(node.items()):
                if i >= max_items:
                    lines.append(f"{prefix}… 还有 {len(node) - max_items} 个键")
                    break
                lines.append(f"{prefix}{key}:{_type_tag(val)}")
                walk(val, prefix + "  ", level + 1)
        elif isinstance(node, list):
            if not node:
                return
            lines.append(f"{prefix}[0]")
            walk(node[0], prefix + "  ", level + 1)

    lines.append(f"根:{_type_tag(obj)}")
    walk(obj, "  ", 1)
    return "\n".join(lines)


def _type_tag(node: Any) -> str:
    if isinstance(node, dict):
        return f" dict({len(node)})"
    if isinstance(node, list):
        return f" list({len(node)})"
    if isinstance(node, str):
        preview = node if len(node) <= 24 else node[:24] + "…"
        return f" {preview!r}"
    return f" {node!r}"


def coerce_availability(
    seat_type: str,
    raw: Any,
    *,
    available_values: Any = None,
    sold_out_values: Any = None,
) -> SeatAvailability | None:
    """把平台的状态取值归一成 ``SeatAvailability``。

    返回 ``None`` 表示「这个值看不懂，跳过」——不猜、不误报。

    归一规则：
    * 数字 ``n``：``n > 0`` 有 n 张；``n == 0`` 无票
    * ``True`` / ``False``：有票（数量未知）/ 无票
    * 字符串里的数字：按数字处理
    * 命中词表：有票（数量未知）/ 无票
    * 其余：跳过
    """
    if raw is None or raw is MISSING:
        return None

    sold_out = {str(v).strip().lower() for v in (sold_out_values or [])} or SOLD_OUT_MARKERS
    available = {str(v).strip().lower() for v in (available_values or [])} or AVAILABLE_MARKERS

    # bool 必须在 int 之前判断：Python 里 True 也是 int
    if isinstance(raw, bool):
        return SeatAvailability(
            seat_type=seat_type, raw=str(raw), count=None if raw else 0, available=raw
        )

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        if n < 0:
            return None  # 负数通常是「不适用」或错误码，不当余票
        return SeatAvailability(seat_type=seat_type, raw=str(n), count=n, available=n > 0)

    text = str(raw).strip()
    if not text:
        return None

    lower = text.lower()
    if lower in sold_out:
        return SeatAvailability(seat_type=seat_type, raw=text, count=0, available=False)
    if lower in available:
        return SeatAvailability(seat_type=seat_type, raw=text, count=None, available=True)
    if text.isdigit():
        n = int(text)
        return SeatAvailability(seat_type=seat_type, raw=text, count=n, available=n > 0)

    log.debug("未知状态取值 seat=%s raw=%r，已跳过（用 radar probe 核对字段）", seat_type, text)
    return None


# ---------------------------------------------------------------------------
# 参数展开
# ---------------------------------------------------------------------------


class _TemplateMap(dict):
    """``str.format_map`` 的容错版本：缺失的键渲染成空串而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


def render_template(value: str, mapping: dict[str, Any]) -> str:
    """展开 ``{xxx}`` 占位符。没有占位符时原样返回。"""
    if "{" not in value:
        return value
    try:
        return value.format_map(_TemplateMap(mapping))
    except (ValueError, IndexError):
        # 用户写了 { 但语义不是占位符，原样返回比抛异常友好
        return value


def template_mapping(params: dict[str, Any], credentials: dict[str, str]) -> dict[str, Any]:
    """构造占位符映射：``url_vars`` + params 顶层标量 + credentials + 常用时间量。

    ``url_vars`` 单独开一个命名空间是有原因的：像 ``item_id`` 这种名字，
    既可能是「URL 里的那个 ID」（一个值），也可能是「从哪个字段取 ID」
    （一个路径）。混在一起就会互相踩。约定是：

    * ``params.url_vars.item_id: "12345"`` —— 值，用于填 URL 占位符
    * ``params.item_id: [id, skuId]``       —— 路径，用于从响应里取字段

    优先级：``url_vars`` > params 顶层标量 > credentials。
    """
    mapping: dict[str, Any] = {}
    for key, val in params.items():
        if isinstance(val, (str, int, float)):
            mapping[key] = val
    extra = params.get("url_vars")
    if isinstance(extra, dict):
        mapping.update(
            {str(k): v for k, v in extra.items() if isinstance(v, (str, int, float))}
        )
    mapping.update({k: v for k, v in credentials.items() if isinstance(v, str)})

    today = dt.date.today()
    mapping.setdefault("today", today.isoformat())
    mapping.setdefault("tomorrow", (today + dt.timedelta(days=1)).isoformat())
    mapping.setdefault("timestamp", int(dt.datetime.now().timestamp()))
    mapping.setdefault("timestamp_ms", int(dt.datetime.now().timestamp() * 1000))
    return mapping


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


@register("json-api")
class JsonApiAdapter(Adapter):
    """配置驱动的通用只读适配器。

    适合「一次 GET/POST 拿回 JSON，字段位置固定」的平台。带签名的平台
    （如大麦 mtop）请继承本类并覆写 :meth:`build_request`。
    """

    name = "json-api"
    min_interval = 300.0
    requires_credentials = True
    base_url = ""

    capability = Capability(
        category="generic",
        summary="通用兜底：任意返回 JSON 的接口，靠配置接入",
        can_search=False,
        limitation="兜底适配器：能力完全取决于你给的配置（接口地址 + 字段路径），"
        "所以不声明搜索能力。只要目标能「一次 HTTP 请求返回 JSON」就能接；"
        "需要 HTML 渲染或签名参数的接口不行。",
    )

    #: 子类可以预设默认字段路径，用户配置只需覆盖差异部分
    defaults: dict[str, Any] = {}

    def param(self, params: dict[str, Any], key: str, fallback: Any = None) -> Any:
        """取配置项，缺省时回退到子类预设。"""
        if key in params and params[key] not in (None, ""):
            return params[key]
        return self.defaults.get(key, fallback)

    # -- 请求构造（子类通常只需覆写这里加签名）-----------------------------

    async def build_request(
        self, task: TaskConfig, client: httpx.AsyncClient
    ) -> tuple[str, str, dict[str, Any]]:
        """返回 ``(method, url, kwargs)``。kwargs 直接喂给 httpx.request。

        子类加签名时覆写本方法，``await super().build_request(...)`` 之后
        往 kwargs["params"] 或 kwargs["headers"] 里补字段即可。
        """
        params = task.params
        mapping = template_mapping(params, self.credentials)

        url = render_template(str(self.param(params, "url", "")), mapping).strip()
        if not url:
            raise AdapterError(
                f"任务 {task.id} 缺少 params.url。\n"
                "先用浏览器打开该平台的余票/详情页 → F12 → Network → "
                "找到返回余量的那个 XHR 请求，把它复制进配置；\n"
                "再用 `radar probe <url> --cookie ...` 确认字段路径。"
            )
        if url.startswith("/"):
            url = (self.base_url or "").rstrip("/") + url
        if not url.startswith(("http://", "https://")):
            raise AdapterError(f"params.url 必须是完整 URL，当前为 {url!r}")

        method = str(self.param(params, "method", "GET")).upper()

        headers = dict(DEFAULT_HEADERS)
        headers.update(self._configured_headers(params.get("headers"), mapping))
        cookie = str(
            params.get("cookie") or self.credentials.get("cookie") or ""
        ).strip()
        if cookie:
            headers["Cookie"] = cookie
        for header_name, cred_key in (self.defaults.get("header_credentials") or {}).items():
            val = self.credentials.get(cred_key, "")
            if val:
                headers[header_name] = val

        kwargs: dict[str, Any] = {"headers": headers}

        query = self.param(params, "query")
        if isinstance(query, dict) and query:
            kwargs["params"] = {
                str(k): render_template(str(v), mapping) for k, v in query.items()
            }

        body = self.param(params, "json_body")
        if isinstance(body, dict) and body:
            kwargs["json"] = body
        form = self.param(params, "form_body")
        if isinstance(form, dict) and form:
            kwargs["data"] = {str(k): render_template(str(v), mapping) for k, v in form.items()}

        return method, url, kwargs

    @staticmethod
    def _configured_headers(raw: Any, mapping: dict[str, Any]) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, val in raw.items():
            if val is None:
                continue
            out[str(key)] = render_template(str(val), mapping)
        return out

    # -- 抓取 ---------------------------------------------------------------

    async def fetch_raw(self, task: TaskConfig, client: httpx.AsyncClient) -> Any:
        """请求一次并返回原始 JSON。供 ``radar probe`` / 排查字段用。"""
        method, url, kwargs = await self.build_request(task, client)
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise AdapterError(f"请求 {url} 失败：{exc}") from exc

        if resp.status_code != 200:
            raise AdapterError(
                f"{url} 返回 HTTP {resp.status_code}。"
                "403/412 常见于风控拦截（通常是缺 Cookie 或 Referer）；"
                "遇到拦截请降低频率或补全凭据，不要尝试绕过。"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(
                f"{url} 返回的不是 JSON（前 200 字符：{resp.text[:200]!r}）。\n"
                "常见原因：接口地址取错、返回了登录页/风控页。"
            ) from exc

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：**这个适配器无法被单独探活**，如实说。

        它没有固定接口——URL、字段路径全在任务配置里。没有任务就不知道
        该请求什么，硬报「正常」就是把「没查」说成「查过了」。

        所以返回「未检查」并指向真正的验证方式：配好任务后用
        ``radar check`` 实测那一轮。
        """
        return HEALTH_UNKNOWN, (
            "通用适配器，探活需要具体任务配置（接口地址 + 字段路径），无法单独检查。"
            "配好后用 radar check -c tasks.yaml --only <任务id> 实测"
        )

    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        data = await self.fetch_raw(task, client)
        trains = self.parse_payload(data, task)
        if not trains:
            raise AdapterError(
                f"任务 {task.id}：请求成功但没解析出任何可售单元。\n"
                f"实际响应结构：\n{shape(data)}\n"
                "请对照上面这棵树修改 params 里的 items_path / seat_name / seat_status。"
            )
        log.info("[%s] 解析到 %d 个可售单元", task.id, len(trains))
        return Snapshot(
            task_id=task.id,
            platform=self.name,
            captured_at=dt.datetime.now(dt.timezone.utc),
            trains=trains,
        )

    # -- 解析 ---------------------------------------------------------------

    def parse_payload(self, data: Any, task: TaskConfig) -> dict[str, TrainState]:
        """把 JSON 解析成 ``{单元ID: TrainState}``。"""
        params = task.params

        items = first_of(data, self.param(params, "items_path"), MISSING)
        if items is MISSING:
            raise AdapterError(
                f"按 items_path={self.param(params, 'items_path')!r} 找不到数组。\n"
                f"实际响应结构：\n{shape(data)}"
            )
        if isinstance(items, dict):
            # 有些接口把列表包在单键对象里，例如 {"list": [...]}
            inner = [v for v in items.values() if isinstance(v, list)]
            if len(inner) == 1:
                items = inner[0]
            else:
                raise AdapterError(
                    f"items_path 指向的是 dict 而不是 list。\n实际结构：\n{shape(data)}"
                )
        if not isinstance(items, list) or not items:
            raise AdapterError(
                f"items_path 解析结果不是非空数组（得到 {type(items).__name__}）。\n"
                f"实际结构：\n{shape(data)}"
            )

        id_paths = self.param(params, "item_id", ["id"])
        label_paths = self.param(params, "item_label", ["name"])
        field_map = self.param(params, "item_fields", {}) or {}
        seats_path = self.param(params, "seats_path", "")
        seat_name_paths = self.param(params, "seat_name", ["name"])
        seat_status_paths = self.param(params, "seat_status", ["status"])
        available_values = self.param(params, "available_values")
        sold_out_values = self.param(params, "sold_out_values")

        trains: dict[str, TrainState] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            code = str(first_of(item, id_paths, "") or "").strip()
            if not code:
                label = str(first_of(item, label_paths, "") or "").strip()
                code = label or f"#{index}"
            # 同一单元可能分多行返回，用 id 归并到同一个 TrainState
            existing = trains.get(code)
            if existing is None:
                extra: dict[str, str] = {}
                for key, path in (field_map or {}).items():
                    val = first_of(item, path, "")
                    extra[str(key)] = "" if val is MISSING else str(val)
                existing = TrainState(
                    train_code=code,
                    from_station=extra.pop("from_station", "") or "",
                    to_station=extra.pop("to_station", "") or "",
                    depart_time=extra.pop("depart_time", "") or "",
                    arrive_time=extra.pop("arrive_time", "") or "",
                    duration=extra.pop("duration", "") or "",
                    seats={},
                )
                trains[code] = existing

            for seat_type, seat in self._seats_of(
                item,
                seats_path,
                seat_name_paths,
                seat_status_paths,
                available_values,
                sold_out_values,
                fallback_label=str(first_of(item, label_paths, "") or ""),
            ):
                # 同名票档重复出现（多分区）时，取更「有票」的那个
                if (
                    seat_type in existing.seats
                    and existing.seats[seat_type].effective >= seat.effective
                ):
                    continue
                existing.seats[seat_type] = seat

        return {code: t for code, t in trains.items() if t.seats}

    def _seats_of(
        self,
        item: dict[str, Any],
        seats_path: Any,
        name_paths: Any,
        status_paths: Any,
        available_values: Any,
        sold_out_values: Any,
        *,
        fallback_label: str = "",
    ) -> list[tuple[str, SeatAvailability]]:
        """产出一组 ``(票档名, 余票)``。"""
        rows: list[Any]
        if seats_path:
            found = dig(item, seats_path)
            if found is MISSING or not isinstance(found, list):
                return []
            rows = found
        else:
            # 没有嵌套票档：item 自身就是一个票档
            rows = [item]

        out: list[tuple[str, SeatAvailability]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(first_of(row, name_paths, "") or "").strip()
            if not name:
                name = fallback_label or "默认票档"
            raw_status = first_of(row, status_paths, None)
            seat = coerce_availability(
                name,
                raw_status,
                available_values=available_values,
                sold_out_values=sold_out_values,
            )
            if seat is not None:
                out.append((name, seat))
        return out


__all__ = [
    "AVAILABLE_MARKERS",
    "DEFAULT_HEADERS",
    "MISSING",
    "SOLD_OUT_MARKERS",
    "JsonApiAdapter",
    "coerce_availability",
    "dig",
    "first_of",
    "render_template",
    "shape",
    "split_path",
    "template_mapping",
]
