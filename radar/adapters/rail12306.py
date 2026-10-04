"""12306 铁路余票适配器。

合规说明（改动前请先读）
------------------------
本适配器只调用 12306 面向浏览器公开的「余票查询」**只读**接口，用于查询
本人关心的车次余票。四条自我约束，请勿修改：

1. **只读**——不调用下单、不调用候补、不携带乘车人身份信息。
2. **低频**——``min_interval`` 默认 60 秒，且 engine 的限流器保证同一平台
   的请求跨任务串行化。不要改成秒级、不要加并发、不要加线程池。
3. **单账号**——不轮换账号、不使用代理 IP 池、不做设备指纹伪装。
4. **不对抗风控**——遇到限流/拦截就退避报错，不研究怎么绕过去。

为什么这四条是硬约束：2026 年 4 月，中央网信办与国家铁路局联合约谈 7 家
涉火车票销售的第三方平台，明确要求「不得利用自动化程序实施大规模、高频次
的抢票操作干扰铁路 12306 平台的安全核验措施」。把上面的参数调一调，这个
项目就从「余票信息聚合」变成了被约谈的那类程序，性质完全不同。

技术备注
--------
12306 的余票接口没有公开文档，返回体是 ``|`` 分隔的裸字符串数组，字段位置
靠社区逆向积累，**存在随版本漂移的可能**。因此：

* 字段索引集中定义在 ``TRAIN_FIELDS`` / ``SEAT_FIELDS``，可用 task params
  里的 ``train_fields`` / ``seat_fields`` 覆盖，不必改代码。
* 所有取值都做过越界保护，字段缺失只会让该席别消失，不会抛异常。
* ``radar debug-raw`` 命令可以直接打印原始字段和下标，方便对照排查。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx

from ..config import TaskConfig
from ..models import SeatAvailability, Snapshot, TrainState
from .base import HEALTH_BROKEN, HEALTH_OK, Adapter, AdapterError, Capability
from .registry import register

log = logging.getLogger("radar.adapters.rail12306")

BASE_URL = "https://kyfw.12306.cn"
INIT_URL = f"{BASE_URL}/otn/leftTicket/init"
STATION_JS_URL = f"{BASE_URL}/otn/resources/js/framework/station_name.js"

#: 查询端点。12306 会在几个等价路径之间切换，按顺序试探并记住可用的那个。
QUERY_ENDPOINTS = ("queryE", "queryZ", "queryA", "query")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": INIT_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

#: 裸字符串数组的字段下标。改这些之前先用 `radar debug-raw` 确认真实位置。
TRAIN_FIELDS: dict[str, int] = {
    "train_code": 3,
    "from_station_code": 6,
    "to_station_code": 7,
    "depart_time": 8,
    "arrive_time": 9,
    "duration": 10,
    "can_web_buy": 11,
    # 下面四项不用于展示，只在补查票价时当参数用（见 enrich_prices）。
    # 余票接口不带价格，价格得拿 train_no + 站序去另一个端点换。
    "train_no": 2,
    "from_station_no": 16,
    "to_station_no": 17,
    "seat_types": 35,
}

#: 席别下标 -> 中文名
SEAT_FIELDS: dict[int, str] = {
    21: "高级软卧",
    22: "其他",
    23: "软卧",
    24: "软座",
    25: "特等座",
    26: "无座",
    27: "硬卧包房",
    28: "硬卧",
    29: "硬座",
    30: "二等座",
    31: "一等座",
    32: "商务座",
    33: "动卧",
}

#: 票价接口里的席别代号 -> 本项目的中文席别名。
#: 一个中文席别列多个代号是因为 12306 对不同车次给的档位不一样：
#: 动车组给 D（优选一等座），普速给 1/2（硬座）。按顺序取第一个命中的。
SEAT_PRICE_CODES: dict[str, tuple[str, ...]] = {
    "商务座": ("A9", "9"),
    "特等座": ("P",),
    "优选一等座": ("D",),
    "一等座": ("M",),
    "二等座": ("O",),
    "无座": ("WZ",),
    "硬座": ("1", "2"),
    "硬卧": ("3",),
    "软卧": ("4",),
    "高级软卧": ("6",),
    "动卧": ("F",),
}

#: 「这个席别本次列车不提供」的标记，解析时直接跳过，不产生余票记录。
_NOT_OFFERED = {"", "--", "*", "—", "-"}
#: 「有票但数量未知」的标记
_ABUNDANT_MARKERS = {"有", "充足", "有票"}
#: 「无票」的标记
_SOLD_OUT_MARKERS = {"无", "无票", "0"}

_STATION_CACHE_FILE = Path.home() / ".ticket-radar" / "station_name.js"

#: 站名表进程内缓存（一次运行只下载一次）
_STATION_TABLE: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# 解析工具（纯函数，便于单测）
# ---------------------------------------------------------------------------


def resolve_date(spec: str | int, *, today: dt.date | None = None) -> str:
    """把日期写法归一成 ``YYYY-MM-DD``。

    支持：``2026-10-01``、``20261001``、``today``、``tomorrow``、
    ``+7`` 和裸数字 ``7``（都表示 7 天后）、``-1``（昨天）。

    为什么要兼容裸数字：YAML 会把不引号的 ``+7`` 解析成整数 ``7``，
    再 ``str()`` 出来就是 ``"7"``。早期版本只认 ``+7``，导致照着示例配置
    写的人第一步就报「无法解析日期」。示例里现在写成 ``"+7"``，
    同时这里也把裸数字当作天数偏移，两条路都通。
    """
    base = today or dt.date.today()
    s = str(spec if spec is not None else "").strip().lower()
    if not s:
        raise AdapterError("params.date 不能为空")

    if s in {"today", "今天"}:
        return base.isoformat()
    if s in {"tomorrow", "明天"}:
        return (base + dt.timedelta(days=1)).isoformat()
    # 1~3 位数字（含正负号）按「N 天后」处理；覆盖 "0" "+7" "7" "-1"
    if re.fullmatch(r"[+-]?\d{1,3}", s):
        return (base + dt.timedelta(days=int(s))).isoformat()
    # 紧凑日期 20261001
    if re.fullmatch(r"\d{8}", s):
        return dt.datetime.strptime(s, "%Y%m%d").date().isoformat()

    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError as exc:
        raise AdapterError(
            f"无法解析日期 {spec!r}；支持 YYYY-MM-DD / YYYYMMDD / today / tomorrow / +N"
        ) from exc


def parse_seat_value(seat_type: str, raw: str) -> SeatAvailability | None:
    """解析单个席别的余票值。返回 None 表示本次列车不提供该席别。"""
    value = (raw or "").strip()
    if value in _NOT_OFFERED:
        return None
    if value in _ABUNDANT_MARKERS:
        return SeatAvailability(seat_type=seat_type, raw=value, count=None, available=True)
    if value in _SOLD_OUT_MARKERS:
        return SeatAvailability(seat_type=seat_type, raw=value, count=0, available=False)
    if value.isdigit():
        n = int(value)
        return SeatAvailability(seat_type=seat_type, raw=value, count=n, available=n > 0)
    # 未知取值：宁可漏报也不误报，交给 debug-raw 排查
    log.debug("未知席别取值 seat=%s raw=%r，已跳过", seat_type, value)
    return None


def _field(fields: list[str], index: int) -> str:
    """安全取下标，越界返回空串。"""
    if 0 <= index < len(fields):
        return fields[index]
    return ""


def parse_price(raw: str) -> float | None:
    """解析票价字段，统一成「元」。

    12306 这里给两种格式，而且**单位不同**：

    * ``"¥661.0"``   —— 带货币符号，单位是元
    * ``"6610"``     —— 裸数字，单位是角（同一个价格在另一档位出现时）

    不区分这两个就会把 ¥661 的车票显示成 ¥6610。
    """
    text = (raw or "").strip()
    if not text:
        return None
    in_yuan = "¥" in text or "￥" in text
    digits = text.replace("¥", "").replace("￥", "").strip()
    try:
        value = float(digits)
    except ValueError:
        return None
    if value <= 0:
        return None
    return round(value if in_yuan else value / 10, 1)


def parse_ticket_prices(payload: dict[str, Any]) -> dict[str, float]:
    """把 ``queryTicketPrice`` 的返回体转成 ``{中文席别名: 元}``。

    返回体形如::

        {"data": {"O": "¥661.0", "M": "¥1058.0", "A9": "¥2315.0",
                  "MIN": "¥1468.0", "OT": ["优选一等座: ¥1468.0"], ...}}

    ``MIN`` / ``OT`` 这类聚合字段直接忽略——要的是分席别的价，不是最低价。
    """
    data = (payload or {}).get("data") or {}
    prices: dict[str, float] = {}
    for seat_type, codes in SEAT_PRICE_CODES.items():
        for code in codes:
            if code not in data:
                continue
            price = parse_price(str(data[code]))
            if price is not None:
                prices[seat_type] = price
                break
    return prices


def parse_trains(
    rows: list[str],
    station_map: dict[str, str] | None = None,
    *,
    train_fields: dict[str, int] | None = None,
    seat_fields: dict[int, str] | None = None,
) -> dict[str, TrainState]:
    """把 12306 的裸字符串数组解析成 ``{车次: TrainState}``。"""
    tf = dict(TRAIN_FIELDS)
    if train_fields:
        tf.update(train_fields)
    sf = dict(SEAT_FIELDS)
    if seat_fields:
        sf.update(seat_fields)
    smap = station_map or {}

    trains: dict[str, TrainState] = {}
    for row in rows:
        if not row:
            continue
        fields = row.split("|")
        code = _field(fields, tf["train_code"]).strip()
        if not code:
            continue

        seats: dict[str, SeatAvailability] = {}
        for index, seat_type in sf.items():
            raw = _field(fields, index)
            seat = parse_seat_value(seat_type, raw)
            if seat is not None:
                seats[seat_type] = seat

        from_code = _field(fields, tf["from_station_code"]).strip()
        to_code = _field(fields, tf["to_station_code"]).strip()

        trains[code] = TrainState(
            train_code=code,
            from_station=smap.get(from_code, from_code),
            to_station=smap.get(to_code, to_code),
            depart_time=_field(fields, tf["depart_time"]).strip(),
            arrive_time=_field(fields, tf["arrive_time"]).strip(),
            duration=_field(fields, tf["duration"]).strip(),
            seats=seats,
            # 顺手记下补查票价要用的凭据，省得为了查价再重新抓一遍余票
            extra={
                "train_no": _field(fields, tf["train_no"]).strip(),
                "from_station_no": _field(fields, tf["from_station_no"]).strip(),
                "to_station_no": _field(fields, tf["to_station_no"]).strip(),
                "seat_types": _field(fields, tf["seat_types"]).strip(),
            },
        )
    return trains


def parse_station_js(text: str) -> dict[str, str]:
    """解析 station_name.js，返回「中文名/拼音/电报码 -> 电报码」的查询表。

    原始格式：``@bjb|北京北|VAP|beijingbei|bjb|0@bjd|北京东|BOP|...``
    字段依次为：简拼 | 中文名 | 电报码 | 全拼 | 首字母 | 序号
    """
    match = re.search(r"'(.*)'", text, re.DOTALL)
    body = match.group(1) if match else text
    table: dict[str, str] = {}
    for entry in body.split("@"):
        parts = entry.split("|")
        if len(parts) < 5:
            continue
        abbr, zh_name, telecode, pinyin, initials = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not telecode:
            continue
        table[telecode] = telecode
        if zh_name:
            table[zh_name] = telecode
        for alias in (pinyin, initials, abbr):
            if alias:
                table.setdefault(alias.lower(), telecode)
    return table


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


@register("rail12306")
class Rail12306Adapter(Adapter):
    """中国铁路 12306 余票查询。只读、低频、单账号。"""

    name = "rail12306"
    min_interval = 60.0
    requires_credentials = False
    base_url = BASE_URL

    capability = Capability(
        category="train",
        summary="中国铁路 12306 余票查询（只读，匿名可用）",
        can_search=True,
        seat_level=True,
        regions=("CN",),
        limitation="只能按「出发站-到达站-日期」查，查不了站名以外的关键词；"
        "候补、中转换乘不在支持范围内。",
    )

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._working_endpoint: str | None = None
        self._warmed_up = False

    # -- 会话 -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        cookie = (self.credentials.get("cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _warm_up(self, client: httpx.AsyncClient) -> None:
        """先访问一次页面，让服务端下发 JSESSIONID。

        没带自己 Cookie 的时候，这一步能显著提高查询成功率。
        """
        if self._warmed_up:
            return
        self._warmed_up = True
        try:
            await client.get(INIT_URL, headers=self._headers())
        except httpx.HTTPError as exc:
            log.debug("预热会话失败（不影响后续尝试）：%s", exc)

    # -- 站名表 -----------------------------------------------------------

    @staticmethod
    async def _station_table(client: httpx.AsyncClient) -> dict[str, str]:
        """加载站名 -> 电报码映射，进程内 + 磁盘双缓存。"""
        global _STATION_TABLE
        if _STATION_TABLE is not None:
            return _STATION_TABLE

        if _STATION_CACHE_FILE.exists():
            try:
                _STATION_TABLE = parse_station_js(
                    _STATION_CACHE_FILE.read_text(encoding="utf-8")
                )
                return _STATION_TABLE
            except OSError as exc:
                log.debug("站名表缓存读取失败，改为重新下载：%s", exc)

        try:
            resp = await client.get(
                STATION_JS_URL, headers=DEFAULT_HEADERS, timeout=httpx.Timeout(30.0)
            )
            resp.raise_for_status()
            text = resp.text
        except httpx.HTTPError as exc:
            raise AdapterError(f"下载 12306 站名表失败：{exc}") from exc

        _STATION_TABLE = parse_station_js(text)
        if len(_STATION_TABLE) < 100:
            raise AdapterError("站名表解析结果异常（条目过少），接口可能已变更")

        try:
            _STATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATION_CACHE_FILE.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.debug("站名表缓存写入失败：%s", exc)

        return _STATION_TABLE

    async def _to_telecode(self, client: httpx.AsyncClient, value: str) -> str:
        table = await self._station_table(client)
        key = value.strip()
        if key in table:
            return table[key]
        if key.lower() in table:
            return table[key.lower()]
        raise AdapterError(
            f"无法识别车站 {value!r}；请用中文站名（如「北京南」）或电报码（如 VNP）"
        )

    # -- 查询 -------------------------------------------------------------

    async def _query_raw(
        self, client: httpx.AsyncClient, travel_date: str, from_code: str, to_code: str
    ) -> dict[str, Any]:
        """按顺序试探可用端点，成功后记住，避免每次都试错。"""
        await self._warm_up(client)

        params = {
            "leftTicketDTO.train_date": travel_date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        }

        candidates: list[str] = []
        if self._working_endpoint:
            candidates.append(self._working_endpoint)
        candidates.extend(e for e in QUERY_ENDPOINTS if e not in candidates)

        last_error: Exception | None = None
        for endpoint in candidates:
            url = f"{BASE_URL}/otn/leftTicket/{endpoint}"
            try:
                resp = await client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = exc
                log.debug("端点 %s 请求异常：%s", endpoint, exc)
                continue

            if resp.status_code != 200:
                last_error = AdapterError(f"HTTP {resp.status_code}")
                continue
            if "html" in resp.headers.get("content-type", "") and "<html" in resp.text[:200].lower():
                last_error = AdapterError("返回了 HTML（通常是风控拦截页）")
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                last_error = AdapterError(f"响应不是合法 JSON：{exc}")
                continue

            if data.get("data"):
                self._working_endpoint = endpoint
                return data
            last_error = AdapterError(f"响应体无 data 字段：{str(data)[:200]}")

        raise AdapterError(
            f"12306 余票查询失败（{travel_date} {from_code}->{to_code}）：{last_error}\n"
            "常见原因：日期超出预售期、车站码不对、Cookie 过期、或触发了风控限流。"
            "遇到风控请降低频率或稍后重试，不要尝试绕过。"
        )

    async def fetch_raw(self, task: TaskConfig, client: httpx.AsyncClient) -> dict[str, Any]:
        """返回原始 JSON。供 `radar debug-raw` 排查字段漂移用。"""
        params = task.params
        travel_date = resolve_date(str(params.get("date", "today")))
        from_code = await self._to_telecode(client, str(params["from"]))
        to_code = await self._to_telecode(client, str(params["to"]))
        return await self._query_raw(client, travel_date, from_code, to_code)

    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        params = task.params
        for required in ("from", "to"):
            if not params.get(required):
                raise AdapterError(f"任务 {task.id} 缺少 params.{required}")

        travel_date = resolve_date(str(params.get("date", "today")))
        from_code = await self._to_telecode(client, str(params["from"]))
        to_code = await self._to_telecode(client, str(params["to"]))

        data = await self._query_raw(client, travel_date, from_code, to_code)
        body = data.get("data") or {}
        rows: list[str] = body.get("result") or []
        station_map: dict[str, str] = body.get("map") or {}

        trains = parse_trains(
            rows,
            station_map,
            train_fields=params.get("train_fields"),
            seat_fields=_coerce_seat_fields(params.get("seat_fields")),
        )

        log.info(
            "[%s] %s %s->%s 解析到 %d 趟车",
            task.id,
            travel_date,
            params.get("from"),
            params.get("to"),
            len(trains),
        )

        return Snapshot(
            task_id=task.id,
            platform=self.name,
            captured_at=dt.datetime.now(dt.timezone.utc),
            # 配置里写的是 "+7" 这类相对日期，只有这里知道它到底指向哪一天。
            # 不带给上层，推送里就只剩「检测时间」，多日期任务分不清是哪天的票。
            context={"乘车日期": travel_date},
            trains=trains,
        )

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：拉一次站名表。这是全项目最轻的请求，且不需要任何任务参数。

        刻意**绕开** :meth:`_station_table` 的磁盘缓存——体检要证明的是
        「现在连得上」，命中缓存只能证明「上次连得上」。缓存让体检报告
        变成谎报，那还不如不检查。
        """
        try:
            resp = await client.get(
                STATION_JS_URL, headers=DEFAULT_HEADERS, timeout=httpx.Timeout(20.0)
            )
            resp.raise_for_status()
            table = parse_station_js(resp.text)
        except httpx.HTTPError as exc:
            return HEALTH_BROKEN, f"连不上 12306：{exc}"
        if len(table) < 100:
            return HEALTH_BROKEN, f"站名表只解析出 {len(table)} 条，接口结构可能已变"
        return HEALTH_OK, f"站名表 {len(table)} 条，查询接口可达"

    async def enrich_prices(
        self,
        snapshot: Snapshot,
        task: TaskConfig,
        client: httpx.AsyncClient,
        train_codes: Collection[str],
    ) -> Snapshot:
        """去票价端点补 ``train_codes`` 的票价。

        每趟车一次请求，所以 engine 只在**真要发通知**时才调它——
        常态轮询仍然保持每轮 1 次请求。任何一步失败都只是没有票价，
        不影响余票提醒本身。
        """
        targets = [code for code in train_codes if code in snapshot.trains]
        if not targets:
            return snapshot

        travel_date = resolve_date(str(task.params.get("date", "today")))
        await self._warm_up(client)

        trains = dict(snapshot.trains)
        for code in targets:
            train = trains[code]
            info = train.extra
            if not info.get("train_no") or not info.get("seat_types"):
                continue

            params = {
                "train_no": info["train_no"],
                "from_station_no": info.get("from_station_no", ""),
                "to_station_no": info.get("to_station_no", ""),
                "seat_types": info["seat_types"],
                "train_date": travel_date,
            }
            try:
                resp = await client.get(
                    f"{BASE_URL}/otn/leftTicket/queryTicketPrice",
                    params=params,
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    log.debug("[%s] 票价端点返回 HTTP %s", code, resp.status_code)
                    continue
                prices = parse_ticket_prices(resp.json())
            except Exception as exc:  # 拿不到票价不算错误，静默降级
                log.debug("[%s] 补查票价失败：%s", code, exc)
                continue

            if not prices:
                continue
            trains[code] = dataclasses.replace(
                train,
                seats={
                    seat_type: (
                        dataclasses.replace(seat, price=prices[seat_type])
                        if seat_type in prices
                        else seat
                    )
                    for seat_type, seat in train.seats.items()
                },
            )
            log.debug("[%s] 补到票价 %s", code, prices)

        return dataclasses.replace(snapshot, trains=trains)


def _coerce_seat_fields(raw: Any) -> dict[int, str] | None:
    """把配置里的 ``{"30": "二等座"}`` 形式的覆盖表转成 int 键。"""
    if not raw or not isinstance(raw, dict):
        return None
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out or None


__all__ = [
    "DEFAULT_HEADERS",
    "QUERY_ENDPOINTS",
    "SEAT_FIELDS",
    "TRAIN_FIELDS",
    "Rail12306Adapter",
    "parse_seat_value",
    "parse_station_js",
    "parse_trains",
    "resolve_date",
]
