"""``radar find`` 的测试。

这个命令存在的理由是「余票接口要的是平台内部 id，而站内搜索页是前端路由」。
所以它的价值全在两件事上：**把名字翻译成 id**，以及**在搜不到时给出正确的
下一步**（尤其摩天轮——二手平台搜不到是常态，不能被讲成配置错）。
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from radar import cli as cli_mod
from radar.cli import _find_cells, _find_id, _find_platform

# ---------------------------------------------------------------------------
# 平台解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["maoyan", "MAOYAN", "猫眼"])
def test_find_platform_accepts_english_and_chinese(raw):
    assert _find_platform(raw) == "maoyan"


def test_find_platform_accepts_moretickets_alias():
    assert _find_platform("摩天轮") == "moretickets"


@pytest.mark.parametrize("raw", ["damai", "12306", "大麦"])
def test_find_platform_rejects_platforms_that_need_no_translation(raw):
    """只给「需要 id 翻译」的平台开口子。大麦这类不该出现在这里。

    报错要说清支持哪些，而不是只说「不认识」。
    """
    with pytest.raises(typer.BadParameter) as exc:
        _find_platform(raw)
    assert "maoyan" in str(exc.value)


# ---------------------------------------------------------------------------
# 单元格渲染
# ---------------------------------------------------------------------------


def test_find_cells_for_maoyan_translates_status():
    """列表里的状态是数字，给人看必须翻成文案（复用适配器的官方词表）。"""
    cells = _find_cells(
        "maoyan",
        {
            "performanceId": 501675,
            "name": "陈粒「一粒」广州站",
            "cityName": "广州",
            "showTimeRange": "2026.10.18",
            "priceRange": "399-999",
            "ticketStatus": 3,
        },
    )
    assert cells[0] == "501675"
    assert cells[1] == "陈粒「一粒」广州站"
    assert cells[5] == "在售中"


def test_find_cells_for_maoyan_keeps_raw_status_when_unknown():
    """看不懂的状态原样显示，不吞掉——用户至少要能看到那个数字去反馈。"""
    cells = _find_cells("maoyan", {"performanceId": 1, "ticketStatus": 77})
    assert cells[5] == "77"


def test_find_cells_for_moretickets_reads_nested_price():
    cells = _find_cells(
        "moretickets",
        {
            "tourId": "6a2a300f941d1b00014a8828",
            "title": "周杰伦嘉年华",
            "location": "广州",
            "showDate": "2026-10-18",
            "status": "ONSALE",
            "price": {"minSalePrice": 1280},
        },
    )
    assert cells[0] == "6a2a300f941d1b00014a8828"
    assert cells[1] == "周杰伦嘉年华"
    assert cells[4] == "1280"
    assert cells[5] == "ONSALE"


def test_find_cells_tolerate_missing_price_dict():
    cells = _find_cells("moretickets", {"tourId": "t1", "title": "x"})
    assert cells[4] == ""


def test_find_id_picks_the_right_primary_key():
    assert _find_id("maoyan", {"performanceId": 9}) == "9"
    assert _find_id("moretickets", {"tourId": "t9"}) == "t9"


# ---------------------------------------------------------------------------
# 端到端（假搜索，不联网）
# ---------------------------------------------------------------------------


def _patch_maoyan_search(monkeypatch, rows):
    async def fake(client, keyword, *, size=20):  # noqa: ARG001
        return rows

    monkeypatch.setattr("radar.adapters.maoyan.search_performances", fake)
    return fake


def test_find_prints_the_id_to_copy(monkeypatch):
    _patch_maoyan_search(
        monkeypatch,
        [
            {
                "performanceId": 501675,
                "name": "陈粒「一粒」广州站",
                "cityName": "广州",
                "showTimeRange": "2026.10.18",
                "priceRange": "399-999",
                "ticketStatus": 3,
            }
        ],
    )

    result = CliRunner().invoke(cli_mod.app, ["find", "maoyan", "陈粒"])

    assert result.exit_code == 0, result.output
    assert "501675" in result.output
    # 结尾必须直接给出可粘贴的配置片段，而不是让用户自己拼
    assert "performance_id" in result.output
    assert "在售中" in result.output


def test_find_json_mode_is_machine_readable(monkeypatch):
    _patch_maoyan_search(monkeypatch, [{"performanceId": 501675, "name": "陈粒"}])

    result = CliRunner().invoke(cli_mod.app, ["find", "maoyan", "陈粒", "--json"])

    assert result.exit_code == 0, result.output
    assert '"performanceId": 501675' in result.output
    assert '"platform": "maoyan"' in result.output


def test_find_city_filter_narrows_candidates(monkeypatch):
    _patch_maoyan_search(
        monkeypatch,
        [
            {"performanceId": 1, "name": "北京站", "cityName": "北京"},
            {"performanceId": 2, "name": "广州站", "cityName": "广州"},
        ],
    )

    result = CliRunner().invoke(
        cli_mod.app, ["find", "maoyan", "陈粒", "--city", "广州", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert '"performanceId": 2' in result.output
    assert '"performanceId": 1' not in result.output


def test_find_with_no_hits_exits_nonzero_with_a_reason(monkeypatch):
    _patch_maoyan_search(monkeypatch, [])

    result = CliRunner().invoke(cli_mod.app, ["find", "maoyan", "查无此演出"])

    assert result.exit_code == 1
    assert "没搜到" in result.output


def test_find_moretickets_miss_explains_secondhand_nature(monkeypatch):
    """摩天轮搜不到时不能只报「没搜到」——用户会以为是自己配错了。"""

    async def fake(client, keyword, *, length=20):  # noqa: ARG001
        return []

    monkeypatch.setattr("radar.adapters.moretickets.search_tours", fake)

    result = CliRunner().invoke(cli_mod.app, ["find", "moretickets", "冷门"])

    assert result.exit_code == 1
    assert "二手" in result.output


def test_find_unknown_platform_fails_fast():
    result = CliRunner().invoke(cli_mod.app, ["find", "damai", "陈粒"])

    assert result.exit_code != 0
