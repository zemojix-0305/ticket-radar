"""意图解析：把「用户说的一句话 / 贴的一条链接」翻译成结构化需求。

这一层是「用户想干嘛就干嘛」的第一步。上一层的
:mod:`radar.capability` 回答「哪个平台能办这件事」，这一层回答
「用户到底想办什么事」。

设计取向：**纯规则，不依赖 LLM**
--------------------------------
大模型确实能理解「帮我盯一下周杰伦明年在深圳那场」，但它会带来三个
问题：延迟、费用、以及**说不清为什么解析错了**。而监控需求的结构其实
很窄——平台、城市/线路、日期、演出名。用规则能覆盖绝大多数真实输入，
剩下解析不了的**如实说解析不出来**，而不是让模型编一个。

「解析不出来就明说」和「做不到就明说」是同一套哲学：这个项目的可信度
来自它不装懂。

解析与验证分开
--------------
这一层**只做解析，不发任何网络请求**。所以：

* 它不会因为平台风控而失败，也不会消耗限流额度；
* 它给出的 ``target_id`` 可能是错的（用户贴了个别的平台的链接），
  **验证留给下一层**——用真实接口抓一次，抓不到就知道不对。

这和「体检不猜」是同一个原则：解析是解析，抓取是抓取，混在一起
就分不清是谁的问题了。
"""

from __future__ import annotations

import dataclasses
import re

from .capability import FLIGHT, GENERIC, SHOW, TRAIN, Requirement

# -- 平台识别 ---------------------------------------------------------------

#: 域名片段 -> 平台。按顺序匹配，第一个命中的算。
#:
#: 用「域名片段」而不是完整域名，是为了扛住 App 分享链接的各种变体
#: （``m.`` / ``www.`` / ``detail.`` 前缀、带 hash 路由等等）。
_DOMAIN_PLATFORM: tuple[tuple[str, str], ...] = (
    (r"damai\.cn", "damai"),
    (r"maoyan\.com|dianping\.com", "maoyan"),
    (r"moretickets\.com", "moretickets"),
    (r"12306\.cn", "rail12306"),
)

#: 平台 -> 内部 id 的参数名。跟各适配器 ``fetch`` 里读的键名保持一致。
PLATFORM_ID_KEY: dict[str, str] = {
    "damai": "item_id",
    "maoyan": "performance_id",
    "moretickets": "tour_id",
}

#: 各平台 id 的形态。用来在「抓不准」时挑最像 id 的那个片段。
#: 纯数字（大麦、猫眼）或者长 hex（摩天轮是 MongoDB 风格的 id）。
_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "damai": re.compile(r"\d{6,}"),
    "maoyan": re.compile(r"\d{4,}"),
    "moretickets": re.compile(r"[0-9a-f]{20,}"),
}

#: 中文/英文平台别名 -> 平台 key。用于纯文本需求（「大麦上有陈粒吗」）。
_PLATFORM_WORDS: dict[str, str] = {
    "大麦": "damai",
    "猫眼": "maoyan",
    "摩天轮": "moretickets",
    "12306": "rail12306",
    "铁路": "rail12306",
    "火车": "rail12306",
    "高铁": "rail12306",
    "机票": "flight",
    "航班": "flight",
}

#: 已知城市。用于铁路的 from/to，以及给演出需求补一句「在哪」。
#: 只列常见的，认不出就当没写——**不要瞎猜城市**。
_CITIES: tuple[str, ...] = (
    "北京", "上海", "广州", "深圳", "成都", "杭州", "南京", "武汉", "西安",
    "重庆", "天津", "苏州", "郑州", "青岛", "厦门", "长沙", "昆明", "沈阳",
    "大连", "哈尔滨", "济南", "合肥", "福州", "南昌", "贵阳", "南宁", "太原",
    "石家庄", "兰州", "乌鲁木齐", "呼和浩特", "海口", "三亚", "宁波", "温州",
    "无锡", "常州", "徐州", "烟台", "洛阳", "三亚", "香港", "澳门", "台北",
    "东京", "大阪", "首尔", "曼谷", "新加坡", "伦敦", "纽约", "洛杉矶",
)

_CITY_RE = re.compile("|".join(sorted(_CITIES, key=len, reverse=True)))

#: 文本特征 → 需求类别。用于「句子里没提平台名」的情况。
#:
#: 挑的都是**不会歧义**的词。刻意不用「场」「票」这种宽泛字眼——
#: 「停车场」「车票」都能命中，判错类别比判不出更糟。
_SHOW_HINTS = (
    "演唱会", "演出", "音乐会", "话剧", "场次", "门票", "巡演",
    "音乐节", "歌友会", "音乐会", "话剧", "展览",
)
_TRAIN_HINTS = ("高铁", "动车", "列车", "火车", "12306", "购票")

#: 停用词：出现即从关键词里剔除。
#:
#: 这份表是**故意冗长**的。用户嘴上怎么说我都得能听懂——「帮我盯一下
#: 十月十号陈粒深圳场那张票」里，真正要拿去搜的是「陈粒」三个字。
_STOPWORDS = (
    "帮我", "帮忙", "麻烦", "我要", "我想", "我需要", "请问", "请",
    "盯", "盯着", "监控", "监视", "看着", "关注", "留意", "注意",
    "查", "查询", "看", "看看", "找", "找找", "搜", "搜索",
    "有没有", "是不是", "多少", "还", "到底",
    "有票", "有没有票", "还有票", "出票", "放票", "抢票", "回流票",
    "票", "张", "演出", "演唱会", "音乐会", "话剧", "场次", "场", "站",
    "高铁", "动车", "列车", "火车", "航班", "飞机", "车票", "行程", "出发",
    "一下", "一个", "一些", "上", "那", "这", "有", "了", "的",
    "吗", "呢", "吧", "啊", "呀", "嘛", "哦", "嗯", "是",
)

#: 「A 到 B」「A-B」「A去B」——铁路线路。
#:
#: 这里有个**很容易踩的坑**：不能用字符类 ``[广州长沙北京...]`` 拼城市名，
#: 那样等于把「广州」拆成「广|州」，正则会拿单个汉字去匹配，结果
#: 「广州到长沙」永远匹配不上（实测踩过）。必须用 ``(?:广州|长沙|北京|...)``
#: 这样的**完整分支**。
#:
#: 后缀 ``[东西南北]?站?`` 是为铁路准备的：「广州南 → 长沙南」里的
#: 「南」属于站名的一部分，去掉就变成两个不存在的站。
_ROUTE_RE = re.compile(
    r"({cities})([东西南北]?站?)\s*(?:到|至|去|飞|—|－|-|~|→|->)\s*"
    r"({cities})([东西南北]?站?)".format(cities="|".join(_CITIES))
)

#: 中文数字。日期说「十月二十号」比说「10-20」常见得多。
_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(text: str) -> int:
    """中文数字转整数，支持 1~99（十、十五、二十、二十一…）。

    只处理够用的范围：日期不会超过 31 号。超出就返回 0，
    由调用方当「认不出来」处理——**宁可说不知道，不要猜一个错的日期**。
    """
    text = text.strip()
    if not text or any(ch not in _CN_DIGITS for ch in text):
        return 0
    if "十" not in text:
        return _CN_DIGITS[text]
    head, _, tail = text.partition("十")
    if head and tail:
        return _CN_DIGITS[head] * 10 + _CN_DIGITS[tail]
    if head:
        return _CN_DIGITS[head] * 10
    return 10 + (_CN_DIGITS[tail] if tail else 0)


#: 日期：今天 / 明天 / 后天 / 大后天 / 10月10日 / 十月十号 / +3
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"大后天"), "+3"),
    (re.compile(r"后天"), "+2"),
    (re.compile(r"明天|明日"), "+1"),
    (re.compile(r"今天|今日|当天"), "today"),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"), "M月D日"),
    (re.compile(r"([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*[日号]"), "M月D日中文"),
    # 用 (?!\d) 而不是 \b 收尾：Python 的 \b 基于 \w，而汉字也算 \w，
    # 所以「盯+7的票」里 7 和「的」之间**没有词边界**，\b 会匹配失败。
    # 这是中文语境下用 \b 的经典坑。
    (re.compile(r"\+(\d{1,3})(?!\d)"), "+N"),
)


# -- 解析结果 ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ParsedIntent:
    """一句需求解析后的结构。

    ``notes`` 是**给人看的**：解析过程中发现的疑点（认出了平台但没认出
    关键词、日期写法奇怪……）都往里放。宁可提示一句「我不确定」，也不要
    悄悄按错误的理解去跑监控。
    """

    raw: str
    platform: str = ""           # 平台 key；空 = 没认出来
    target_id: str = ""          # 平台内部 id；空 = 需要靠搜索
    id_key: str = ""             # id 在配置里的参数名
    keyword: str = ""            # 拿去搜的关键词
    route_from: str = ""         # 铁路起点
    route_to: str = ""           # 铁路终点
    city: str = ""               # 演出所在城市（用于消歧，**不能丢**）
    date: str = ""               # 日期表达式
    category: str = GENERIC
    link: str = ""
    notes: tuple[str, ...] = ()
    confidence: float = 0.0

    @property
    def needs_search(self) -> bool:
        """要不要靠搜索定位目标。已有 id 就不用搜。"""
        return not self.target_id

    def requirement(self) -> Requirement:
        """转成路由层能用的需求描述。

        两处刻意的宽松，都是为了别把用户挡在门外：

        * ``category`` 判不出来时传空串（= 不限类别），路由会列出所有
          可用平台让他挑，而不是回一句「没有平台能处理」；
        * ``needs_seat_level`` 恒为 False——用户说「盯陈粒」时，能看到
          项目级状态（「热卖」）通常就够。设成 True 会把大麦这类拿不到
          票档的平台一票否决，而它其实盯得住。
        """
        return Requirement(
            category=self.category if self.category != GENERIC else "",
            need_search=self.needs_search,
            region="",
            needs_seat_level=False,
            text=self.raw,
        )

    def summary(self) -> str:
        """一行话总结，供 CLI 展示。"""
        bits: list[str] = []
        if self.platform:
            bits.append(f"平台 {self.platform}")
        if self.target_id:
            bits.append(f"id {self.target_id}（{self.id_key}）")
        if self.keyword:
            bits.append(f"关键词「{self.keyword}」")
        if self.route_from and self.route_to:
            bits.append(f"线路 {self.route_from} → {self.route_to}")
        if self.city:
            bits.append(f"城市 {self.city}")
        if self.date:
            bits.append(f"日期 {self.date}")
        return "　".join(bits) if bits else "什么都没解析出来"


# -- 解析 -------------------------------------------------------------------


def _detect_platform_from_url(url: str) -> str:
    for pattern, platform in _DOMAIN_PLATFORM:
        if re.search(pattern, url, re.I):
            return platform
    return ""


def _pick_id(url: str, platform: str) -> str:
    """从 URL 里挑出最像 id 的片段。

    为什么用「挑」而不是「按固定正则取」：平台的分享链接格式会变
    （猫眼就同时存在 ``/qqw#/detail/123`` 和 ``/show/123`` 两种），
    穷举格式迟早会漏。**按形态挑** + 后续用真实接口验证，比赌格式稳。
    """
    shape = _ID_SHAPES.get(platform)
    if shape is None:
        return ""
    # 先按 query 参数找（最可靠：?id=xxx）
    for key in ("id", "itemId", "item_id", "tourId", "performanceId"):
        m = re.search(rf"[?&]{key}=([0-9a-zA-Z]+)", url)
        if m:
            return m.group(1)
    # 再按形态在整条 URL 里找
    m = shape.search(url)
    return m.group(0) if m else ""


def _strip_stopwords(text: str) -> str:
    out = text
    for word in _STOPWORDS:
        out = out.replace(word, " ")
    return re.sub(r"\s+", " ", out).strip(" 　,，。.!！?？")


def _parse_date(text: str, notes: list[str]) -> str:
    for pattern, value in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if value == "M月D日":
            month, day = int(m.group(1)), int(m.group(2))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                notes.append(f"日期「{m.group(0)}」看着不像合法日期，已忽略")
                continue
            return f"{month:02d}-{day:02d}"
        if value == "M月D日中文":
            month, day = _cn_to_int(m.group(1)), _cn_to_int(m.group(2))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                notes.append(f"日期「{m.group(0)}」看着不像合法日期，已忽略")
                continue
            return f"{month:02d}-{day:02d}"
        if value == "+N":
            return f"+{int(m.group(1))}"
        return value
    return ""


def parse_intent(text: str) -> ParsedIntent:
    """把一句需求解析成 :class:`ParsedIntent`。**不发任何网络请求。**

    参数是完整的一条输入，可以是：
    * 一条链接 → ``https://detail.damai.cn/item.htm?id=123456``
    * 一句话 → ``盯周杰伦深圳场``
    * 两者混合 → ``https://show.maoyan.com/qqw#/detail/174231 这场``
    """
    raw = (text or "").strip()
    notes: list[str] = []
    if not raw:
        return ParsedIntent(raw="", notes=("输入是空的",), confidence=0.0)

    # 1. 链接
    url_match = re.search(r"https?://[^\s，,。]+", raw)
    url = url_match.group(0) if url_match else ""
    platform = _detect_platform_from_url(url) if url else ""
    target_id = _pick_id(url, platform) if (url and platform) else ""

    # 2. 平台（文本里的平台词；链接识别出来的更可信，不覆盖）
    rest = raw.replace(url, " ") if url else raw
    if not platform:
        for word, key in _PLATFORM_WORDS.items():
            if word in rest:
                platform = key
                break

    # 3. 铁路线路：「广州到长沙南」这种
    route_from = route_to = ""
    rm = _ROUTE_RE.search(rest)
    if rm:
        # 站名后缀（「南」「西」「站」）是站名的一部分，不能丢
        route_from = rm.group(1) + rm.group(2)
        route_to = rm.group(3) + rm.group(4)
        # 识别过就从原文里抹掉，否则「到」「南」这种碎片会漏进搜索关键词
        rest = rest.replace(rm.group(0), " ")

    # 4. 日期
    date = _parse_date(rest, notes)
    dm = next((m for p, _ in _DATE_PATTERNS if (m := p.search(rest))), None)
    if dm:
        rest = rest.replace(dm.group(0), " ")

    # 5. 类别
    #
    # 顺序有讲究：**显式说的平台优先于线路推断**。说了「机票」却因为句子里
    # 出现了「北京到上海」被判成火车，等于把用户的原话当没看见。
    #
    # 最后两条是「文本特征」兜底。实测踩过：用户说「帮我盯十月十号陈粒
    # 深圳场那张票」，句子里没有「猫眼」「大麦」这些平台名，于是类别退回
    # generic，路由把**所有**平台都拒了，输出「没有平台能处理」——
    # 而用户明明在问演出。所以类别判断不能只依赖「认出平台没有」。
    if platform == "flight":
        category = FLIGHT
    elif platform == "rail12306" or route_from:
        category = TRAIN
    elif platform in ("damai", "maoyan", "moretickets"):
        category = SHOW
    elif any(hint in raw for hint in _SHOW_HINTS):
        category = SHOW
    elif any(hint in raw for hint in _TRAIN_HINTS):
        category = TRAIN
    else:
        category = GENERIC

    # 6. 关键词：去掉平台词、城市词、停用词之后剩下的
    #
    # 城市要**单独留一份**再从关键词里抹掉。实测踩过：用户说「陈粒深圳场」，
    # 把「深圳」当停用词删掉后，搜索变宽成「陈粒」，结果返回陈粒在贵阳、
    # 三亚、广州、临沂的场次——**一个深圳的都没有**。城市是这个需求里
    # 最重要的约束，删掉它等于把用户的问题换了一个。
    cities_found = _CITY_RE.findall(rest)
    city = cities_found[0] if cities_found else ""

    keyword = rest
    if platform in PLATFORM_ID_KEY:
        for word, key in _PLATFORM_WORDS.items():
            if key == platform:
                keyword = keyword.replace(word, " ")
    keyword = _CITY_RE.sub(" ", keyword)
    keyword = _strip_stopwords(keyword)
    # 日期残留也要去掉（「10月10日」不该进搜索词）
    keyword = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|\+\d{1,3}", " ", keyword)
    keyword = re.sub(r"\s+", " ", keyword).strip(" 　,，。")

    # 4) 清掉没意义的碎片。
    #
    # 停用词是「替换」式的，删掉「上午」里的「上」会剩下「午」这种单字
    # 碎片。与其把它当关键词拿去搜索（搜「午」的结果毫无意义），
    # 不如老实说「没有关键词」。
    #
    # 另外：铁路和机票是**靠线路定位**的（from/to 或机场三字码），
    # 它们根本不需要搜索词——留着一个「订」「机」之类的碎片只会添乱。
    if category in (TRAIN, FLIGHT) or len(keyword) < 2:
        keyword = ""

    # 7. 置信度与提示
    confidence = 0.0
    if platform:
        confidence += 0.5
    if target_id:
        confidence += 0.4
    elif keyword:
        confidence += 0.2
    if route_from:
        confidence += 0.3

    if not platform:
        notes.append("没认出是哪个平台——可以指明（大麦/猫眼/摩天轮/12306），或直接贴链接")
    elif not target_id and not keyword and not route_from:
        notes.append(f"认出是 {platform} 了，但没找到要盯什么，补个演出名或场次链接")
    if url and not platform:
        notes.append("贴的链接不是受支持的平台，监控不了")
    if category == GENERIC:
        notes.append("没能判断这是要盯什么（演出 / 火车 / 机票？）")

    return ParsedIntent(
        raw=raw,
        platform=platform,
        target_id=target_id,
        id_key=PLATFORM_ID_KEY.get(platform, ""),
        keyword=keyword,
        route_from=route_from,
        route_to=route_to,
        city=city,
        date=date,
        category=category,
        link=url,
        notes=tuple(notes),
        confidence=min(confidence, 1.0),
    )


__all__ = [
    "PLATFORM_ID_KEY",
    "ParsedIntent",
    "parse_intent",
]
