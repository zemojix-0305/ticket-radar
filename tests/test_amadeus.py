"""Amadeus 机票适配器单测（全部离线，走 MockTransport）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from radar.adapters.amadeus import (
    TEST_HOST,
    AmadeusAdapter,
    format_duration,
    format_time,
    parse_flight_offers,
)
from radar.adapters.registry import create_adapter
from radar.config import TaskConfig

OFFERS = {
    "data": [
        {
            "numberOfBookableSeats": 9,
            "itineraries": [
                {
                    "duration": "PT2H15M",
                    "segments": [
                        {
                            "carrierCode": "MU",
                            "number": "5101",
                            "departure": {"iataCode": "PEK", "at": "2026-10-05T08:30:00"},
                            "arrival": {"iataCode": "SHA", "at": "2026-10-05T10:45:00"},
                        }
                    ],
                }
            ],
        },
        {
            "numberOfBookableSeats": 3,
            "itineraries": [
                {
                    "duration": "PT26H25M",
                    "segments": [
                        {
                            "carrierCode": "CA",
                            "number": "1501",
                            "departure": {"iataCode": "PEK", "at": "2026-10-05T21:00:00"},
                            "arrival": {"iataCode": "SHA", "at": "2026-10-06T23:25:00"},
                        }
                    ],
                }
            ],
        },
    ]
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("PT2H15M", "2h15m"), ("PT45M", "45m"), ("PT3H", "3h"), ("", ""), ("bogus", "bogus")],
)
def test_format_duration(raw, expected):
    assert format_duration(raw) == expected


def test_format_time():
    assert format_time("2026-10-05T08:30:00") == "08:30"
    assert format_time("2026-10-05") == "2026-10-05"  # 没有 T 就原样返回


def test_parse_flight_offers_basic():
    trains = parse_flight_offers(OFFERS)
    assert set(trains) == {"MU5101", "CA1501"}

    mu = trains["MU5101"]
    assert mu.from_station == "PEK"
    assert mu.to_station == "SHA"
    assert mu.depart_time == "08:30"
    assert mu.arrive_time == "10:45"
    assert mu.duration == "2h15m"
    assert mu.seats["经济舱"].count == 9
    assert mu.seats["经济舱"].available is True


def test_cross_day_arrival_is_flagged():
    """跨天到达要带 +1，否则通知里的时刻会让人误判。"""
    trains = parse_flight_offers(OFFERS)
    assert trains["CA1501"].arrive_time == "23:25+1"


def test_parse_flight_offers_prefers_more_seats():
    payload = {
        "data": [
            {
                "numberOfBookableSeats": 2,
                "itineraries": OFFERS["data"][0]["itineraries"],
            },
            {
                "numberOfBookableSeats": 7,
                "itineraries": OFFERS["data"][0]["itineraries"],
            },
        ]
    }
    trains = parse_flight_offers(payload)
    assert trains["MU5101"].seats["经济舱"].count == 7


def test_parse_flight_offers_handles_multisegment_connection():
    payload = {
        "data": [
            {
                "numberOfBookableSeats": 4,
                "itineraries": [
                    {
                        "duration": "PT6H",
                        "segments": [
                            {
                                "carrierCode": "MU",
                                "number": "5101",
                                "departure": {"iataCode": "PEK", "at": "2026-10-05T08:30:00"},
                                "arrival": {"iataCode": "CAN", "at": "2026-10-05T11:40:00"},
                            },
                            {
                                "carrierCode": "CZ",
                                "number": "3501",
                                "departure": {"iataCode": "CAN", "at": "2026-10-05T13:00:00"},
                                "arrival": {"iataCode": "SHA", "at": "2026-10-05T15:30:00"},
                            },
                        ],
                    }
                ],
            }
        ]
    }
    trains = parse_flight_offers(payload)
    assert list(trains) == ["MU5101+CZ3501"]
    assert trains["MU5101+CZ3501"].to_station == "SHA"


def test_travel_class_label_is_configurable():
    trains = parse_flight_offers(OFFERS, travel_class="BUSINESS")
    assert "公务舱" in trains["MU5101"].seats


def test_parse_reports_shape_when_data_missing():
    from radar.adapters.base import AdapterError

    with pytest.raises(AdapterError) as exc:
        parse_flight_offers({"errors": [{"code": "400"}]})
    assert "data" in str(exc.value)


def test_missing_seat_count_means_unknown_not_zero():
    payload = {
        "data": [
            {
                "itineraries": OFFERS["data"][0]["itineraries"],
            }
        ]
    }
    trains = parse_flight_offers(payload)
    seat = trains["MU5101"].seats["经济舱"]
    assert seat.count is None
    assert seat.available is True


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def _task(**params) -> TaskConfig:
    return TaskConfig(id="air1", adapter="amadeus", interval_seconds=600, params=params)


def _client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_amadeus_end_to_end_with_oauth():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "TOK", "expires_in": 1799})
        assert request.headers["authorization"] == "Bearer TOK"
        return httpx.Response(200, json=OFFERS)

    adapter = create_adapter(
        "amadeus", {"client_id": "id", "client_secret": "sec"}
    )
    # from 是 Python 关键字，params 里用字符串键构造
    task = _task(**{"from": "PEK", "to": "SHA", "date": "+14"})

    async def go():
        async with _client_with(handler) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())

    assert snapshot.platform == "amadeus"
    assert set(snapshot.trains) == {"MU5101", "CA1501"}
    assert len(seen) == 2
    body = dict(httpx.QueryParams(seen[0].content.decode()))
    assert body["grant_type"] == "client_credentials"


def test_amadeus_token_is_cached_across_calls():
    counter = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            counter["token"] += 1
            return httpx.Response(200, json={"access_token": "TOK", "expires_in": 1799})
        return httpx.Response(200, json=OFFERS)

    adapter = create_adapter("amadeus", {"client_id": "id", "client_secret": "sec"})
    task = _task(**{"from": "PEK", "to": "SHA"})

    async def go():
        async with _client_with(handler) as client:
            await adapter.fetch(task, client)
            await adapter.fetch(task, client)

    asyncio.run(go())
    assert counter["token"] == 1, "token 应该被缓存，不该每次请求都换"


def test_amadeus_retries_once_on_401():
    counter = {"offers": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            counter["token"] += 1
            return httpx.Response(200, json={"access_token": f"T{counter['token']}", "expires_in": 1799})
        counter["offers"] += 1
        if counter["offers"] == 1:
            return httpx.Response(401, json={"errors": [{"code": "401"}]})
        return httpx.Response(200, json=OFFERS)

    adapter = create_adapter("amadeus", {"client_id": "id", "client_secret": "sec"})
    task = _task(**{"from": "PEK", "to": "SHA"})

    async def go():
        async with _client_with(handler) as client:
            return await adapter.fetch(task, client)

    snapshot = asyncio.run(go())
    assert snapshot.trains
    assert counter["offers"] == 2
    assert counter["token"] == 2


def test_amadeus_missing_credentials_explains_signup():
    from radar.adapters.base import AdapterError

    adapter = create_adapter("amadeus", {})
    task = _task(**{"from": "PEK", "to": "SHA"})

    async def go():
        async with httpx.AsyncClient() as client:
            await adapter.fetch(task, client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    message = str(exc.value)
    assert "developers.amadeus.com" in message
    assert "AMADEUS_CLIENT_ID" in message


def test_amadeus_requires_origin_and_destination():
    from radar.adapters.base import AdapterError

    adapter = create_adapter("amadeus", {"client_id": "id", "client_secret": "sec"})

    async def go():
        async with httpx.AsyncClient() as client:
            await adapter.fetch(_task(**{"to": "SHA"}), client)

    with pytest.raises(AdapterError) as exc:
        asyncio.run(go())
    assert "params.from" in str(exc.value)


def test_amadeus_defaults_to_test_host_and_can_be_overridden():
    assert AmadeusAdapter({}).host == TEST_HOST
    assert (
        AmadeusAdapter({"host": "https://api.amadeus.com"}).host
        == "https://api.amadeus.com"
    )
    # 允许只写域名
    assert AmadeusAdapter({"host": "api.amadeus.com"}).host == "https://api.amadeus.com"
