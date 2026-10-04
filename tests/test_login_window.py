"""登录窗口的测试——**这次修复的核心是「登录不许用自动化浏览器」**。

背景（用户实际踩到的坑）：
滑块提示「位置不对」，但用户明明划到了指定位置。原因是自动化浏览器能被
页面认出来（``navigator.webdriver`` / CDP 痕迹），于是怎么划都判错。

修法不是「划得更准」（那是在骗风控，本项目明确不做），而是**登录窗口压根
不带自动化参数**：用 ``subprocess`` 拉起一个普通 Edge 让人登录，登录完再用
无头浏览器把 Cookie 取回来。

所以这个文件里最要紧的那条测试是
:func:`test_cli_login_does_not_open_an_automated_window`——只要有人把登录
改回自动化窗口，它就会红。
"""

from __future__ import annotations

import threading

import pytest

import radar.browser as browser_mod
from radar.browser import (
    AUTOMATION_FLAGS,
    BrowserUnavailable,
    edge_argv,
    find_edge,
    launch_manual_browser,
    wait_for_browser_exit,
)

# ---------------------------------------------------------------------------
# 命令行：不许带自动化参数
# ---------------------------------------------------------------------------


def test_login_window_carries_no_automation_flag():
    """命令行里出现任何一个自动化开关，滑块就又有理由判我们错。"""
    argv = edge_argv("msedge.exe", "C:/profile", "https://www.damai.cn/")

    for flag in AUTOMATION_FLAGS:
        assert not any(a.startswith(flag) for a in argv), f"登录窗口带了 {flag}"


def test_login_window_uses_its_own_profile():
    """用项目自己的 profile：既不锁用户的日常浏览器，也不碰他的数据。"""
    argv = edge_argv("msedge.exe", r"D:\Amyapp\ticket-radar\data\browser-profile", "https://x/")

    assert any(a.startswith("--user-data-dir=") for a in argv)
    assert not any("AppData" in a for a in argv), "别去动用户日常的 Edge 配置"


def test_login_window_disables_the_proxy():
    """会话注入的 http_proxy 会让 Chromium 把所有请求塞进代理，页面直接打不开。"""
    assert "--no-proxy-server" in edge_argv("msedge.exe", "C:/profile", "https://x/")


def test_login_window_puts_the_url_last():
    """URL 必须是最后一个参数，否则 Edge 会把它当参数解析。"""
    argv = edge_argv("msedge.exe", "C:/profile", "https://www.damai.cn/")
    assert argv[-1] == "https://www.damai.cn/"


# ---------------------------------------------------------------------------
# 找 Edge
# ---------------------------------------------------------------------------


def test_find_edge_returns_none_when_not_installed(monkeypatch):
    """Edge 装在别处时要能降级，不能抛异常。"""
    monkeypatch.setattr(browser_mod, "EDGE_CANDIDATES", ())
    assert find_edge({"LOCALAPPDATA": ""}) is None


def test_find_edge_picks_an_existing_candidate(monkeypatch, tmp_path):
    fake = tmp_path / "msedge.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(browser_mod, "EDGE_CANDIDATES", (str(fake),))
    assert find_edge({}) == fake


def test_find_edge_skips_missing_candidates(monkeypatch, tmp_path):
    real = tmp_path / "real.exe"
    real.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        browser_mod, "EDGE_CANDIDATES", (str(tmp_path / "nope.exe"), str(real))
    )
    assert find_edge({}) == real


def test_missing_edge_gives_an_actionable_message(monkeypatch):
    """报错要让人知道下一步干什么，而不是丢一个 FileNotFoundError。"""
    monkeypatch.setattr(browser_mod, "EDGE_CANDIDATES", ())
    monkeypatch.setattr(browser_mod, "find_edge", lambda *_a, **_k: None)

    with pytest.raises(BrowserUnavailable) as exc:
        launch_manual_browser(url="https://x/", profile_dir="C:/profile")

    text = str(exc.value)
    assert "Edge" in text
    assert "msedge" in text or "路径" in text


# ---------------------------------------------------------------------------
# 启动子进程
# ---------------------------------------------------------------------------


def test_manual_launch_strips_proxy_from_child_env(monkeypatch, tmp_path):
    """子进程环境里带代理变量的话，用户看到的登录页会打不开。"""
    captured: dict = {}

    class FakeProc:
        def poll(self):  # pragma: no cover - 只是给个形状
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(browser_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:61811")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:61811")

    launch_manual_browser(
        url="https://www.damai.cn/", profile_dir=tmp_path, edge="msedge.exe"
    )

    assert "http_proxy" not in captured["env"]
    assert "HTTPS_PROXY" not in captured["env"]
    assert captured["argv"][-1] == "https://www.damai.cn/"


def test_manual_launch_creates_the_profile_dir(monkeypatch, tmp_path):
    """profile 目录不存在时 Edge 会另起一个临时 profile，登录态就丢了。"""
    target = tmp_path / "data" / "browser-profile"

    class FakeProc:
        def poll(self):  # pragma: no cover
            return 0

    monkeypatch.setattr(browser_mod.subprocess, "Popen", lambda *a, **k: FakeProc())

    launch_manual_browser(url="https://x/", profile_dir=target, edge="msedge.exe")

    assert target.is_dir()


# ---------------------------------------------------------------------------
# 等用户关掉浏览器
# ---------------------------------------------------------------------------


def test_wait_returns_closed_when_process_exits():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert wait_for_browser_exit(proc, timeout=30, poll_interval=0.05) == "closed"
    finally:
        proc.wait(timeout=10)


def test_wait_returns_enter_when_user_presses_enter():
    """Edge 的「启动增强」会让后台进程赖着不走，所以必须留一个快捷键出口。"""

    class AliveProc:
        def poll(self):
            return None

    stop = threading.Event()
    stop.set()
    assert wait_for_browser_exit(AliveProc(), timeout=30, stop_event=stop) == "enter"


def test_wait_gives_up_on_timeout():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_for_browser_exit(proc, timeout=0.3, poll_interval=0.05) == "timeout"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# CLI：默认路径必须走「普通浏览器」，不许回头用自动化窗口
# ---------------------------------------------------------------------------


def test_cli_login_does_not_open_an_automated_window(monkeypatch, tmp_path):
    """本次修复的**根**：只要 login 又回到自动化窗口，用户的滑块就会再挂一次。"""
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    env_file = tmp_path / ".env"
    env_file.write_text("DAMAI_COOKIE=\n", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(env_file))

    calls = {"capture": 0, "launched": 0, "read": 0}

    def fake_capture(**_kwargs):
        calls["capture"] += 1
        raise AssertionError("默认路径不该用自动化窗口登录")

    def fake_launch(*, url, profile_dir, edge=None):
        calls["launched"] += 1

        class FakeProc:
            def poll(self):
                return 0

        return FakeProc()

    async def fake_read_cookies(**_kwargs):
        calls["read"] += 1
        return "_m_h5_tk=abc_1759123456789; cookie2=xyz", ["_m_h5_tk", "cookie2"]

    monkeypatch.setattr(browser_mod, "capture", fake_capture)
    monkeypatch.setattr(browser_mod, "launch_manual_browser", fake_launch)
    monkeypatch.setattr(browser_mod, "read_cookies", fake_read_cookies)
    # 回车监视器会阻塞在 input() 上，测试里换掉（proc.poll() 会先返回）
    monkeypatch.setattr(browser_mod, "watch_for_enter", lambda: None)

    result = CliRunner().invoke(cli_mod.app, ["login", "damai"])

    assert result.exit_code == 0, result.output
    assert calls == {"capture": 0, "launched": 1, "read": 1}

    written = env_file.read_text(encoding="utf-8")
    assert "DAMAI_COOKIE" in written
    assert "abc_1759123456789" in written


def test_cli_login_closes_the_browser_before_reading_cookies(monkeypatch, tmp_path):
    """「按回车」这条出口不会关浏览器，而 Edge 关窗后还会把进程留后台。

    不先把浏览器请走，读取必然单例冲突失败——用户看到的就是
    「回车之后没反应」。所以顺序必须是：关浏览器 -> 读 Cookie。
    """
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    env_file = tmp_path / ".env"
    env_file.write_text("DAMAI_COOKIE=\n", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(env_file))

    order: list[str] = []

    class AliveProc:
        """模拟 Edge 关掉窗口后仍赖在后台的那种进程。"""

        pid = 4242

        def poll(self):
            return None

    async def fake_read_cookies(**_kwargs):
        order.append("read")
        return "_m_h5_tk=abc_1759123456789", ["_m_h5_tk"]

    monkeypatch.setattr(browser_mod, "launch_manual_browser", lambda **kw: AliveProc())
    monkeypatch.setattr(browser_mod, "wait_for_browser_exit", lambda *a, **k: "enter")
    monkeypatch.setattr(browser_mod, "watch_for_enter", lambda: None)
    monkeypatch.setattr(browser_mod, "read_cookies", fake_read_cookies)

    def fake_close(proc, **kwargs):
        order.append("close")
        return True

    monkeypatch.setattr(browser_mod, "close_browser", fake_close)

    result = CliRunner().invoke(cli_mod.app, ["login", "damai"])

    assert result.exit_code == 0, result.output
    assert order == ["close", "read"], "必须先关浏览器，再读 Cookie"


def test_cli_login_bails_out_when_the_browser_wont_close(monkeypatch, tmp_path):
    """关不掉就明说，别硬着头皮去读——那样只会得到一句看不懂的报错。"""
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    env_file = tmp_path / ".env"
    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(env_file))

    class AliveProc:
        pid = 4243

        def poll(self):
            return None

    async def fake_read_cookies(**_kwargs):
        raise AssertionError("关不掉浏览器时不该再去读 Cookie")

    monkeypatch.setattr(browser_mod, "launch_manual_browser", lambda **kw: AliveProc())
    monkeypatch.setattr(browser_mod, "wait_for_browser_exit", lambda *a, **k: "enter")
    monkeypatch.setattr(browser_mod, "watch_for_enter", lambda: None)
    monkeypatch.setattr(browser_mod, "read_cookies", fake_read_cookies)
    monkeypatch.setattr(browser_mod, "close_browser", lambda proc, **kw: False)

    result = CliRunner().invoke(cli_mod.app, ["login", "damai"])

    assert result.exit_code == 1
    assert "关不掉" in result.output
    assert not env_file.exists(), "失败时不该动 .env"


def test_cli_login_reports_when_nothing_was_captured(monkeypatch, tmp_path):
    """没登录成功时不能假装成功——要给出下一步怎么做。"""
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    env_file = tmp_path / ".env"
    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(env_file))

    class FakeProc:
        def poll(self):
            return 0

    async def fake_read_cookies(**_kwargs):
        return "", []

    monkeypatch.setattr(
        browser_mod, "launch_manual_browser", lambda **kwargs: FakeProc()
    )
    monkeypatch.setattr(browser_mod, "read_cookies", fake_read_cookies)
    monkeypatch.setattr(browser_mod, "watch_for_enter", lambda: None)

    result = CliRunner().invoke(cli_mod.app, ["login", "damai"])

    assert result.exit_code == 1
    assert "没拿到" in result.output
    assert not env_file.exists(), "没拿到 Cookie 就不该动 .env"


def test_cli_login_automated_path_is_opt_in(monkeypatch, tmp_path):
    """``--automated`` 是逃生门，不是默认。它必须显式传才会用自动化窗口。"""
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    env_file = tmp_path / ".env"
    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(env_file))

    seen = {"capture": 0}

    class FakeResult:
        cookies = "_m_h5_tk=abc_1759123456789; cookie2=xyz"
        cookie_names = ["_m_h5_tk", "cookie2"]
        requests = []

    async def fake_capture(**_kwargs):
        seen["capture"] += 1
        return FakeResult()

    monkeypatch.setattr(browser_mod, "capture", fake_capture)
    monkeypatch.setattr(
        browser_mod,
        "launch_manual_browser",
        lambda **kwargs: pytest.fail("--automated 时不该走普通浏览器"),
    )

    result = CliRunner().invoke(cli_mod.app, ["login", "damai", "--automated"])

    assert result.exit_code == 0, result.output
    assert seen["capture"] == 1


def test_cli_sniff_refuses_before_any_login(monkeypatch, tmp_path):
    """没有登录态时 sniff 给的是「先去登录」，不是莫名其妙地失败。"""
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_env_path", lambda *a, **k: str(tmp_path / ".env"))

    result = CliRunner().invoke(cli_mod.app, ["sniff", "https://detail.damai.cn/item.htm?id=1"])

    assert result.exit_code == 1
    assert "radar login" in result.output


# ---------------------------------------------------------------------------
# 不需要登录的平台：不许把用户拉去登录
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["maoyan", "moretickets"])
def test_cli_login_refuses_for_public_platforms(monkeypatch, platform):
    """把用户拉去扫码、关窗、写 .env，做完发现压根用不上——那种被耍的感觉很差。

    这两个平台的接口是公开的（2026-09-30 实测），所以 login 应该直接说
    「这步可以省掉」并给出正确动作（radar find），而且**不打开任何浏览器**。
    """
    from typer.testing import CliRunner

    import radar.cli as cli_mod

    def bomb(*_a, **_k):
        raise AssertionError("公开接口的平台不该打开浏览器")

    monkeypatch.setattr(browser_mod, "launch_manual_browser", bomb)
    monkeypatch.setattr(browser_mod, "capture", bomb)

    result = CliRunner().invoke(cli_mod.app, ["login", platform])

    assert result.exit_code == 0, result.output
    assert "不需要登录" in result.output
    # 光说「不用登录」不够，得给出下一步该干什么
    assert f"radar find {platform}" in result.output
