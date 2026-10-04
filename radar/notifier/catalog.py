"""渠道目录：每个渠道的免费额度与准备成本。

这是「我该选哪个通知渠道」这个问题的**唯一答案来源**——
README 的对比表、``radar channels`` 命令都从这里渲染。
写成代码而不是文档，是为了避免「代码里加了渠道、文档忘了写」，
以及「平台改了免费额度、README 还写着旧数字」。

关于「永久免费」这个诉求
------------------------
本项目是开源项目，作者不能替使用者付费。所以渠道按下面三档排：

1. **零注册、零成本、可自建**（``ntfy`` / ``bark``）——官方公共实例免费，
   且都是 MIT/Apache 开源、能用 Docker 一行自建，不受任何商业政策影响。
   这是开源项目唯一稳妥的默认选择。
2. **大厂机器人，永久免费**（``wecom`` / ``dingtalk``）——额度宽松，
   不需要开发者账号，只要建个群。缺点是消息落在群里、不进私聊。
3. **第三方聚合服务**（``pushplus`` / ``serverchan``）——直达微信最省事，
   但免费额度是平台说了算，改政策的先例不少。用可以，别写进默认配置。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelInfo:
    """一个渠道的选型信息。"""

    name: str
    quota: str
    wechat: bool
    needs: str
    note: str
    #: 是否能通过 Docker 自建，从而彻底摆脱第三方服务
    self_hostable: bool = False


CHANNELS: tuple[ChannelInfo, ...] = (
    ChannelInfo(
        name="ntfy",
        quota="250 条/天，自建不限量",
        wechat=False,
        needs="一个别人猜不到的 topic 名（无需注册）",
        note="唯一零注册渠道。开源可自建，Android/iOS/Web 全平台都有客户端。",
        self_hostable=True,
    ),
    ChannelInfo(
        name="bark",
        quota="免费，自建不限量",
        wechat=False,
        needs="装 Bark App，复制设备码",
        note="iOS 专用，走苹果 APNs，比 ntfy 更即时，支持穿透勿扰模式。",
        self_hostable=True,
    ),
    ChannelInfo(
        name="wecom",
        quota="20 条/分钟",
        wechat=False,
        needs="建一个只有自己的企业微信群，加群机器人",
        note="永久免费，无需开发者账号。消息落在群里，手机同样有提醒。",
    ),
    ChannelInfo(
        name="dingtalk",
        quota="20 条/分钟",
        wechat=False,
        needs="建钉钉群，加自定义机器人（建议开「加签」）",
        note="永久免费，额度宽松。同样落在群里。",
    ),
    ChannelInfo(
        name="pushplus",
        quota="200 条/天",
        wechat=True,
        needs="扫码登录 + 实名认证",
        note="直达微信最省事，但**必须实名**，且免费额度由平台随时调整。",
    ),
    ChannelInfo(
        name="serverchan",
        quota="仅 5 条/天",
        wechat=True,
        needs="扫码登录，复制 SendKey",
        note="免费版额度太小（每天 5 条），余票监控容易不够用；扩容要订阅。",
    ),
    ChannelInfo(
        name="telegram",
        quota="免费无明确上限",
        wechat=False,
        needs="自建网络环境 + BotFather 建机器人",
        note="格式最灵活，但国内需要代理，对普通用户门槛偏高。",
    ),
    ChannelInfo(
        name="smtp",
        quota="取决于邮箱服务商",
        wechat=False,
        needs="邮箱开启 SMTP 并获取授权码",
        note="零额外依赖，但邮件不弹强提醒，抢票这种时效场景不合适。",
    ),
)


def channel_info(name: str) -> ChannelInfo | None:
    for item in CHANNELS:
        if item.name == name:
            return item
    return None


def catalog_names() -> list[str]:
    return [c.name for c in CHANNELS]


__all__ = ["CHANNELS", "ChannelInfo", "catalog_names", "channel_info"]
