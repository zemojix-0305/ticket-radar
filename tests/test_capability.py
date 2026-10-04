"""需求路由的测试。

重点不在「路由对不对」，而在两件容易退化的事：

1. **每个平台都声明了能力**。新加适配器时忘了写 ``capability`` 是
   最典型的退化——路由会静默地把它排除掉，用户永远看不到那个平台。
   这里用一条守卫测试钉死。
2. **做不到时必须说得出话**。``RouteResult.explain()`` 在无候选时
   不能返回空，也不能只说「不支持」，得把每个平台为什么不行摆出来。
"""

from __future__ import annotations

import pytest

from radar.adapters import get_adapter_class, list_adapters
from radar.adapters.base import Capability
from radar.capability import (
    CATEGORIES,
    FLIGHT,
    SHOW,
    TRAIN,
    Requirement,
    capabilities,
    category_label,
    route,
    supported_platforms,
)


def _req(**kw) -> Requirement:
    return Requirement(**kw)


# --- 守卫：声明完整性 ------------------------------------------------------


def test_every_registered_adapter_declares_capability():
    """每个注册适配器都必须有 capability —— 漏了就会被路由静默排除。"""
    missing = [n for n in list_adapters() if not isinstance(getattr(get_adapter_class(n), "capability", None), Capability)]
    assert not missing, f"这些适配器没声明 capability，路由会静默排除它们：{missing}"


def test_every_capability_uses_known_category():
    """类别必须是已知常量之一，否则路由永远匹配不上。"""
    bad = {
        name: cap.category
        for name, cap in capabilities().items()
        if cap.category not in CATEGORIES
    }
    assert not bad, f"未知类别（路由会永远排除）：{bad}"


def test_every_capability_states_its_limits():
    """limitation 不能空着——「做不到但说清」全靠它。

    空 limitation 意味着这个平台遇到搞不定的需求时说不出理由，
    又变回同类项目的「沉默」。
    """
    silent = [name for name, cap in capabilities().items() if not cap.limitation.strip()]
    assert not silent, f"这些平台没写 limitation，做不到时说不出原因：{silent}"


# --- 路由：能办到的时候 ----------------------------------------------------


def test_show_with_keyword_excludes_damai():
    """用户只给了关键词 → 不会搜索的大麦必须出局，并说明原因。"""
    result = route(_req(category=SHOW, need_search=True, region="CN", text="周杰伦深圳场"))
    names = [c.platform for c in result.candidates]
    assert "maoyan" in names and "moretickets" in names
    assert "damai" not in names

    damai_rej = next(r for r in result.rejections if r.platform == "damai")
    assert "搜索" in damai_rej.reason


def test_show_with_id_keeps_damai():
    """用户自己有场次 ID → 大麦不该被「不会搜索」这条排除。"""
    result = route(_req(category=SHOW, need_search=False, region="CN"))
    assert "damai" in [c.platform for c in result.candidates]


def test_region_filters_platforms():
    """只覆盖国内的平台不该出现在境外需求里。"""
    result = route(_req(category=SHOW, need_search=True, region="JP"))
    assert [c.platform for c in result.candidates] == ["moretickets"]
    maoyan_rej = next(r for r in result.rejections if r.platform == "maoyan")
    assert "JP" in maoyan_rej.reason


def test_train_routes_to_rail12306():
    result = route(_req(category=TRAIN, need_search=True, region="CN"))
    assert [c.platform for c in result.candidates] == ["rail12306"]


def test_platforms_without_login_rank_first():
    """不需要登录的排前面——登录是最大的上手障碍。"""
    result = route(_req(category=SHOW, need_search=False, region="CN"))
    assert [c.platform for c in result.candidates][:2] == ["maoyan", "moretickets"]
    assert result.candidates[-1].platform == "damai"


def test_unsupported_platform_never_appears():
    """接不了的平台（纷玩岛）任何时候都不能进候选。"""
    for category in CATEGORIES:
        result = route(_req(category=category, need_search=False))
        assert "fenwandao" not in [c.platform for c in result.candidates]


# --- 路由：办不到的时候（本项目最在意的部分）------------------------------


def test_unknown_category_has_no_candidate_but_full_reasons():
    """没有任何平台能处理时，必须把**每个**平台为什么不行说清楚。"""
    result = route(_req(category="hotel", need_search=True, text="盯酒店空房"))

    assert result.candidates == ()
    assert result.outcome == "unsupported"

    text = result.explain()
    assert "酒店空房" in text, "回显用户原话，让他知道是哪条需求"
    # 每个注册平台都得有一条理由，不能只挑几个说
    for name in list_adapters():
        assert name in text, f"拒绝了却没说 {name} 为什么不行"


def test_explain_never_empty():
    """explain() 在任何需求下都不能返回空——「做不到」也要有话可说。"""
    for category in (*CATEGORIES, "hotel", "不存在的类别"):
        for region in ("", "CN", "XX"):
            text = route(_req(category=category, region=region)).explain()
            assert text.strip(), f"category={category} region={region} 时 explain() 是空的"


def test_flight_needs_login_is_flagged():
    """只有需要登录的候选时，结局要标 need_login，而不是笼统的 ok。"""
    result = route(_req(category=FLIGHT, need_search=True))
    assert result.outcome == "need_login"
    assert "登录" in result.explain()


def test_supported_platforms_excludes_unsupported():
    """supported_platforms 用于对外声明能力，不能把接不了的列进去。"""
    assert "fenwandao" not in supported_platforms()
    assert "fenwandao" not in supported_platforms(SHOW)
    assert "rail12306" in supported_platforms(TRAIN)


# --- 小工具 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [(TRAIN, "交通票务"), (SHOW, "演出票务"), (FLIGHT, "航班")],
)
def test_category_label(raw: str, want: str):
    assert category_label(raw) == want


def test_category_label_unknown_returns_raw():
    """未知类别原样返回，不瞎猜——猜错了比不猜更糟。"""
    assert category_label("hotel") == "hotel"


def test_capability_available_matches_status():
    assert Capability(status="healthy").available() is True
    assert Capability(status="degraded").available() is True
    assert Capability(status="unsupported").available() is False
