"""渠道目录测试：保证目录和注册表不会各说一套。"""

from __future__ import annotations

from radar.notifier import RECOMMENDED_FREE, list_notifiers
from radar.notifier.catalog import CHANNELS, catalog_names, channel_info


def test_catalog_covers_every_registered_channel():
    """注册了渠道却没写进目录 → 用户在 `radar channels` 里永远看不到它。"""
    assert set(catalog_names()) == set(list_notifiers())


def test_catalog_marks_reflect_reality():
    """标记要和实际能力吻合，不能拿文档吹。"""
    ntfy = channel_info("ntfy")
    assert ntfy is not None
    assert ntfy.self_hostable is True
    assert ntfy.wechat is False  # ntfy 不直达微信，得装 App

    pushplus = channel_info("pushplus")
    assert pushplus is not None
    assert pushplus.wechat is True


def test_recommended_free_channels_are_catalogued():
    for name in RECOMMENDED_FREE:
        assert channel_info(name) is not None, f"{name} 在推荐列表里，但没有目录条目"


def test_catalog_names_are_unique():
    names = catalog_names()
    assert len(names) == len(set(names))


def test_channel_info_returns_none_for_unknown():
    assert channel_info("并不存在的渠道") is None


def test_every_catalog_entry_has_actionable_guidance():
    """「需要你准备」不能是空的——用户看目录就是为了知道要做什么。"""
    for info in CHANNELS:
        assert info.quota.strip(), f"{info.name} 没写免费额度"
        assert info.needs.strip(), f"{info.name} 没写需要准备什么"
