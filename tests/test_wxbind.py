"""radar wxbind 测试：用小程序 code 换 openid 并写回 .env。不联网。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from radar import cli as cli_mod
from radar.cli import app

runner = CliRunner()

_ENV = (
    "WX_APPID=wxtest0000000000ab\n"
    "WX_APPSECRET=secret-x\n"
    "WX_TEMPLATE_ID=tmpl-x\n"
    "WX_OPEN_ID=\n"
)


def _fake_get(monkeypatch, payload: dict):
    def fake(self, url, params=None, **kwargs):  # noqa: ARG001
        # 必须挂上 request，否则 raise_for_status 会报错
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake)


@pytest.fixture()
def env_dir(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(_ENV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WX_APPID", "wxtest0000000000ab")
    monkeypatch.setenv("WX_APPSECRET", "secret-x")
    return tmp_path


def test_wxbind_writes_openid(env_dir, monkeypatch):
    """成功路径：code → openid → 写进 .env 的 WX_OPEN_ID。"""
    _fake_get(monkeypatch, {"openid": "oTEST123456789", "session_key": "sk"})
    res = runner.invoke(app, ["wxbind", "CODE-abc"])
    assert res.exit_code == 0, res.output

    text = (env_dir / ".env").read_text(encoding="utf-8")
    assert "WX_OPEN_ID=oTEST123456789" in text
    # session_key 属于敏感信息，不该出现在输出里
    assert "sk" not in res.output


def test_wxbind_replaces_existing_value(env_dir, monkeypatch):
    """重复绑定时不能追加一行，得原地替换。"""
    (env_dir / ".env").write_text(_ENV.replace("WX_OPEN_ID=\n", "WX_OPEN_ID=oOLD\n"), encoding="utf-8")
    _fake_get(monkeypatch, {"openid": "oNEW"})
    res = runner.invoke(app, ["wxbind", "CODE-abc"])
    assert res.exit_code == 0, res.output

    text = (env_dir / ".env").read_text(encoding="utf-8")
    assert text.count("WX_OPEN_ID=") == 1
    assert "WX_OPEN_ID=oNEW" in text


def test_wxbind_reports_bad_code(env_dir, monkeypatch):
    """code 失效是最常见的失败，必须给出人话提示 + 非零退出码。"""
    _fake_get(monkeypatch, {"errcode": 40029, "errmsg": "invalid code"})
    res = runner.invoke(app, ["wxbind", "CODE-stale"])
    assert res.exit_code == 1
    assert "40029" in res.output
    assert "WX_OPEN_ID=" in (env_dir / ".env").read_text(encoding="utf-8")


def test_wxbind_requires_credentials(tmp_path, monkeypatch):
    """没填 AppID/Secret 时直接报缺配置，不要发无意义的请求。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WX_APPID", raising=False)
    monkeypatch.delenv("WX_APPSECRET", raising=False)
    res = runner.invoke(app, ["wxbind", "CODE-abc"])
    assert res.exit_code == 1
    assert "WX_APPID" in res.output


def test_wxbind_accepts_inline_credentials(tmp_path, monkeypatch):
    """.env 里没有时，命令行 --app-id / --secret 也要能用。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WX_APPID", raising=False)
    monkeypatch.delenv("WX_APPSECRET", raising=False)
    _fake_get(monkeypatch, {"openid": "oINLINE"})

    res = runner.invoke(
        app, ["wxbind", "CODE-abc", "--app-id", "wxzzz", "--secret", "sss"]
    )
    assert res.exit_code == 0, res.output
    assert "WX_OPEN_ID=oINLINE" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_wxbind_is_registered():
    """命令必须真的注册上，否则 README 里写的用法就是空头支票。"""
    names = [
        c.name or (c.callback.__name__ if c.callback else "")
        for c in cli_mod.app.registered_commands
    ]
    assert "wxbind" in names
