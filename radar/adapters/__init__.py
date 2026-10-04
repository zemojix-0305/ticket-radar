"""适配器包的统一出口。

导入此包即完成所有内置适配器的注册——registry 依赖导入副作用，
所以这里必须 import 各个实现模块。

平台全景
--------
======================  ====================  ==============  ==========================
适配器名                 平台                   需登录          说明
======================  ====================  ==============  ==========================
``rail12306``           中国铁路 12306          否              余票查询，匿名可用
``amadeus``             机票（正规开放 API）     是（API Key）    OAuth2，Test 环境免费
``damai``               大麦                    是（仅签名）     mtop 签名，只读项目级售票状态
``maoyan``              猫眼演出                否              公开 JSON 接口，匿名可用
``moretickets``         摩天轮票务              否              公开 JSON 接口，二手票挂单
``fenwandao``           纷玩岛                  是              无网页端，暂接不了（见 README）
``json-api``            任意 JSON 接口           视情况          配置驱动的通用适配器
======================  ====================  ==============  ==========================

猫眼和摩天轮在 2026-09-30 之前被标成「需要登录」，那是错的。实测两者的关键
接口都是匿名的（猫眼走大众点评网关 ``m.dianping.com/myshow``，摩天轮走
``unify.moretickets.com``），不需要 Cookie、不需要签名。详见各自模块的 docstring。

``json-api`` 是兜底：只要某个平台能被「一次 HTTP 请求返回 JSON」描述，
不写代码就能接上——这也是本项目持续扩展的方式。
"""

from __future__ import annotations

from .amadeus import AmadeusAdapter
from .base import Adapter, AdapterError
from .damai import DamaiAdapter
from .json_api import JsonApiAdapter, coerce_availability, dig, first_of, shape
from .maoyan import MaoyanAdapter
from .moretickets import MoreTicketsAdapter
from .rail12306 import Rail12306Adapter
from .registry import available_adapters, create_adapter, get_adapter_class, list_adapters
from .shows import FenWanDaoAdapter

# 触发注册（registry 依赖 import 副作用，别删）
_BUILTIN_ADAPTERS = (
    Rail12306Adapter,
    AmadeusAdapter,
    DamaiAdapter,
    MaoyanAdapter,
    MoreTicketsAdapter,
    FenWanDaoAdapter,
    JsonApiAdapter,
)

__all__ = [
    "Adapter",
    "AdapterError",
    "AmadeusAdapter",
    "DamaiAdapter",
    "FenWanDaoAdapter",
    "JsonApiAdapter",
    "MaoyanAdapter",
    "MoreTicketsAdapter",
    "Rail12306Adapter",
    "available_adapters",
    "coerce_availability",
    "create_adapter",
    "dig",
    "first_of",
    "get_adapter_class",
    "list_adapters",
    "shape",
]
