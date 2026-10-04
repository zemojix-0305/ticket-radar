"""票价的币种：一个小字段，一个说错就误导人的坑。

背景：摩天轮会给境外场次返回 ``HK$``。模型里 ``price`` 只是一个 float，
``price_text`` 曾硬编码 ``¥``——于是 3499 港币会被渲染成「¥3499」。
这不是显示不美观，是**报了一个错误的价格**，用户可能据此下单。

所以币种必须跟着数值一起走：从适配器读到、落库、再渲染出来。
"""

from __future__ import annotations

from radar.models import SeatAvailability


def _seat(**kwargs) -> SeatAvailability:
    base = {"seat_type": "二等座", "raw": "有", "count": 5, "available": True}
    base.update(kwargs)
    return SeatAvailability(**base)


def test_default_currency_is_renminbi():
    """12306 和国内演出票都不带币种字段，默认必须是 ¥，不能留空。"""
    assert _seat(price=661).price_text == "¥661"


def test_price_text_uses_the_given_currency():
    assert _seat(price=3499, currency="HK$").price_text == "HK$3499"


def test_integer_price_has_no_trailing_zero():
    """:g 的用意：661.0 要显示成 661，不是 661.0。"""
    assert _seat(price=661.0).price_text == "¥661"


def test_no_price_yields_empty_text():
    """查不到票价返回空串，而不是「¥0」——0 元是另一件事。"""
    assert _seat(price=None).price_text == ""
    assert _seat(price=None, currency="HK$").price_text == ""


def test_label_includes_currency_aware_price():
    assert _seat(price=3499, currency="HK$").label == "二等座 5 张 HK$3499"


def test_currency_survives_payload_round_trip():
    seat = _seat(price=3499, currency="HK$")
    restored = SeatAvailability.from_payload(seat.to_payload())
    assert restored.currency == "HK$"
    assert restored.price_text == "HK$3499"


def test_old_payloads_without_currency_fall_back_to_renminbi():
    """老库里存的快照没有 currency 键，读出来不能炸，也不能变成空符号。"""
    legacy = {
        "seat_type": "二等座",
        "raw": "有",
        "count": 5,
        "available": True,
        "price": 553.0,
    }
    restored = SeatAvailability.from_payload(legacy)
    assert restored.currency == "¥"
    assert restored.price_text == "¥553"
