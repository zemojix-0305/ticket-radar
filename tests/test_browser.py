"""浏览器登录助手的测试。

这里只测不需要真浏览器的部分——**尤其是 :func:`write_env_value`**：
它要动用户的 ``.env``，写坏了会连累所有已配好的凭据，所以边界情况
（更新已有键、追加新键、末尾没换行、值里带空格和分号）都得钉死。
"""

from __future__ import annotations

from radar.browser import (
    clean_env,
    interesting_requests,
    join_cookies,
    site_url,
    write_env_value,
)
from radar.config import parse_dotenv

# ---------------------------------------------------------------------------
# 环境变量：必须把代理剥掉
# ---------------------------------------------------------------------------


def test_clean_env_strips_proxy_variables():
    env = {
        "PATH": "/usr/bin",
        "http_proxy": "http://127.0.0.1:61811",
        "HTTPS_PROXY": "http://127.0.0.1:61811",
        "no_proxy": "localhost",
        "KEEP_ME": "1",
    }
    cleaned = clean_env(env)
    assert "http_proxy" not in cleaned
    assert "HTTPS_PROXY" not in cleaned
    assert "no_proxy" not in cleaned
    assert cleaned["KEEP_ME"] == "1"
    assert cleaned["PATH"] == "/usr/bin"


def test_clean_env_does_not_mutate_input():
    env = {"http_proxy": "x"}
    clean_env(env)
    assert env == {"http_proxy": "x"}


# ---------------------------------------------------------------------------
# 站点地址
# ---------------------------------------------------------------------------


def test_site_url_collapses_to_root():
    assert site_url("https://detail.damai.cn/item.htm?id=123") == "https://detail.damai.cn/"
    assert site_url("https://www.damai.cn/") == "https://www.damai.cn/"
    assert site_url("not-a-url") == "not-a-url"


# ---------------------------------------------------------------------------
# Cookie 拼接
# ---------------------------------------------------------------------------


def test_join_cookies_basic_format():
    raw = [
        {"name": "_m_h5_tk", "value": "abc_123", "domain": ".damai.cn", "path": "/"},
        {"name": "cookie2", "value": "xyz", "domain": ".damai.cn", "path": "/"},
    ]
    assert join_cookies(raw) == "_m_h5_tk=abc_123; cookie2=xyz"


def test_join_cookies_prefers_more_specific_same_name():
    """同名 Cookie 若同时挂在根域和子域上，要取更贴合当前页面的那条。"""
    raw = [
        {"name": "tk", "value": "generic", "domain": ".damai.cn", "path": "/"},
        {"name": "tk", "value": "specific", "domain": "detail.damai.cn", "path": "/item"},
    ]
    assert join_cookies(raw) == "tk=specific"


def test_join_cookies_skips_nameless_and_keeps_order():
    raw = [
        {"name": "", "value": "junk"},
        {"name": "b", "value": "2"},
        {"name": "a", "value": "1"},
    ]
    assert join_cookies(raw) == "b=2; a=1"


def test_join_cookies_empty():
    assert join_cookies([]) == ""


# ---------------------------------------------------------------------------
# 请求筛选
# ---------------------------------------------------------------------------


def test_interesting_requests_keeps_only_xhr_and_fetch():
    entries = [
        ("https://www.damai.cn/", "document"),
        ("https://mtop.damai.cn/h5/api/1.0/", "xhr"),
        ("https://static.damai.cn/a.js", "script"),
        ("https://api.damai.cn/perform", "fetch"),
    ]
    assert interesting_requests(entries) == [
        "https://mtop.damai.cn/h5/api/1.0/",
        "https://api.damai.cn/perform",
    ]


def test_interesting_requests_drops_analytics_noise():
    entries = [
        ("https://sensorsdata.damai.cn/sa.gif", "xhr"),
        ("https://hm.baidu.com/hm.js", "fetch"),
        ("https://mtop.damai.cn/real", "xhr"),
    ]
    assert interesting_requests(entries) == ["https://mtop.damai.cn/real"]


def test_interesting_requests_dedupes_and_limits():
    entries = [("https://a/1", "xhr"), ("https://a/1", "xhr"), ("https://a/2", "xhr")]
    assert interesting_requests(entries) == ["https://a/1", "https://a/2"]
    assert len(interesting_requests([(f"https://a/{i}", "xhr") for i in range(50)], limit=3)) == 3


# ---------------------------------------------------------------------------
# 写 .env —— 动用户文件，边界必须钉死
# ---------------------------------------------------------------------------


def test_write_env_value_updates_existing_key_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "NTFY_TOPIC=my-topic\nDAMAI_COOKIE=\nLLM_API_KEY=sk-keepme\n",
        encoding="utf-8",
    )

    created = write_env_value(env, "DAMAI_COOKIE", "_m_h5_tk=a_1; c=2")

    assert created is False
    parsed = parse_dotenv(env.read_text(encoding="utf-8"))
    assert parsed["DAMAI_COOKIE"] == "_m_h5_tk=a_1; c=2"
    assert parsed["NTFY_TOPIC"] == "my-topic"
    assert parsed["LLM_API_KEY"] == "sk-keepme"


def test_write_env_value_appends_when_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NTFY_TOPIC=my-topic\n", encoding="utf-8")

    created = write_env_value(env, "MAOYAN_COOKIE", "a=1; b=2")

    assert created is True
    parsed = parse_dotenv(env.read_text(encoding="utf-8"))
    assert parsed["MAOYAN_COOKIE"] == "a=1; b=2"
    assert parsed["NTFY_TOPIC"] == "my-topic"


def test_write_env_value_creates_file_and_parents(tmp_path):
    env = tmp_path / "nested" / ".env"

    assert write_env_value(env, "DAMAI_COOKIE", "a=1") is True
    assert parse_dotenv(env.read_text(encoding="utf-8"))["DAMAI_COOKIE"] == "a=1"


def test_write_env_value_handles_file_without_trailing_newline(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NTFY_TOPIC=my-topic", encoding="utf-8")

    write_env_value(env, "DAMAI_COOKIE", "a=1")

    text = env.read_text(encoding="utf-8")
    assert "\nDAMAI_COOKIE=" in text
    parsed = parse_dotenv(text)
    assert parsed["NTFY_TOPIC"] == "my-topic"
    assert parsed["DAMAI_COOKIE"] == "a=1"


def test_write_env_value_quotes_so_semicolons_survive(tmp_path):
    """Cookie 里带分号和空格，不加引号会被行尾注释规则啃掉。"""
    env = tmp_path / ".env"
    cookie = "_m_h5_tk=abc_123; cookie2=xyz; cna=def"
    write_env_value(env, "DAMAI_COOKIE", cookie)

    text = env.read_text(encoding="utf-8")
    assert f'DAMAI_COOKIE="{cookie}"' in text
    assert parse_dotenv(text)["DAMAI_COOKIE"] == cookie


def test_write_env_value_does_not_touch_commented_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# DAMAI_COOKIE=old\nNTFY_TOPIC=t\n", encoding="utf-8")

    write_env_value(env, "DAMAI_COOKIE", "new=1")

    text = env.read_text(encoding="utf-8")
    assert "# DAMAI_COOKIE=old" in text
    assert parse_dotenv(text)["DAMAI_COOKIE"] == "new=1"
