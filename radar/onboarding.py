"""平台接入引导：把「人必须亲手做的那一步」讲到照着做就行。

为什么引导写在代码里，而不是一份 README
----------------------------------------
文档会过期，而且没人读。引导跟着代码走：用户运行 ``radar onboard damai``
看到的就是**当前这个版本真正需要他做的事**，不多不少。

本模块只负责两件事：

1. 描述「只有人能做的那一步」——登录、复制 Cookie、在 F12 里找到接口地址。
   这三件事没有任何程序能替他做，本项目也不该替他做。
2. 校验他做完之后的结果——Cookie 填了没、够不够长、关键字段在不在。

校验刻意是**软的**：拿不准的地方就说拿不准，不猜字段名，更不编造接口地址。
编一个看起来很像的键名比留白更糟——用户会照着去找一个根本不存在的东西。

程序能替他做的部分（探测响应结构、推断字段路径、用真实解析器验证）已经由
``radar probe`` 和 :mod:`radar.assist` 承担了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 校验结果
# ---------------------------------------------------------------------------

EMPTY = "empty"
TOO_SHORT = "too_short"
INCOMPLETE = "incomplete"
OK = "ok"
PLANNED = "planned"
#: 平台压根不需要凭据（公开接口）。这不是「填好了」，也不是「规划中」。
NOT_NEEDED = "not_needed"

#: 短于这个长度的「Cookie」几乎一定是复制漏了——完整串通常上百字符。
MIN_COOKIE_CHARS = 20


@dataclass(frozen=True)
class CredentialStatus:
    """一次凭据体检的结果。``headline`` 直接展示，``hint`` 是补救建议。"""

    level: str
    headline: str
    hint: str = ""

    @property
    def ok(self) -> bool:
        """这个平台此刻能不能跑。

        「无需登录」也算能跑——它比「填好了」还省事。忘了把它算进来，
        概览表会把一个完全可用的平台标成待办。
        """
        return self.level in (OK, NOT_NEEDED)


# ---------------------------------------------------------------------------
# 引导档案
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guide:
    """一个平台的接入说明。

    ``cookie_steps`` / ``api_steps`` 刻意写成「第几步点哪里」而不是「配置
    某字段」——用户此刻面对的是浏览器，不是配置文件。
    """

    key: str
    label: str
    adapter: str
    env_key: str
    login_url: str
    what_you_get: str
    cookie_steps: tuple[str, ...] = ()
    api_steps: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    #: 硬校验的 Cookie 键名。只填**确定无疑**的——宁缺毋滥。
    required_keys: tuple[str, ...] = ()
    sample_task: str = ""
    #: 推荐用哪种方式登录（``radar login`` 弹出浏览器时会提示）。
    #: 只填**实地验证过**的——没验证过的平台留空，别给用户指错路。
    login_hint: str = ""
    #: 这个平台的关键接口是**公开**的（实测过），压根不需要凭据。
    #: 置 True 之后 ``check_credential`` 会说「无需凭据」而不是「未填写」。
    no_credentials: bool = False
    #: 订正 ``planned`` 那句话的说法。教务系统是「还没写」，
    #: 纷玩岛是「平台没有网页端」——同样是「现在不能用」，原因不一样。
    blocked_reason: str = ""
    planned: bool = False


_DAMAI = Guide(
    key="damai",
    label="大麦（演出票）",
    adapter="damai",
    env_key="DAMAI_COOKIE",
    login_url="https://www.damai.cn/",
    what_you_get="盯某场演出的售票状态（热卖 / 缺货 / 预约），从没票变成有票就推你手机上（只提醒，不代下单）",
    required_keys=("_m_h5_tk",),
    login_hint=(
        "选「扫码登录」——手机打开大麦 App（或淘宝 App）扫一下就行，"
        "全程不出现验证码，也不用收短信"
    ),
    cookie_steps=(
        "打开 https://www.damai.cn/ 并登录",
        "★ 别走「密码登录 / 短信登录」：那两条路中间会弹图形验证码，"
        "而且容易反复失败。登录框上方切到「扫码登录」，手机扫一下最快",
        "按 F12 → 切到 Network / 网络 标签 → 按 F5 刷新页面",
        "在请求列表里点第一条（通常就是页面本身那个请求）",
        "右侧找 Request Headers / 请求标头 → 找到 Cookie 那一行 → 右键 → Copy value",
        "这一步一次性拿到整条 Cookie（含 HttpOnly 的那些），不用手工拼分号",
        "打开 ticket-radar 文件夹里的 .env，粘到 DAMAI_COOKIE= 后面（整行别换行、别加引号）",
        "保存。⚠️ 不要发给我，也不要截图给我——它等于你的登录状态",
    ),
    api_steps=(
        "打开你要盯的那场演出详情页（网址形如 https://detail.damai.cn/item.htm?id=123456789）",
        "地址栏里 id= 后面那串数字就是 item_id，填进任务配置的 params.item_id",
        "★ 更省事的办法：跑 radar login damai 时顺手打开一下这个页面，"
        "程序会把页面调过的数据接口一并列出来，不用自己翻 Network",
        "想手工确认接口名：F12 → Network / 网络 → 筛选框输入 mtop，"
        "看名字带 item.detail.getdetail 的那条",
    ),
    caveats=(
        "⚠️ 大麦能监控到的是「项目级售票状态」，不是票档余量。2026-09-30 实测："
        "PC 网页端已经不卖票了（详情页按钮恒为「该渠道不支持购票」，提示「请到大麦App购买」），"
        "票档余量只存在于 App 的选座页，Web 端拿不到。"
        "所以本工具盯的是「巡演各站」列表上的 saleStatus——热卖 / 缺货 / 预约。"
        "粒度比票档粗，但足以回答「现在到底能不能买」，回流票场景要的就是这个",
        "同一场演出的多个日期共用一个项目状态：只要还有任一场可购就是「热卖」。"
        "所以盯加场（如 10-18）时，主场的售罄不会让它变「缺货」，"
        "要两场都卖光才会变——这是刻意保守：宁可漏报，也不误报",
        "接口名与 appKey 会随版本漂移。当前实测有效的是 "
        "mtop.damai.item.detail.getdetail 配 appKey 12574478（H5 端）；"
        "失效时用 radar probe 校正，写进 params.api / params.app_key",
        "Cookie 一般几十天后失效。以后雷达报「令牌过期」或 ret 里出现 FAIL_SYS_TOKEN_*，照上面重取一次即可",
        "遇到 403 / 412 是平台风控，说明频率高了，等一会儿再跑，不要尝试绕过",
        "本工具不代登录、不处理验证码、不做滑块识别、不改设备指纹。"
        "所以「验证码老失败」这件事的正解是换一种登录方式（扫码），"
        "而不是让程序去骗过验证码——后者正是本项目明确不做的事",
    ),
    sample_task="""\
  - id: damai-my-show
    name: 大麦 我的心水演出
    adapter: damai
    enabled: true
    interval_seconds: 600
    jitter_seconds: 60
    credentials: damai
    params:
      item_id: "123456789"
      # 只盯巡演其中一站（站名模糊匹配）。留空则每站状态变化都会推。
      # city: 广州
    watch:
      seat_types: []
      min_count: 1
      notify_on: ["appeared"]""",
)


# 猫眼和摩天轮曾经共用下面这个「取 Cookie + 找接口」的模板，前提是
# 「演出票平台的接口都要登录态」。2026-09-30 的实测推翻了这个前提：
# 两家的关键接口都是公开的，一个 Cookie 都不用。模板随之删除——
# 留着它会诱导后人把「公开平台」也讲成「先取 Cookie」，那就成了假引导。


_MAOYAN = Guide(
    key="maoyan",
    label="猫眼演出",
    adapter="maoyan",
    # env_key 保留但不再是必填：公开接口填了不问、不填也能跑。
    # 有些用户习惯带上登录态，带上确实更稳，所以不禁止。
    env_key="MAOYAN_COOKIE",
    login_url="https://show.maoyan.com/",
    what_you_get="盯猫眼上某场演出的售票状态（在售中 / 预售 / 已售罄…），状态一变就推你手机（只提醒，不代下单）",
    no_credentials=True,
    api_steps=(
        "★ 先跑 radar find maoyan 陈粒 —— 它直接列出候选和 performance_id，"
        "抄进 params.performance_id 就完事，不用自己翻 Network",
        "想手工核对：余票接口是 "
        "m.dianping.com/myshow/ajax/performance/<id>?sellChannel=7。"
        "注意网关在**大众点评**域下，不在 maoyan.com 下",
        "演出网页 show.maoyan.com 是 Next.js 服务端渲染，页面本身不发数据请求，"
        "所以 F12 的 Network 里看不到它——不是你没翻到，是它真的不发",
    ),
    caveats=(
        "猫眼能拿到的是「项目级售票状态」（在售中 / 预售 / 已售罄 / 已结束…），"
        "不是票档余量。理由和大麦一样：Web 端不暴露票档库存",
        "★ 列表接口和详情接口的状态会打架：实测同一时刻列表报「预售」、详情报「在售中」。"
        "列表那份是搜索索引里的旧值，详情才是权威，本适配器只信详情",
        "show.maoyan.com 现在会跳「格瓦拉生活网」，那是同一家（猫眼旗下），不是走错了",
        "遇到 403 / 412 是风控，等一会儿再跑，不要尝试绕过",
    ),
    sample_task="""\
  - id: maoyan-my-show
    name: 猫眼 我的心水演出
    adapter: maoyan
    enabled: true
    interval_seconds: 600
    jitter_seconds: 60
    params:
      performance_id: "501675"     # radar find maoyan 陈粒
      # 也可以只给关键词，让它每轮自己搜（多一次请求，不推荐）：
      # keyword: "陈粒"
      # 只在候选里挑某城的（仅 keyword 模式生效）：city: 广州
    watch:
      seat_types: []
      min_count: 1
      notify_on: ["appeared"]""",
)

_MORETICKETS = Guide(
    key="moretickets",
    label="摩天轮票务",
    adapter="moretickets",
    env_key="MORETICKETS_COOKIE",
    login_url="https://moretickets.com/",
    what_you_get="盯摩天轮上各站场次有没有卖家挂单，从没票变成有票就推你手机（只提醒，不代下单）",
    no_credentials=True,
    api_steps=(
        "★ 先跑 radar find moretickets Jay Chou —— 直接给 tour_id，抄进 params.tour_id",
        "★ 它的搜索很**松**：搜「周杰伦」会连带返回 ENHYPEN、Blue 这些不相干的场次。"
        "所以别照着搜索结果第一行随便填，要用 radar find 挑准 tour_id",
        "想手工核对：搜索走 POST unify.moretickets.com/user/foundation/show/search/v1，"
        "场次走 POST .../pub/session/city/list/v1，body 就一句 {\"tourId\":\"...\"}",
        "★ 它**只认 POST + application/json**。同一个地址用 GET 会返回一个 111 字节空壳"
        "（statusCode 12123），那不是平台故障，是请求姿势不对",
    ),
    caveats=(
        "摩天轮是**二手票**平台：它的「票」是第三方卖家挂单，不是官方直售。"
        "挂单随卖家上下架剧烈波动，通知会比较吵——建议只推 appeared",
        "一个巡演通常只有部分城市挂得上票，「某站没挂单」是常态，不是故障；"
        "冷门演出可能整站都没有挂单，这是平台性质，不是配置错",
        "「有没有票」看场次的 hasTicket 布尔值（最干净）。文案 sessionStatusDesc 只当旁证——"
        "站点有中/英/繁三套，文案会变，布尔值不会",
        "★ 城市字段有两层，含义不同：regionName 才是真实城市（Guangzhou, CN），"
        "cityName 是国家级（China）。params.city 用城市名填（如「广州」），"
        "匹的是 regionName，所以不会把整场中国巡演混在一起",
        "★ 价格带币种：境外场次挂单价是港币（HK$），不是人民币。"
        "本工具会把币种一起显示出来，别把它当票价看",
        "价格含卖家溢价，可能高于票面，风险自担；本工具只做提醒",
    ),
    sample_task="""\
  - id: moretickets-my-show
    name: 摩天轮 我的心水演出
    adapter: moretickets
    enabled: true
    interval_seconds: 900
    jitter_seconds: 60
    params:
      tour_id: "6a2a300f941d1b00014a8828"   # radar find moretickets Jay Chou
      # 只看某城市/区域（模糊匹配）。巡演横跨多城时强烈建议填，
      # 否则每个城市售罄都会推一条：
      city: 广州
      # 也可以只给关键词，让它每轮自己搜（多一次请求，不推荐）：
      # keyword: "Jay Chou"
    watch:
      seat_types: []
      min_count: 2
      notify_on: ["appeared"]""",
)

_FENWANDAO = Guide(
    key="fenwandao",
    label="纷玩岛",
    adapter="fenwandao",
    env_key="FENWANDAO_COOKIE",
    login_url="",  # 没有网页端可登——见下方说明
    what_you_get="（暂不可用）纷玩岛没有网页端购票入口，浏览器里盯不了它的票",
    planned=True,
    blocked_reason="平台没有网页端",
    caveats=(
        "实地查证（2026-09）：纷玩岛官网 www.fenwandao.com 的 HTTPS 证书是坏的、"
        "HTTP 直接连接重置，压根打不开",
        "它的真实域名是 livelab.com.cn（上海名辉文化）。能打开的 m.livelab.com.cn "
        "是个静态 App 下载落地页——页脚还写着「纷玩岛 © 2021」，"
        "点「演唱会」页面纹丝不动，一个数据接口都不发",
        "所以纷玩岛主页就是 App 和微信小程序：票档、余票、抢票全在里面。"
        "小程序接口带 wx 系签名参数，浏览器拿不到可复用的地址",
        "本项目不做小程序签名逆向——那属于对抗平台防护，与「只读提醒」的定位冲突，"
        "也与项目「不做验证码识别 / 不做设备指纹伪装」的红线一致",
        "结论：纷玩岛这一家接不了，不是配置问题，是它没有能被浏览器访问的票务接口。"
        "想盯演出票，用大麦（radar login damai）或猫眼（radar login maoyan）——"
        "这两家网页端是实打实有票档的",
        "适配器代码仍然保留在 radar/adapters/shows.py。哪天它上线了网页版票务页，"
        "这个适配器配一下就能直接用，不用重写",
    ),
)

_JWC = Guide(
    key="jwc",
    label="高校教务系统（规划中，还没实现）",
    adapter="jwc",
    env_key="JWC_USERNAME",
    login_url="",
    what_you_get="查成绩 / 查课表 / 查选课余量——但这一项现在还不能用",
    planned=True,
    caveats=(
        "它和票务平台其实是同一个问题：带登录态去查一个私有接口。"
        "现有的适配器机制、状态机、通知渠道都能直接复用，不用改核心",
        "只多两件事：① 登录通常走统一身份认证，Cookie 会过期，"
        "需要拿账号密码自动重新登录；② 有些系统的课表/成绩是 HTML 表格而不是 JSON，"
        "要多一层解析",
        "所以它需要新写一个「登录 + 会话刷新」的适配器，不是配置一下就能接上的",
        "等你要做的时候告诉我学校名，我判断是哪一套系统（正方 / 强智 / 青果 / URP 差别很大）",
        "明确不做抢课：那会自动提交表单，性质是「代你操作」而不是「只读提醒」，也违反校规。"
        "查余量、提醒你「可以选了」，才是本项目在做的事",
    ),
)


GUIDES: dict[str, Guide] = {
    g.key: g
    for g in (_DAMAI, _MAOYAN, _MORETICKETS, _FENWANDAO, _JWC)
}

#: 中文别名，省得用户去记英文 key
ALIASES: dict[str, str] = {
    "大麦": "damai",
    "damai.cn": "damai",
    "猫眼": "maoyan",
    "摩天轮": "moretickets",
    "纷玩岛": "fenwandao",
    "教务": "jwc",
    "教务系统": "jwc",
}


def iter_guides() -> list[Guide]:
    """按展示顺序返回全部引导档案。"""
    return list(GUIDES.values())


def resolve(key: str | None) -> Guide | None:
    """把用户输入（英文 key 或中文名）解析成引导档案。"""
    if not key:
        return None
    raw = key.strip()
    lowered = raw.lower()
    if lowered in GUIDES:
        return GUIDES[lowered]
    target = ALIASES.get(raw) or ALIASES.get(lowered)
    return GUIDES.get(target) if target else None


def check_credential(guide: Guide, env: Mapping[str, str]) -> CredentialStatus:
    """体检一个平台的凭据：填了没、够不够长、关键字段在不在。"""
    if guide.planned:
        # 「不能用」有两种原因：还没写（教务），和平台本身不给机会（纷玩岛）。
        # 前者说「还没实现」是诚实的，后者再说「还没实现」就是在甩锅给代码了。
        if guide.blocked_reason:
            return CredentialStatus(PLANNED, guide.blocked_reason, "不是配置问题，见下方说明")
        return CredentialStatus(PLANNED, "规划中", "还没实现，见下方说明")

    raw = (env.get(guide.env_key) or "").strip()
    if guide.no_credentials:
        # 公开接口：填了 Cookie 也不会有害（部分平台带上更稳），但绝不能说
        # 「未填写」——那会让用户以为不填就跑不起来，去折腾一件不必要的事。
        if raw:
            return CredentialStatus(
                OK, f"无需登录（你另填了 {len(raw)} 字符的 Cookie，会被带上）"
            )
        return CredentialStatus(NOT_NEEDED, "无需登录", "公开接口，直接就能跑")
    if not raw:
        return CredentialStatus(
            EMPTY, "未填写", f".env 里的 {guide.env_key} 还是空的"
        )
    if len(raw) < MIN_COOKIE_CHARS:
        return CredentialStatus(
            TOO_SHORT,
            f"太短了（{len(raw)} 字符）",
            "大概率只复制了其中一个值，需要拼成完整的 Cookie 串",
        )

    missing = [k for k in guide.required_keys if f"{k}=" not in raw]
    if missing:
        return CredentialStatus(
            INCOMPLETE,
            f"缺少 {'、'.join(missing)}",
            "这一条是签名必需的，缺了请求一定会被拒",
        )

    # 不复述值本身——只报长度，避免把登录态写进日志或终端回滚缓冲
    return CredentialStatus(OK, f"已填写（{len(raw)} 字符）")


__all__ = [
    "ALIASES",
    "EMPTY",
    "GUIDES",
    "INCOMPLETE",
    "MIN_COOKIE_CHARS",
    "NOT_NEEDED",
    "OK",
    "PLANNED",
    "TOO_SHORT",
    "CredentialStatus",
    "Guide",
    "check_credential",
    "iter_guides",
    "resolve",
]
