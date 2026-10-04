"""``.env`` 自动加载的测试。

这个模块的存在理由是一次真实事故：README 引导用户「打开 .env 把密钥填进去」，
但 config.py 从来不读 .env —— 照文档操作永远不生效，而且报错说的是
「渠道没填凭据」，完全指不到根因。这里把那条路径钉死。
"""

from __future__ import annotations

import os
import textwrap

import pytest

from radar.config import (
    bootstrap_dotenv,
    find_dotenv,
    load_config,
    load_dotenv,
    parse_dotenv,
)


def test_parse_dotenv_basic():
    text = textwrap.dedent(
        """
        # 注释行
        NTFY_TOPIC=ntfy-radar-abc

        BARK_KEY="abc123"
        DINGTALK_SECRET='s3cr3t'
        export PUSHPLUS_TOKEN=tok   # 行尾注释
        """
    )
    assert parse_dotenv(text) == {
        "NTFY_TOPIC": "ntfy-radar-abc",
        "BARK_KEY": "abc123",
        "DINGTALK_SECRET": "s3cr3t",
        "PUSHPLUS_TOKEN": "tok",
    }


def test_parse_dotenv_keeps_hash_inside_value():
    """只有「空白 + #」才是注释，否则 C:\\a#b 这类值会被截断。"""
    assert parse_dotenv('A="a#b"') == {"A": "a#b"}
    assert parse_dotenv("A=a#b") == {"A": "a#b"}
    assert parse_dotenv("A=a #b") == {"A": "a"}


def test_parse_dotenv_ignores_junk_lines():
    assert parse_dotenv("这不是键值对\n=A\n1BAD=x\nOK=1\n") == {"OK": "1"}


def test_parse_dotenv_keeps_empty_value():
    """``KEY=`` 是最常见的「还没填」状态，要保留成空串而不是丢掉。"""
    assert parse_dotenv("NTFY_TOPIC=") == {"NTFY_TOPIC": ""}


def test_parse_dotenv_keeps_chinese_in_value():
    """值里的中文要原样保留；键必须合法（非法键整行忽略，避免污染 os.environ）。"""
    assert parse_dotenv("SMTP_FROM=余票雷达 <a@b.com>\n") == {
        "SMTP_FROM": "余票雷达 <a@b.com>"
    }
    assert parse_dotenv("备注=张三\nOK=1\n") == {"OK": "1"}


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MY_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MY_KEY", "from-shell")

    load_dotenv(tmp_path / ".env")

    assert os.environ["MY_KEY"] == "from-shell"


def test_load_dotenv_override_true(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MY_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MY_KEY", "from-shell")

    load_dotenv(tmp_path / ".env", override=True)

    assert os.environ["MY_KEY"] == "from-file"


def test_load_dotenv_handles_window_notepad_bom(tmp_path, monkeypatch):
    """记事本存 UTF-8 会带 BOM，否则第一个键会变成 \\ufeffNTFY_TOPIC 而静默失效。"""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    (tmp_path / ".env").write_bytes("NTFY_TOPIC=abc\n".encode("utf-8-sig"))

    load_dotenv(tmp_path / ".env")

    assert os.environ["NTFY_TOPIC"] == "abc"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_find_dotenv_returns_first_existing(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (second / ".env").write_text("X=1", encoding="utf-8")

    assert find_dotenv(first, second) == second / ".env"
    assert find_dotenv(first) is None


def test_bootstrap_prefers_config_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "proj"
    config_dir.mkdir()
    (config_dir / ".env").write_text("WHO=config-dir\n", encoding="utf-8")
    monkeypatch.delenv("WHO", raising=False)

    found = bootstrap_dotenv(config_dir / "tasks.yaml")

    assert found == config_dir / ".env"
    assert os.environ["WHO"] == "config-dir"


# ---------------------------------------------------------------------------
# 集成：这是这次事故的复现用例
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
notify:
  - type: ntfy
    enabled: true
    options:
      topic: ${TEST_RADAR_TOPIC}

tasks:
  - id: t
    adapter: rail12306
    interval_seconds: 300
    params:
      from: 北京南
      to: 上海虹桥
      date: "2026-10-06"
"""


def _write_case(tmp_path, dotenv_value: str):
    cfg = tmp_path / "tasks.yaml"
    cfg.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"TEST_RADAR_TOPIC={dotenv_value}\n", encoding="utf-8"
    )
    return cfg


def test_load_config_reads_dotenv_next_to_config(tmp_path, monkeypatch):
    """README 让用户填 .env，这条路必须是通的。"""
    monkeypatch.delenv("TEST_RADAR_TOPIC", raising=False)
    cfg = _write_case(tmp_path, "ntfy-radar-deadbeef")

    app = load_config(cfg)

    assert app.notify[0].options["topic"] == "ntfy-radar-deadbeef"


def test_load_config_shell_env_wins_over_dotenv(tmp_path, monkeypatch):
    """临时 `TEST_RADAR_TOPIC=x radar run` 应该盖过文件，否则没法临时排查。"""
    monkeypatch.setenv("TEST_RADAR_TOPIC", "from-shell")
    cfg = _write_case(tmp_path, "from-file")

    app = load_config(cfg)

    assert app.notify[0].options["topic"] == "from-shell"


def test_load_config_can_skip_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_RADAR_TOPIC", raising=False)
    cfg = _write_case(tmp_path, "from-file")

    app = load_config(cfg, dotenv=False)

    assert app.notify[0].options["topic"] == ""


@pytest.mark.parametrize("value", ["ntfy-radar-x1", "有中文也可以"])
def test_dotenv_value_survives_roundtrip(tmp_path, monkeypatch, value):
    monkeypatch.delenv("TEST_RADAR_TOPIC", raising=False)
    cfg = _write_case(tmp_path, value)

    assert load_config(cfg).notify[0].options["topic"] == value
