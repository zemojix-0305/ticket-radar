"""钉住两个真实踩到的坑。

1. **收集 Cookie 不能只看主域。** 大麦的登录态横跨 ``.damai.cn`` 与
   ``ipassport.damai.cn``，阿里系的统计/风控又在 ``.mmstat.com``。
   实测只问 ``https://www.damai.cn/`` 会少 4 条——少了它可能就"没登录"。

2. **读 Cookie 前必须确保登录浏览器真的退出了。** Edge 关掉窗口后会把
   主进程留在后台占着 profile，于是紧接着的无头读取单例冲突、直接失败。
   用户看到的症状是「回车之后没反应/取不到 Cookie」。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from radar.browser import close_browser, edge_argv, relevant_cookies, site_root


def _cookie(name: str, domain: str, value: str = "v") -> dict:
    return {"name": name, "value": value, "domain": domain, "path": "/"}


DAMAI = "www.damai.cn"


# ---------------------------------------------------------------------------
# 1. Cookie 收集范围
# ---------------------------------------------------------------------------


def test_keeps_the_main_domain():
    picked = relevant_cookies([_cookie("_m_h5_tk", ".damai.cn")], site_host=DAMAI)
    assert [c["name"] for c in picked] == ["_m_h5_tk"]


def test_keeps_login_subdomain():
    """ipassport 是大麦的登录域，漏了它等于没拿到完整的登录态。"""
    picked = relevant_cookies(
        [_cookie("_uab_collina", "ipassport.damai.cn")], site_host=DAMAI
    )
    assert [c["name"] for c in picked] == ["_uab_collina"]


def test_keeps_alibaba_family_for_alibaba_platforms():
    """大麦走淘宝账号体系，签名和风控依赖阿里系那几个域。"""
    raw = [
        _cookie("cna", ".mmstat.com"),
        _cookie("_tb_token_", ".taobao.com"),
        _cookie("cookie2", ".alibaba.com"),
        _cookie("cbc", ".ynuf.aliapp.org"),
    ]
    assert len(relevant_cookies(raw, site_host=DAMAI)) == 4


def test_drops_other_platforms_and_third_parties():
    """profile 里混着用户逛别的站留下的 Cookie，不能一股脑塞进请求头。"""
    raw = [
        _cookie("BAIDUID", ".baidu.com"),
        _cookie("_lxsdk", ".maoyan.com"),
        _cookie("__uni__uid", ".dcloud.net.cn"),
        _cookie("page404.num", "www.damai.cn"),
    ]
    names = [c["name"] for c in relevant_cookies(raw, site_host=DAMAI)]
    assert names == ["page404.num"], "猫眼/百度/uni 的 Cookie 不该进大麦的请求头"


def test_maoyan_gets_its_own_cookies_and_no_alibaba():
    """猫眼不走淘宝账号体系，就不该把阿里系那堆塞给它。"""
    raw = [
        _cookie("_lxsdk", ".maoyan.com"),
        _cookie("_m_h5_tk", ".damai.cn"),
        _cookie("cna", ".mmstat.com"),
    ]
    names = [c["name"] for c in relevant_cookies(raw, site_host="show.maoyan.com")]
    assert names == ["_lxsdk"]


def test_site_host_covers_platforms_not_in_the_whitelist():
    """白名单之外的新平台（比如以后接的教务系统）也能靠主机名收全。"""
    raw = [
        _cookie("JSESSIONID", "jw.example.edu.cn"),
        _cookie("BAIDUID", ".baidu.com"),
    ]
    picked = relevant_cookies(raw, site_host="jw.example.edu.cn")
    assert [c["name"] for c in picked] == ["JSESSIONID"]


def test_empty_domain_cookie_is_skipped():
    assert relevant_cookies([{"name": "x", "value": "1", "domain": ""}], site_host=DAMAI) == []


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.damai.cn", "damai.cn"),
        ("ipassport.damai.cn", "damai.cn"),
        (".maoyan.com", "maoyan.com"),
        ("localhost", "localhost"),
    ],
)
def test_site_root(host, expected):
    assert site_root(host) == expected


# ---------------------------------------------------------------------------
# 2. 登录浏览器必须真的退出
# ---------------------------------------------------------------------------


def test_close_browser_is_a_noop_when_already_gone(monkeypatch):
    """已经退出的进程不该再去 taskkill 一次。"""

    class Dead:
        pid = 12345

        def poll(self):
            return 0

    called = {"n": 0}
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )

    assert close_browser(Dead()) is True
    assert called["n"] == 0


def test_close_browser_really_ends_a_live_process():
    """活的进程要被结束掉，否则 profile 一直占着。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert close_browser(proc, wait=3.0) is True
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_close_browser_gives_up_gracefully_on_timeout(monkeypatch):
    """关不掉时要返回 False，让调用方能给出人话提示，而不是抛异常。"""
    import radar.browser as mod

    class Stubborn:
        pid = 999999

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: None)
    assert close_browser(Stubborn(), wait=0.5) is False


# ---------------------------------------------------------------------------
# 3. 登录窗口不许留后台进程
# ---------------------------------------------------------------------------


def test_login_window_disables_background_mode():
    """Edge 的「启动增强」会在关窗后留进程，把 profile 锁死。"""
    argv = edge_argv("msedge.exe", "C:/profile", "https://www.damai.cn/")
    assert "--disable-background-mode" in argv


@pytest.mark.parametrize("flag", ["--headless", "--enable-automation"])
def test_background_flag_does_not_smuggle_in_automation(flag):
    """防呆：加参数时别顺手把一个自动化开关也带进来。"""
    argv = edge_argv("msedge.exe", "C:/profile", "https://x/")
    assert not any(a.startswith(flag) for a in argv)
