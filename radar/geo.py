"""城市名匹配。

单独成模块的原因：它被两处用到，而这两处互相不能直接 import——

* :mod:`radar.adapters.moretickets` 要用它按城市过滤场次；
* :mod:`radar.target` 要用它做候选消歧。

而 :mod:`radar.target` 已经 import 了 :mod:`radar.adapters`，反向引用会成环。
所以放这里，两边都往上找。

存在的理由（都是实测踩出来的）
--------------------------------
**中英不一致。** 用户嘴里的城市是中文，平台返回的常常是英文：

    用户说「广州」  摩天轮返回 "Guangzhou, CN"  猫眼返回「广州」

用 ``"广州" in "Guangzhou, CN"`` 判永远是 False，于是：

* 摩天轮按城市过滤 → 一条都不剩 → 任务看起来「没抓到」；
* 消歧时城市筛选 → 一个都匹配不上 → 明明有广州站却说找不到。

而且这种失败**不报错、不崩溃**，只是安静地返回空——最难查的那种。

**城市名会带尾巴。** "Guangzhou, CN"、"HongKong, CN"、"Kuala Lumpur, MY"，
比对前得把标点和空格去掉，大小写也统一。
"""

from __future__ import annotations

import re

#: 中文城市 -> 各地平台常见的英文/拼音写法。
#:
#: 这张表**永远不可能列全**，它只负责把最常见的挡掉。真正兜底的是
#: 「过滤后一条不剩」时的提示——告诉用户「可能是平台用了外文名，
#: 直接用 find 拿 id」，而不是假装平台没有。
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "深圳": ("shenzhen",),
    "香港": ("hongkong", "hong kong"),
    "广州": ("guangzhou",),
    "上海": ("shanghai",),
    "北京": ("beijing", "peking"),
    "澳门": ("macau", "macao"),
    "台北": ("taipei",),
    "天津": ("tianjin",),
    "重庆": ("chongqing",),
    "成都": ("chengdu",),
    "杭州": ("hangzhou",),
    "南京": ("nanjing",),
    "武汉": ("wuhan",),
    "西安": ("xian", "xi an"),
    "长沙": ("changsha",),
    "苏州": ("suzhou",),
    "青岛": ("qingdao",),
    "东京": ("tokyo",),
    "大阪": ("osaka",),
    "首尔": ("seoul", "soul"),
    "曼谷": ("bangkok",),
    "新加坡": ("singapore",),
    "伦敦": ("london",),
    "纽约": ("newyork", "new york"),
    "洛杉矶": ("losangeles", "los angeles"),
    "吉隆坡": ("kualalumpur", "kuala lumpur"),
    "悉尼": ("sydney",),
    "墨尔本": ("melbourne",),
}


def normalize_city(raw: str) -> str:
    """归一化成可比较的小写串。

    「HongKong, CN」→「hongkongcn」，「广州」→「广州」。

    **必须保留汉字。** 踩过的坑：只保留 ``[a-z]`` 会把「广州」清成空串，
    于是猫眼返回的「广州」被判成「不在广州」，筛选静默地一个都匹配不上、
    还不报错。汉字和拉丁字母要一起留。
    """
    return re.sub(r"[^a-z0-9一-鿿]", "", (raw or "").lower())


def city_matches(target: str, want: str) -> bool:
    """``target``（平台返回的地点）是否就是用户说的 ``want``（中文城市名）。

    * ``want`` 为空 → 恒为真。用户没说城市时不该拦他。
    * ``target`` 为空 → 恒为假。**没有地点信息不等于匹配**，
      宁可漏掉也不要把「不知道在哪」的场次当成「在你要的城市」。
    """
    if not want:
        return True
    if not target:
        return False
    for alias in (want, *CITY_ALIASES.get(want, ())):
        key = normalize_city(alias)
        if key and key in normalize_city(target):
            return True
    # 平台直接返回中文城市名的情况（猫眼）
    return want in target


__all__ = ["CITY_ALIASES", "city_matches", "normalize_city"]
