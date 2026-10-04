"""意图解析的测试。

这层的价值全在**听懂人话**：用户嘴上怎么说我都得能听懂。所以测试重点
是那些真实口语，而不是规整的输入。

两个原则被反复验证：

* **解析不了要明说**，不能瞎猜（``notes`` 里必须出现提示）；
* **解析不联网**——它只做翻译，验证留给下一层用真实接口抓。
"""

from __future__ import annotations

import pytest

from radar.capability import FLIGHT, SHOW, TRAIN
from radar.intent import _cn_to_int, parse_intent

# --- 链接 -------------------------------------------------------------------


def test_parses_damai_share_link():
    p = parse_intent("https://detail.damai.cn/item.htm?id=1076797433026")
    assert p.platform == "damai"
    assert p.target_id == "1076797433026"
    assert p.id_key == "item_id"
    assert p.category == SHOW
    assert p.needs_search is False


def test_parses_maoyan_hash_route_link():
    """猫眼 App 分享链接是 hash 路由：``/qqw#/detail/174231``。

    id 在 ``#`` 之后——按 ``/(\\d+)`` 那种常规路径去抠会拿到 0。这是实测
    踩过的，所以单独钉一条。
    """
    p = parse_intent("https://show.maoyan.com/qqw#/detail/174231?fromTag=myshare")
    assert p.platform == "maoyan"
    assert p.target_id == "174231"
    assert p.id_key == "performance_id"


def test_parses_link_with_trailing_prose():
    """用户常常在链接后面补一句（「这场挺便宜」）。整段也要能解析。"""
    p = parse_intent("盯一下 https://show.maoyan.com/qqw#/detail/174231 这场")
    assert p.platform == "maoyan"
    assert p.target_id == "174231"


def test_unknown_domain_is_reported_not_guessed():
    """不支持的链接要明说，不能瞎猜是哪个平台。"""
    p = parse_intent("https://example.com/show/12345")
    assert p.platform == ""
    assert any("链接" in n for n in p.notes)


# --- 日期 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("明天", "+1"),
        ("后天", "+2"),
        ("大后天", "+3"),
        ("今天", "today"),
        ("10月10日", "10-10"),
        ("11月5号", "11-05"),
        ("+7", "+7"),
    ],
)
def test_parses_dates(text: str, want: str):
    assert parse_intent(f"盯{text}的票").date == want


def test_parses_chinese_numeral_date():
    """口语里说「十月十号」远比说「10-10」常见。"""
    assert parse_intent("帮我盯一下十月十号陈粒深圳场那张票").date == "10-10"


@pytest.mark.parametrize(
    ("cn", "want"),
    [("十", 10), ("十五", 15), ("二十", 20), ("二十一", 21), ("三十一", 31), ("五", 5)],
)
def test_chinese_numeral_conversion(cn: str, want: int):
    assert _cn_to_int(cn) == want


def test_chinese_numeral_rejects_out_of_range():
    """认不出来的数字返回 0，而不是猜一个。由调用方当「不知道」处理。"""
    assert _cn_to_int("二零三零") == 0


def test_impossible_date_is_reported_not_guessed():
    """13 月这种要提示，不能默默当成 1 月。"""
    p = parse_intent("13月40日的演出")
    assert p.date == ""
    assert any("日期" in n for n in p.notes)


# --- 铁路线路 ---------------------------------------------------------------


def test_parses_simple_route():
    p = parse_intent("广州到长沙南的票")
    assert (p.route_from, p.route_to) == ("广州", "长沙南")
    assert p.category == TRAIN


def test_route_keeps_station_direction_suffix():
    """「广州南」里的「南」是站名的一部分，去掉就变成两个不存在的站。"""
    p = parse_intent("广州南到长沙南上午的高铁")
    assert (p.route_from, p.route_to) == ("广州南", "长沙南")
    assert p.platform == "rail12306"


def test_route_words_do_not_leak_into_keyword():
    """识别出线路后，「到」「南」这类碎片不该混进搜索关键词。"""
    p = parse_intent("广州南到长沙南上午的高铁")
    assert p.keyword == "", f"碎片漏进了关键词：{p.keyword!r}"


def test_train_needs_no_keyword():
    """铁路靠线路定位，不需要搜索词。留着碎片只会让搜索变脏。"""
    assert parse_intent("订北京到上海的机票").keyword == ""


# --- 类别推断 ---------------------------------------------------------------


def test_explicit_platform_beats_route_inference():
    """说了「机票」就判航班，不能因为句子里有「北京到上海」判成火车。"""
    p = parse_intent("订北京到上海的机票")
    assert p.category == FLIGHT
    assert (p.route_from, p.route_to) == ("北京", "上海")


def test_show_hint_works_without_platform_name():
    """句子里没提平台名，但「演唱会」足以判定是演出。

    实测踩过：用户说「陈粒深圳场那张票」，句子里没有「猫眼」「大麦」，
    类别退回 generic，路由把**所有**平台都拒了——而用户明明在问演出。
    """
    assert parse_intent("周杰伦演唱会").category == SHOW


def test_unknown_category_does_not_block_user():
    """判不出类别时，需求描述的 category 要是空串（= 不限）。

    判不出类别不等于什么都干不了。把用户挡在门外是最差的结果。
    """
    p = parse_intent("随便看看")
    assert p.requirement().category == ""
    assert p.notes, "判不出类别要提示，不能默默接受"


# --- 关键词清洗 -------------------------------------------------------------


def test_strips_polite_and_verb_noise():
    """「帮我盯一下…那张票」里真正要搜的只有「陈粒」。"""
    p = parse_intent("帮我盯一下十月十号陈粒深圳场那张票")
    assert p.keyword == "陈粒"


def test_strips_platform_word_from_keyword():
    """平台名不进搜索词——搜「大麦 陈粒」和搜「陈粒」结果不同。"""
    p = parse_intent("大麦上有陈粒的演出吗")
    assert p.platform == "damai"
    assert p.keyword == "陈粒"


def test_single_char_fragment_is_discarded():
    """停用词是替换式的，会留下「午」这种碎片。宁可说没关键词。"""
    p = parse_intent("广州南到长沙南上午的高铁")
    assert p.keyword == ""


# --- 不装懂 -----------------------------------------------------------------


def test_empty_input_is_reported():
    p = parse_intent("   ")
    assert p.notes
    assert p.confidence == 0.0


def test_platform_without_target_says_what_is_missing():
    p = parse_intent("大麦上有陈粒的演出吗")
    assert p.target_id == ""
    assert p.needs_search is True
    # 认出了平台、有关键词，就不该再抱怨「没找到要盯什么」
    assert not any("没找到要盯什么" in n for n in p.notes)


def test_confidence_rises_with_evidence():
    """置信度要随证据增加：只有链接 > 链接加平台。"""
    only_text = parse_intent("随便看看").confidence
    with_link = parse_intent("https://detail.damai.cn/item.htm?id=1076797433026").confidence
    assert only_text < with_link
