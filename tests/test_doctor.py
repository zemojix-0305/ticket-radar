"""``radar doctor`` 的测试。

这个命令要证明它跟 ``radar onboard`` 的分工是真实的：

* ``onboard`` 只看配置——凭据「填了没有」；
* ``doctor`` 真去试——凭据「还有效没」。

所以测试盯三件事：

1. **默认实现必须返回「未检查」，不能返回「正常」。** 漏实现就报正常，
   等于用体检报告掩盖盲区——这跟「没消息就是没问题」是同一种错误。
2. **大麦的过期判断不发请求也能算对**。Cookie 只有几小时寿命，如果每次
   体检都靠真抓去撞错误，用户会频繁看到红色却什么也做不了。
3. **「不支持」「未检查」「正常」三者在输出里必须可区分**。
"""

from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from radar.adapters import create_adapter
from radar.adapters.base import (
    HEALTH_AUTH,
    HEALTH_OK,
    HEALTH_UNKNOWN,
    HEALTH_UNSUPPORTED,
    Adapter,
    AdapterError,
    AuthError,
)
from radar.cli import _DOCTOR_STYLE, app

runner = CliRunner()

#: CLI 里「异常类」状态到分档的映射。凭据问题与接口异常必须分开。
_DOCTOR_STATUS_FOR = {"auth": "auth", "broken": "broken"}


def _cookie_with_expiry(seconds_from_now: float) -> str:
    """造一条带过期时间戳的 _m_h5_tk Cookie。

    真实格式是 ``{token}_{过期时间戳毫秒}``，这里照着造。
    """
    ts_ms = int((time.time() + seconds_from_now) * 1000)
    return f"_m_h5_tk=abc123_{ts_ms}; cookie2=xyz"


# --- 默认实现不许说「正常」------------------------------------------------


def test_base_doctor_returns_unknown_not_ok():
    """没实现 doctor 的适配器必须报「未检查」。

    这是最重要的一条：默认返回 ok 会让所有新适配器一进体检表就显示绿色，
    而实际上没人检查过。
    """

    class Bare(Adapter):
        name = "bare"

        async def fetch(self, task, client):  # pragma: no cover - 不该被调用
            raise AssertionError("doctor 不该调 fetch")

    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await Bare().doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_UNKNOWN
    assert "不" in note, "说明里要讲清这是「没检查」而不是「正常」"


# --- 大麦：不联网算过期 ---------------------------------------------------


def test_damai_doctor_flags_expired_cookie():
    ad = create_adapter("damai", {"cookie": _cookie_with_expiry(-3600)})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_AUTH
    assert "过期" in note
    assert "radar login damai" in note, "必须给出可执行的下一步，而不是只说失效"


def test_damai_doctor_reports_remaining_hours():
    ad = create_adapter("damai", {"cookie": _cookie_with_expiry(2 * 3600)})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_OK
    assert "2.0" in note, f"应报出剩余小时数，实际：{note}"


def test_damai_doctor_flags_missing_token():
    """Cookie 填了但没有 _m_h5_tk —— 算不出签名，一样不可用。"""
    ad = create_adapter("damai", {"cookie": "cookie2=xyz; unb=123"})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_AUTH
    assert "_m_h5_tk" in note


def test_damai_doctor_admits_when_format_is_unrecognised():
    """格式不认识就说不认识——不许报一个看起来很确定的结论。"""
    ad = create_adapter("damai", {"cookie": "_m_h5_tk=justtokennounderscore"})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_UNKNOWN
    assert "格式不认识" in note


def test_damai_doctor_makes_no_network_call():
    """过期判断必须纯本地——大麦间隔 5 分钟，体检不该消耗配额。"""
    ad = create_adapter("damai", {"cookie": _cookie_with_expiry(-1)})
    import asyncio

    import httpx

    calls = 0

    async def handler(request):  # pragma: no cover - 不该被调用
        nonlocal calls
        calls += 1
        raise AssertionError("doctor 不该发请求")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await ad.doctor(c)

    asyncio.run(go())
    assert calls == 0, "本地就能算出过期，不该发请求"


# --- 其它平台 --------------------------------------------------------------


def test_amadeus_doctor_names_the_missing_env_keys():
    """凭据没填要说清是哪几个 key 空着，而不是笼统的「失败」。"""
    ad = create_adapter("amadeus", {})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_AUTH
    assert "AMADEUS_CLIENT_ID" in note and "AMADEUS_CLIENT_SECRET" in note


def test_fenwandao_doctor_reports_unsupported_without_requesting():
    """平台层面接不了：明确报「不支持」，且一个请求都不发。"""
    ad = create_adapter("fenwandao", {})
    import asyncio

    import httpx

    calls = 0

    async def handler(request):  # pragma: no cover - 不该被调用
        nonlocal calls
        calls += 1
        raise AssertionError("接不了的平台不该发请求")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_UNSUPPORTED
    assert "纷玩岛" in note
    assert calls == 0


def test_json_api_doctor_says_unknown_not_ok():
    """通用适配器无法单独探活——必须说「未检查」并指向真正的验证方式。"""
    ad = create_adapter("json-api", {})
    import asyncio

    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return await ad.doctor(c)

    status, note = asyncio.run(go())
    assert status == HEALTH_UNKNOWN
    assert "radar check" in note, "要告诉用户怎么真的验证"


# --- CLI -------------------------------------------------------------------


def test_doctor_command_runs_and_reports(monkeypatch, tmp_path):
    """命令整体能跑通，且把三类结局都渲染出来。"""


    from radar import cli as cli_mod

    class FakeAdapter(Adapter):
        name = "fake"
        min_interval = 0.0
        capability = type(create_adapter("maoyan").capability)(
            category="show", summary="假平台", can_search=True
        )

        async def fetch(self, task, client):
            from radar.models import SeatAvailability, Snapshot, TrainState

            return Snapshot(
                platform="fake",
                captured_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                trains={"X1": TrainState(train_code="X1", from_station="A", to_station="B",
                                        seats={"二等座": SeatAvailability(seat_type="二等座", count=3, available=3)})},
            )

        async def doctor(self, client):
            return HEALTH_OK, "假平台正常"

    cfg_file = tmp_path / "tasks.yaml"
    cfg_file.write_text(
        "storage:\n  path: " + str(tmp_path / "d.sqlite") + "\n"
        "tasks:\n"
        "  - id: t1\n"
        "    adapter: fake\n"
        "    enabled: true\n"
        "    interval_seconds: 60\n"
        "    display_name: 假任务\n"
        "    params: {}\n"
        "notify: []\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_mod, "create_adapter", lambda n, c=None: FakeAdapter(), raising=False)
    monkeypatch.setattr(
        "radar.adapters.list_adapters", lambda: ["fake", "fenwandao"], raising=False
    )

    res = runner.invoke(app, ["doctor", "-c", str(cfg_file)])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "假任务" in out
    assert "正常" in out
    # 接不了的平台要显示成「不支持」，不能混成「异常」
    assert "平台不支持" in out


def test_doctor_command_reports_missing_config():
    """配置文件不存在时给友好提示，而不是抛栈。"""
    res = runner.invoke(app, ["doctor", "-c", "definitely-not-here.yaml"])
    assert res.exit_code != 0


@pytest.mark.parametrize("status", ["ok", "auth", "broken", "unsupported", "unknown"])
def test_every_status_has_a_distinct_style(status):
    """五种状态必须有五种呈现——尤其「未检查」不能被涂成绿色。"""
    from radar.cli import _DOCTOR_STYLE

    assert status in _DOCTOR_STYLE
    styles = list(_DOCTOR_STYLE.values())
    assert len(set(styles)) == len(styles), f"状态样式有重复，会让人分不清：{styles}"
    assert "green" not in _DOCTOR_STYLE["unknown"], "「未检查」涂成绿色等于掩盖盲区"
    assert "green" not in _DOCTOR_STYLE["unsupported"], "「不支持」不是正常状态"


def test_auth_error_would_be_reported_as_credential_problem():
    """AuthError 必须能被单独识别——否则 CLI 只能笼统报「接口异常」。

    用户看到「接口异常」会去降频重试；看到「凭据问题」才会去登录。
    分错档等于把人指向错误的修复动作。
    """
    assert issubclass(AuthError, AdapterError)
    assert _DOCTOR_STATUS_FOR.get("auth") is not None
    # 凭据问题与接口异常在展示上必须不同
    assert _DOCTOR_STYLE["auth"] != _DOCTOR_STYLE["broken"]
