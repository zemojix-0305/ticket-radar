"""通知渠道。

渠道分三档（详见 README「通知渠道」一节）：

1. **零注册、永久免费**（开源项目的默认推荐）
   ``ntfy`` —— 跨平台，无需账号，官方实例免费 250 条/天，可自建
   ``bark`` —— iOS 专属，走 APNs，可自建
2. **永久免费但要建群**
   ``wecom`` / ``dingtalk`` —— 大厂机器人，额度宽松，消息进群不进私聊
3. **免费额度受限或需实名**
   ``pushplus``（200 条/天，需实名）/ ``serverchan``（仅 5 条/天）
   ``telegram``（需自备网络环境）/ ``smtp``（自己邮箱）
"""

from __future__ import annotations

from .bark import BarkNotifier
from .base import Message, NotConfiguredError, Notifier
from .dingtalk import DingTalkNotifier
from .hub import NotifierHub
from .mail import SmtpNotifier
from .ntfy import NtfyNotifier
from .pushplus import PushPlusNotifier
from .registry import (
    RECOMMENDED_FREE,
    build_notifiers,
    list_notifiers,
    register_notifier,
)
from .serverchan import ServerChanNotifier
from .telegram import TelegramNotifier
from .wecom import WeComNotifier

# 触发注册
_ = (
    BarkNotifier,
    DingTalkNotifier,
    NtfyNotifier,
    PushPlusNotifier,
    ServerChanNotifier,
    TelegramNotifier,
    WeComNotifier,
    SmtpNotifier,
)

__all__ = [
    "RECOMMENDED_FREE",
    "BarkNotifier",
    "DingTalkNotifier",
    "Message",
    "NotConfiguredError",
    "Notifier",
    "NotifierHub",
    "NtfyNotifier",
    "PushPlusNotifier",
    "ServerChanNotifier",
    "SmtpNotifier",
    "TelegramNotifier",
    "WeComNotifier",
    "build_notifiers",
    "list_notifiers",
    "register_notifier",
]
