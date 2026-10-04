"""接入引导的测试。

两个重点：

1. **示例配置必须真的能加载**——引导里给用户的 YAML 片段如果自己都跑不起来，
   那这份引导就是在骗人。所以这里把它写进临时文件，走一遍真实的
   :func:`load_config`。
2. **引导文案不能回显凭据**——状态是用在终端和日志里的，一旦把 Cookie 值
   带进去就等于泄露登录态。只允许报长度。
"""

from __future__ import annotations

import pytest

from radar.config import load_config
from radar.onboarding import (
    EMPTY,
    INCOMPLETE,
    OK,
    PLANNED,
    TOO_SHORT,
    check_credential,
    iter_guides,
    resolve,
)


def _damai():
    guide = resolve("damai")
    assert guide is not None
    return guide


# ---------------------------------------------------------------------------
# 解析平台名
# ---------------------------------------------------------------------------


def test_resolve_accepts_key_case_and_chinese_alias():
    assert resolve("damai").key == "damai"
    assert resolve("DAMAI").key == "damai"
    assert resolve("  大麦 ").key == "damai"
    assert resolve("猫眼").key == "maoyan"
    assert resolve("摩天轮").key == "moretickets"


def test_resolve_returns_none_for_unknown_or_empty():
    assert resolve("weibo") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_guides_cover_all_four_show_platforms_plus_jwc():
    keys = [g.key for g in iter_guides()]
    for expected in ("damai", "maoyan", "moretickets", "fenwandao", "jwc"):
        assert expected in keys, f"缺少 {expected} 的接入引导"


# ---------------------------------------------------------------------------
# 凭据体检
# ---------------------------------------------------------------------------


def test_status_empty_when_credential_missing():
    status = check_credential(_damai(), {})
    assert status.level == EMPTY
    assert not status.ok
    assert "DAMAI_COOKIE" in status.hint


def test_status_too_short_when_only_a_fragment():
    status = check_credential(_damai(), {"DAMAI_COOKIE": "abc"})
    assert status.level == TOO_SHORT
    assert not status.ok


def test_status_incomplete_when_required_key_absent():
    status = check_credential(_damai(), {"DAMAI_COOKIE": "cookie2=abcdefghijklmnopqrstuvwxyz"})
    assert status.level == INCOMPLETE
    assert "_m_h5_tk" in status.headline


def test_status_ok_when_required_key_present():
    raw = "_m_h5_tk=8f3a1c9b2d4e_1759123456789; cookie2=abcdef123456"
    status = check_credential(_damai(), {"DAMAI_COOKIE": raw})
    assert status.level == OK
    assert status.ok


def test_status_never_echoes_credential_value():
    raw = "_m_h5_tk=SECRETVALUE_1759123456789; cookie2=othersecretvalue"
    status = check_credential(_damai(), {"DAMAI_COOKIE": raw})
    assert "SECRETVALUE" not in status.headline
    assert "SECRETVALUE" not in status.hint
    assert "othersecretvalue" not in status.headline


def test_planned_guide_is_flagged_not_upgraded_to_ok():
    """教务系统还没实现，不能因为「填了用户名」就显示成可用。"""
    status = check_credential(resolve("jwc"), {"JWC_USERNAME": "someone"})
    assert status.level == PLANNED
    assert not status.ok


# ---------------------------------------------------------------------------
# 示例配置必须真能用
# ---------------------------------------------------------------------------

_HEADER = """\
storage:
  path: ./data/radar.db
notify:
  - type: ntfy
    enabled: true
    options:
      topic: test-topic
credentials:
  damai: {cookie: "placeholder"}
  maoyan: {cookie: "placeholder"}
  moretickets: {cookie: "placeholder"}
  fenwandao: {cookie: "placeholder"}
"""

_GUIDES_WITH_SAMPLE = [g for g in iter_guides() if g.sample_task]


@pytest.mark.parametrize("guide", _GUIDES_WITH_SAMPLE, ids=lambda g: g.key)
def test_sample_task_loads_as_real_config(guide, tmp_path):
    path = tmp_path / "tasks.yaml"
    path.write_text(_HEADER + "tasks:\n" + guide.sample_task + "\n", encoding="utf-8")

    config = load_config(path)

    assert len(config.tasks) == 1, "示例片段应该正好是一个任务"
    assert config.tasks[0].adapter == guide.adapter


def test_every_unplanned_guide_has_actionable_steps():
    for guide in iter_guides():
        if guide.planned:
            continue
        # 公开接口的平台（实测匿名可用）没有「取 Cookie」这一步。
        # 硬要求它有步骤，等于逼着后人写一段假引导。
        if guide.no_credentials:
            assert not guide.cookie_steps, f"{guide.key} 不需要登录，不该有取 Cookie 的步骤"
        else:
            assert guide.cookie_steps, f"{guide.key} 没写取 Cookie 的步骤"
        assert guide.api_steps, f"{guide.key} 没写找接口地址的步骤"
        assert guide.env_key == guide.env_key.upper(), "环境变量名必须是大写"


def test_public_platforms_are_marked_no_credentials():
    """猫眼和摩天轮的接口是公开的（2026-09-30 实测）。标错会让用户白折腾一轮。"""
    from radar.onboarding import resolve

    for key in ("maoyan", "moretickets"):
        guide = resolve(key)
        assert guide.no_credentials is True, f"{key} 应该是公开接口"


def test_no_credentials_guide_reports_not_needed_not_empty():
    """体检结果不能显示「未填写」——那会让人去填一个不需要的东西。"""
    from radar.onboarding import NOT_NEEDED, check_credential, resolve

    status = check_credential(resolve("maoyan"), {})
    assert status.level == NOT_NEEDED
    assert "无需登录" in status.headline
    assert status.ok  # 绿色，不是黄/红


# ---------------------------------------------------------------------------
# 「推荐登录方式」必须是实地验证过的，且不得不说
# ---------------------------------------------------------------------------


def test_damai_guide_tells_user_to_use_qr_login():
    """大麦的图形验证码是用户反馈过的最痛点。

    扫码登录能完全绕开它，所以这条建议不能从引导里悄悄消失——
    删掉它，下一个用户还会卡在同一个地方。
    """
    guide = _damai()
    assert guide.login_hint, "大麦必须给出推荐登录方式"
    assert "扫码" in guide.login_hint
    assert any("扫码" in step for step in guide.cookie_steps), (
        "手工取 Cookie 的步骤里也要提扫码，否则用户照做还是会撞上验证码"
    )


def test_guides_never_promise_what_we_will_not_do():
    """引导文案里不能出现「处理验证码」这类与合规红线冲突的暗示。"""
    forbidden = ("自动识别验证码", "破解验证码", "绕过风控", "伪装设备")
    for guide in iter_guides():
        blob = " ".join(
            (guide.what_you_get, guide.login_hint, *guide.cookie_steps,
             *guide.api_steps, *guide.caveats)
        )
        for word in forbidden:
            assert word not in blob, f"{guide.key} 的文案里出现了红线词：{word}"


# ---------------------------------------------------------------------------
# 接不了的平台：要说真话，而不是留个死胡同
# ---------------------------------------------------------------------------


def test_fenwandao_is_blocked_by_the_platform_not_by_our_todo():
    """纷玩岛接不了的原因是「它没有网页端」，不是「我们还没写」。

    这个区别对用户是有意义的：前者别等了，后者可以等。所以不能
    偷懒地把它标成「规划中」了事。
    """
    guide = resolve("fenwandao")
    assert guide is not None, "中文别名 纷玩岛 仍要能解析出来，好让 onboard 能解释原因"
    assert guide.planned
    assert "网页" in guide.blocked_reason, "说清楚是哪一类「不能用」"
    assert not guide.login_url, "没有网页端可登，就不该给出登录地址"
    assert not guide.cookie_steps and not guide.api_steps, "没有网页流程，就别写步骤"
    assert not guide.sample_task, "没有能跑起来的示例，就不要给示例"


def test_blocked_platform_is_never_reported_as_usable():
    """哪怕用户硬填了 Cookie，也不能显示「已填写」让他以为能用了。"""
    guide = resolve("fenwandao")
    status = check_credential(guide, {"FENWANDAO_COOKIE": "x" * 400})
    assert status.level == PLANNED
    assert not status.ok
    assert "网页" in status.headline


def test_unavailable_reason_is_explained_in_the_guide():
    """onboard 里那一段说明必须真的存在，否则拒绝用户时就无话可说。"""
    guide = resolve("fenwandao")
    assert len(guide.caveats) >= 3
    joined = " ".join(guide.caveats)
    assert "小程序" in joined, "要讲清楚票到底卖在哪"
    assert "接不了" in joined, "要给出明确结论，别让用户猜"


def test_no_guide_is_a_silent_dead_end():
    """每个平台要么能登录，要么有 blocked_reason——不允许两者都没有。"""
    for guide in iter_guides():
        if guide.planned:
            assert guide.blocked_reason or guide.key == "jwc", (
                f"{guide.key} 标了不可用却没说明原因"
            )
        else:
            assert guide.login_url or guide.cookie_steps, (
                f"{guide.key} 既不能自动登录，又没写手工步骤"
            )


# ---------------------------------------------------------------------------
# CLI：拒绝的时候要讲理由
# ---------------------------------------------------------------------------


def test_login_command_refuses_blocked_platform_with_a_reason(tmp_path):
    """「不支持」这三个字最没用。要告诉用户为什么、以及该去用哪个。"""
    from typer.testing import CliRunner

    from radar.cli import app

    result = CliRunner().invoke(app, ["login", "fenwandao"])

    assert result.exit_code == 1
    assert "没法自动登录" in result.output
    assert "小程序" in result.output, "拒绝时要给出证据，不是一句「不支持」"
    assert "damai" in result.output, "顺便指条明路"
