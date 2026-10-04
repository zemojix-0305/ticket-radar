"""Amadeus 机票余座适配器（正规开放 API，非爬虫）。

为什么机票走 API 而不是爬携程/去哪儿
-------------------------------------
1. **合法**：Amadeus Self-Service 是航司分销体系里的官方接口，注册即用，
   有明确的开发者协议。爬 OTA 页面则踩的是同一类"未经授权获取数据"的线。
2. **稳定**：字段有正式文档，不会因为前端改版就失效。
3. **可写进简历**：OAuth2 client_credentials + REST 集成，
   是比"我会写爬虫"更正面的一笔。

Test 环境免费、不限量，数据是缓存的（不能真出票），足够做监控演示。
正式环境要签约，本项目默认走 Test。

需要的凭据
----------
到 https://developers.amadeus.com 注册 → 建一个 App → 拿到
``API Key``（即 client_id）和 ``API Secret``，填进 .env：

    AMADEUS_CLIENT_ID=xxx
    AMADEUS_CLIENT_SECRET=yyy

然后配任务：

    - id: pek-sha
      adapter: amadeus
      interval_seconds: 600
      credentials: amadeus
      link: https://www.amadeus.com/
      params:
        from: PEK            # 机场三字码，不是城市名
        to: SHA
        date: "+14"
        adults: 1
        currency: CNY
        max: 20
      watch:
        seat_types: ["经济舱"]
        min_count: 1
        notify_on: ["appeared", "increased"]

关于"余票"的语义
----------------
Amadeus 的 ``numberOfBookableSeats`` 是该报价还能卖几个座位。机票不像车票
按席别分档，所以这里把一个舱位（默认「经济舱」）当成一个席别。想区分舱位
可用 ``params.travel_class``（ECONOMY / BUSINESS / FIRST / PREMIUM_ECONOMY）。

注意：通知里**不显示价格**。因为价格波动会让票档名漂移，进而把"降价"误判成
"余票出现"。稳定优先——要盯价格是另一个需求（价格监控），不该混进余票状态机。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from ..config import TaskConfig
from ..models import SeatAvailability, Snapshot, TrainState
from .base import HEALTH_AUTH, HEALTH_OK, Adapter, AdapterError, Capability
from .rail12306 import resolve_date
from .registry import register

log = logging.getLogger("radar.adapters.amadeus")

TEST_HOST = "https://test.api.amadeus.com"
PROD_HOST = "https://api.amadeus.com"

#: token 提前多少秒视为过期，避免边界上刚好失效
TOKEN_SAFETY_SECONDS = 60

TRAVEL_CLASS_LABELS = {
    "ECONOMY": "经济舱",
    "PREMIUM_ECONOMY": "超级经济舱",
    "BUSINESS": "公务舱",
    "FIRST": "头等舱",
}


def format_duration(iso_duration: str) -> str:
    """``PT2H15M`` -> ``2h15m``；解析不了就原样返回。"""
    text = (iso_duration or "").strip().upper()
    if not text.startswith("PT"):
        return iso_duration or ""
    body = text[2:]
    hours = minutes = 0
    num = ""
    for ch in body:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours = int(num or 0)
            num = ""
        elif ch == "M":
            minutes = int(num or 0)
            num = ""
        else:
            num = ""
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return "".join(parts)


def format_time(iso_datetime: str) -> str:
    """``2026-10-05T08:30:00`` -> ``08:30``（跨天会带 ``+1`` 后缀提示）。"""
    text = (iso_datetime or "").strip()
    if "T" not in text:
        return text
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%H:%M")


def _day_offset(origin_iso: str, target_iso: str) -> int:
    """到达日相对出发日的天数差（用于跨天航班的 +1 提示）。"""
    try:
        a = dt.datetime.fromisoformat(origin_iso)
        b = dt.datetime.fromisoformat(target_iso)
    except (ValueError, TypeError):
        return 0
    return max((b.date() - a.date()).days, 0)


def parse_flight_offers(payload: Any, *, travel_class: str = "ECONOMY") -> dict[str, TrainState]:
    """把 Flight Offers Search 响应解析成 ``{航班号: TrainState}``。

    一个 offer 里的 ``itineraries`` 通常只有一段（单程）。多段行程会把
    所有航班号用 ``+`` 连起来作为键。
    """
    offers = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(offers, list):
        raise AdapterError(
            f"响应里没有 data 数组。实际内容：{str(payload)[:300]!r}\n"
            "常见原因：凭据无效（401）、参数不全（400）、或超出 Test 环境限制。"
        )

    seat_label = TRAVEL_CLASS_LABELS.get(travel_class.upper(), travel_class)
    trains: dict[str, TrainState] = {}

    for offer in offers:
        if not isinstance(offer, dict):
            continue
        itineraries = offer.get("itineraries") or []
        if not itineraries:
            continue
        first = itineraries[0] or {}
        segments = first.get("segments") or []
        if not segments:
            continue

        codes: list[str] = []
        for seg in segments:
            carrier = str(seg.get("carrierCode") or "")
            number = str(seg.get("number") or "")
            if carrier or number:
                codes.append(f"{carrier}{number}")
        if not codes:
            continue
        code = "+".join(codes)

        head = segments[0]
        tail = segments[-1]
        depart_iso = str((head.get("departure") or {}).get("at") or "")
        arrive_iso = str((tail.get("arrival") or {}).get("at") or "")
        offset = _day_offset(depart_iso, arrive_iso)

        seats_left = offer.get("numberOfBookableSeats")
        # 没有这个字段时：能查到报价本身就说明有座，但数量未知（None ≠ 0）
        count = int(seats_left) if isinstance(seats_left, (int, float)) else None

        seat = SeatAvailability(
            seat_type=seat_label,
            raw=str(seats_left if seats_left is not None else ""),
            count=count,
            available=True,
        )

        # 同一航班多个报价：保留座位数更多的那个（对用户更宽松）
        existing = trains.get(code)
        if (
            existing is not None
            and existing.seats.get(seat_label) is not None
            and existing.seats[seat_label].effective >= seat.effective
        ):
            continue

        trains[code] = TrainState(
            train_code=code,
            from_station=str((head.get("departure") or {}).get("iataCode") or ""),
            to_station=str((tail.get("arrival") or {}).get("iataCode") or ""),
            depart_time=format_time(depart_iso) + ("+1" if offset else ""),
            arrive_time=format_time(arrive_iso) + ("+1" if offset else ""),
            duration=format_duration(str(first.get("duration") or "")),
            seats={seat_label: seat},
        )

    return trains


@register("amadeus")
class AmadeusAdapter(Adapter):
    """Amadeus Flight Offers Search：只读查询航班与可订座位数。"""

    name = "amadeus"
    min_interval = 300.0
    requires_credentials = True
    base_url = TEST_HOST

    capability = Capability(
        category="flight",
        summary="机票：Amadeus 正规开放 API，Test 环境免费不限量",
        can_search=True,
        seat_level=True,
        regions=(),  # 全球航线，不限地区
        limitation="需要你自己申请 Amadeus 开发者账号（免费档有调用次数上限）；"
        "返回的是可订座位数，不是精确余票。",
    )

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._access_token: str | None = None
        self._token_expires_at: dt.datetime | None = None

    @property
    def host(self) -> str:
        raw = (self.credentials.get("host") or "").strip().rstrip("/")
        if raw:
            return raw if raw.startswith("http") else f"https://{raw}"
        return TEST_HOST

    async def doctor(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """探活：取一次 access token 就够，不发搜索请求（省配额）。

        Amadeus 免费档有调用次数上限，所以探活必须比 ``fetch`` 更省——
        一次 token 请求即可证明「Key/Secret 有效、host 配对」。
        """
        client_id = (self.credentials.get("client_id") or "").strip()
        client_secret = (self.credentials.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            missing = [
                name
                for name, value in (("AMADEUS_CLIENT_ID", client_id), ("AMADEUS_CLIENT_SECRET", client_secret))
                if not value
            ]
            return HEALTH_AUTH, (
                f"凭据没填：{'、'.join(missing)}。去 developers.amadeus.com 注册建 App 后填进 .env"
            )
        try:
            await self._ensure_token(client)
        except AdapterError as exc:
            return HEALTH_AUTH, f"取 access token 失败，Key/Secret 可能无效：{str(exc)[:140]}"
        return HEALTH_OK, f"凭据有效，已取到 access token（{self.host}）"

    # -- OAuth2 -------------------------------------------------------------

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        """取（或复用）access token。client_credentials 模式，无需用户授权。"""
        now = dt.datetime.now(dt.timezone.utc)
        if self._access_token and self._token_expires_at and now < self._token_expires_at:
            return self._access_token

        client_id = (self.credentials.get("client_id") or "").strip()
        client_secret = (self.credentials.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise AdapterError(
                "缺少 Amadeus 凭据。请到 https://developers.amadeus.com 注册并创建 App，\n"
                "然后把 API Key / API Secret 填进 .env：\n"
                "    AMADEUS_CLIENT_ID=xxx\n"
                "    AMADEUS_CLIENT_SECRET=yyy"
            )

        try:
            resp = await client.post(
                f"{self.host}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"请求 Amadeus token 失败：{exc}") from exc

        if resp.status_code != 200:
            raise AdapterError(
                f"获取 Amadeus token 失败（HTTP {resp.status_code}）：{resp.text[:200]}"
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AdapterError(f"token 响应里没有 access_token：{str(body)[:200]}")
        self._access_token = str(token)
        expires_in = int(body.get("expires_in") or 1799)
        self._token_expires_at = now + dt.timedelta(
            seconds=max(expires_in - TOKEN_SAFETY_SECONDS, 30)
        )
        return self._access_token

    # -- 查询 ---------------------------------------------------------------

    async def fetch_raw(self, task: TaskConfig, client: httpx.AsyncClient) -> Any:
        """返回 Flight Offers 原始 JSON，供排查字段用。"""
        params = task.params
        for key in ("from", "to"):
            if not params.get(key):
                raise AdapterError(f"任务 {task.id} 缺少 params.{key}（机场三字码，如 PEK）")

        query = {
            "originLocationCode": str(params["from"]).upper(),
            "destinationLocationCode": str(params["to"]).upper(),
            "departureDate": resolve_date(str(params.get("date", "+14"))),
            "adults": int(params.get("adults") or 1),
            "currencyCode": str(params.get("currency") or "CNY"),
            "max": int(params.get("max") or 20),
        }
        if params.get("travel_class"):
            query["travelClass"] = str(params["travel_class"]).upper()
        if params.get("non_stop"):
            query["nonStop"] = "true"

        token = await self._ensure_token(client)
        try:
            resp = await client.get(
                f"{self.host}/v2/shopping/flight-offers",
                params=query,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"查询 Amadeus 航班失败：{exc}") from exc

        if resp.status_code == 401:
            # token 可能被服务端提前失效，清掉重新取一次
            self._access_token = None
            token = await self._ensure_token(client)
            resp = await client.get(
                f"{self.host}/v2/shopping/flight-offers",
                params=query,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise AdapterError(
                f"Amadeus 返回 HTTP {resp.status_code}：{resp.text[:300]}\n"
                "常见原因：机场码不存在、日期超出可售期、Test 环境不支持该航线。"
            )
        return resp.json()

    async def fetch(self, task: TaskConfig, client: httpx.AsyncClient) -> Snapshot:
        payload = await self.fetch_raw(task, client)
        travel_class = str(task.params.get("travel_class") or "ECONOMY")
        trains = parse_flight_offers(payload, travel_class=travel_class)
        if not trains:
            raise AdapterError(
                f"任务 {task.id}：查询成功但没有可用航班。\n"
                "可能是该航线在 Test 环境无数据，换个热门航线（如 PEK→SHA）试试。"
            )
        log.info("[%s] 解析到 %d 个航班", task.id, len(trains))
        return Snapshot(
            task_id=task.id,
            platform=self.name,
            captured_at=dt.datetime.now(dt.timezone.utc),
            # 与 rail12306 同理：把相对日期解析后的真实日期带给推送文案
            context={"出发日期": resolve_date(str(task.params.get("date", "+14")))},
            trains=trains,
        )


__all__ = [
    "PROD_HOST",
    "TEST_HOST",
    "AmadeusAdapter",
    "format_duration",
    "format_time",
    "parse_flight_offers",
]
