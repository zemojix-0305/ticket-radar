"""把一段看不懂的 JSON，变成能直接用的适配器配置。

为什么这是**开发期**工具而不是运行期依赖
------------------------------------------
监控本身永远不需要 LLM：抓取、限流、去重、推送全是确定性的本地逻辑，
离线、可测、不会哪天因为某个 API 涨价而停摆。

LLM 只在一种场景下真正有价值——你新接一个平台，面对 F12 里那坨私有接口
返回的 JSON，不知道该把哪个 key 填进 ``items_path``、哪个填进
``seat_status``。这件事靠人肉数下标很烦，而模型很擅长。

所以定位是「配置助手」：它吐出一段**你回头扫一眼就能否掉**的 YAML 片段，
而不是让 LLM 参与任何运行时决策。不配 ``LLM_API_KEY`` 时，本项目其余部分
完全不受影响。

两道约束保证它不胡说
--------------------
1. 提示词只给**结构树**（key + 类型 + 少量样例值），不给全量数据；
2. 拿到配置后，**直接用真实的** :meth:`JsonApiAdapter.parse_payload` **跑一遍**，
   解析不出东西就把失败原因回灌给模型重试。

模型可以猜，但猜错了会被它自己的下游代码当场戳穿。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .adapters.json_api import JsonApiAdapter, shape
from .config import TaskConfig

#: 默认走 B.AI 网关（OpenAI 兼容，Agnes 就是这一套）。换任何兼容网关只需改环境变量。
DEFAULT_BASE_URL = "https://api.b.ai/v1"
#: 默认模型。选便宜/免费档即可——这个任务是「照着结构树填表」，不需要强推理。
DEFAULT_MODEL = "deepseek-v4-flash"

#: 送进提示词的结构树上限，防止响应特别大时把 token 打爆。
MAX_SHAPE_CHARS = 6000
#: 样例元素最多附带几个。
MAX_SAMPLES = 2
#: 样例元素里单个字符串截断长度。
SAMPLE_STRIP = 80


class LLMNotConfigured(RuntimeError):
    """没配 LLM 凭据。带上足够清楚的引导文案。"""


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI 兼容网关的连接参数。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LLMConfig:
        env = os.environ if env is None else env
        key = (env.get("LLM_API_KEY") or "").strip()
        if not key:
            raise LLMNotConfigured(
                "未配置 LLM_API_KEY，无法使用配置助手。\n"
                "这个功能是**可选的**：只有在你新接一个平台、需要推断字段路径时才用到。\n"
                "要用的话，在 .env 里填三行（任何 OpenAI 兼容网关都行）：\n"
                "  LLM_BASE_URL=https://api.b.ai/v1\n"
                "  LLM_API_KEY=sk-...\n"
                "  LLM_MODEL=deepseek-v4-flash\n"
                "注意部分海外网关需要代理，且代理得覆盖命令行（全局 / TUN 模式）。"
            )
        return cls(
            base_url=(env.get("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
            api_key=key,
            model=(env.get("LLM_MODEL") or DEFAULT_MODEL).strip(),
            timeout=float(env.get("LLM_TIMEOUT") or 60.0),
        )


@dataclass
class InferResult:
    """一次推断的结果。``params`` 已经过真实解析器验证。"""

    ok: bool
    params: dict[str, Any]
    note: str
    attempts: int
    raw: str = ""
    #: 中间失败的尝试记录，便于判断是模型不行还是结构树给少了
    trail: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 字段契约：必须与 radar/adapters/json_api.py 的 parse_payload 保持一致
# ---------------------------------------------------------------------------

FIELD_SPEC = """\
items_path       string   「可售单元」数组在响应里的位置。支持点号与下标，
                          如 data.result / data.list / result[0].items
item_id          string[] 单元去重键的候选路径（按顺序取第一个非空），
                          如 ["skuId", "id"]。缺省 ["id"]
item_label       string[] 单元显示名的候选路径，如 ["name", "title"]。缺省 ["name"]
item_fields      object   附加字段映射，可选键固定为：
                          from_station / to_station / depart_time /
                          arrive_time / duration（值也是路径，可为数组）
seats_path       string   单元内部「票档数组」的位置。若单元自身就是票档
                          （每个元素一个票档），留空字符串 ""
seat_name        string[] 票档名的候选路径，如 ["priceName", "name"]。缺省 ["name"]
seat_status      string[] 余量字段的候选路径，如 ["remainNum", "stock", "status"]
available_values any[]    视为「有票」的字面量，如 ["有", 1, true]
sold_out_values  any[]    视为「无票」的字面量，如 ["无", 0, false, "售罄"]\
"""

SYSTEM_PROMPT = """\
你在帮一个只读的余票监控程序填写「字段路径配置」。程序按你给的路径从 JSON 里
取值，所以**路径必须真实存在于我给你的结构树里**，不能凭常识编造。

可用配置项如下（只允许出现这些键）：
""" + FIELD_SPEC + """

输出要求：
- 只输出一个 JSON 对象，不要输出任何解释文字。
- 路径写法用点号分隔，数组下标写成 .0 或 [0]，两种都支持。
- 拿不准该用哪个字段时，给**多个候选**（数组形式），程序会按顺序取第一个非空的。
- 不要输出 url / method / headers / cookie —— 那些由用户自己填，
  你只负责数据字段路径。
- 如果结构树里确实找不到余量字段，就输出 {"__error__": "原因"}。

判断线索：
- 票档名常见于 name / priceName / skuName / seatName / categoryName。
- 余量常见于 remainNum / remain / stock / count / num / leftNum / status / saleStatus。
- 余量可能是数字（0 表示无票），也可能是字符串（"有"/"无"/"售罄"）。
  两种情况都要在 available_values / sold_out_values 里说明。\
"""


def _preview_items(payload: Any, limit: int = MAX_SAMPLES) -> str:
    """挑出「最像列表」的那个数组，给出前几个元素的精简 JSON。

    结构树能看出层级，但看不出「哪个字段的值像余票数字」。样例值补上这一环。
    """
    found: list[Any] | None = None

    def walk(node: Any) -> None:
        nonlocal found
        if found is not None:
            return
        if isinstance(node, dict):
            for val in node.values():
                walk(val)
        elif isinstance(node, list) and node and isinstance(node[0], dict):
            found = node

    walk(payload)
    if not found:
        return ""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in list(node.items())[:20]}
        if isinstance(node, list):
            return [strip(v) for v in node[:3]]
        if isinstance(node, str) and len(node) > SAMPLE_STRIP:
            return node[:SAMPLE_STRIP] + "…"
        return node

    samples = [strip(x) for x in found[:limit]]
    return json.dumps(samples, ensure_ascii=False, indent=2)[:2000]


def build_messages(
    payload: Any, intent: str, url: str | None = None
) -> list[dict[str, str]]:
    """把 payload 压缩成提示词（结构树 + 少量样例值）。"""
    shape_text = shape(payload, depth=4, max_items=14)[:MAX_SHAPE_CHARS]
    sample_text = _preview_items(payload)

    parts = []
    if url:
        parts.append(f"接口地址：{url}")
    parts.append(f"接口用途：{intent or '查询可售单元的余量'}")
    parts.append("")
    parts.append("响应结构树（缩进表示层级；`key: 类型`，字符串给了截断预览）：")
    parts.append("```")
    parts.append(shape_text)
    parts.append("```")

    if sample_text:
        parts.append("")
        parts.append("数组元素样例：")
        parts.append("```json")
        parts.append(sample_text)
        parts.append("```")

    parts.append("")
    parts.append("请输出字段路径配置 JSON。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复里抠出 JSON 对象。容忍代码块和前后废话。"""
    candidates: list[str] = []
    match = _JSON_BLOCK.search(text)
    if match:
        candidates.append(match.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for raw in candidates:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("回复里没有可解析的 JSON 对象")


def verify_params(payload: Any, params: dict[str, Any]) -> tuple[bool, str]:
    """拿**真实适配器**当裁判，验证这套路径能不能解出东西。

    这是整个助手最值钱的一步：模型可以猜，但猜完必须过下游代码这一关。
    """
    clean = {k: v for k, v in params.items() if not str(k).startswith("__")}
    try:
        task = TaskConfig(id="_infer", adapter="json-api", params=clean)
    except Exception as exc:  # 配置 schema 不合法
        return False, f"配置本身不合法：{exc}"

    adapter = JsonApiAdapter({})
    try:
        trains = adapter.parse_payload(payload, task)
    except Exception as exc:
        return False, str(exc)

    if not trains:
        return False, "解析成功，但一个可售单元都没解出来（路径可能指到了空数组）"

    seats = sum(len(t.seats) for t in trains.values())
    if seats == 0:
        return False, "解出了单元，但一个票档都没有（检查 seats_path / seat_name / seat_status）"

    return True, f"验证通过：解析出 {len(trains)} 个单元、{seats} 个票档"


async def complete(
    llm: LLMConfig,
    messages: list[dict[str, str]],
    *,
    client: httpx.AsyncClient,
) -> str:
    """调用 OpenAI 兼容的 /chat/completions。"""
    url = f"{llm.base_url}/chat/completions"
    payload = {
        "model": llm.model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    try:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {llm.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"连不上 {url}：{exc}\n"
            "如果用的是海外网关：确认代理已开启，且代理覆盖命令行"
            "（规则/分流模式只放行浏览器，命令行仍会连不上——要全局或 TUN 模式）。"
        ) from exc

    if resp.status_code == 401:
        raise RuntimeError("网关返回 401：API Key 无效或已失效，检查 LLM_API_KEY。")
    if resp.status_code == 402:
        raise RuntimeError("网关返回 402：额度或计费问题，检查该 Key 的余额/套餐。")
    if resp.status_code == 404:
        raise RuntimeError(
            f"网关返回 404：模型 {llm.model!r} 可能不存在。"
            "用 `radar models` 列出该网关可用模型，再把 LLM_MODEL 改成正确的。"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"网关返回 HTTP {resp.status_code}：{resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"网关返回的不是 JSON：{resp.text[:200]!r}") from exc

    choices = data.get("choices") or []
    if not choices:
        # 有些网关把错误塞在 200 响应里
        raise RuntimeError(
            f"网关响应里没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}"
        )
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(
            "模型返回了空内容。若你用的是带思维链的模型，"
            "试试把 LLM_MODEL 换成非推理档（如 deepseek-v4-flash）。"
        )
    return content


async def list_models(llm: LLMConfig, *, client: httpx.AsyncClient) -> list[str]:
    """列出网关可用模型，省得靠猜模型名。"""
    try:
        resp = await client.get(
            f"{llm.base_url}/models",
            headers={"Authorization": f"Bearer {llm.api_key}"},
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"连不上 {llm.base_url}/models：{exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:200]}")
    data = resp.json().get("data") or []
    return [str(m.get("id")) for m in data if m.get("id")]


async def infer_params(
    payload: Any,
    *,
    llm: LLMConfig,
    client: httpx.AsyncClient,
    intent: str = "",
    url: str | None = None,
    attempts: int = 2,
) -> InferResult:
    """推断字段路径，并用真实解析器验证。失败会自动带着错误重试。"""
    messages = build_messages(payload, intent, url)
    trail: list[str] = []
    last_note = ""
    reply = ""

    for attempt in range(1, attempts + 1):
        reply = await complete(llm, messages, client=client)

        try:
            params = extract_json(reply)
        except ValueError as exc:
            last_note = f"回复不是合法 JSON（{exc}）"
            trail.append(f"第 {attempt} 次：{last_note}")
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": f"你上次的输出无法解析：{exc}。"
                    "请只输出一个 JSON 对象，不要任何其他文字。",
                },
            ]
            continue

        if "__error__" in params:
            return InferResult(
                ok=False,
                params={},
                note=f"模型认为无法从这份结构里确定字段：{params['__error__']}",
                attempts=attempt,
                raw=reply,
                trail=trail,
            )

        ok, note = verify_params(payload, params)
        if ok:
            return InferResult(
                ok=True, params=params, note=note, attempts=attempt, raw=reply, trail=trail
            )

        last_note = note
        trail.append(f"第 {attempt} 次：{note.splitlines()[0]}")
        messages = [
            *messages,
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": (
                    "这套路径被真实的解析器试运行了，没有通过：\n"
                    f"{note}\n\n"
                    "请对照结构树重新检查每一处路径，输出修正后的完整 JSON。"
                    "注意 seats_path 只在「单元内部还有一层票档数组」时才填。"
                ),
            },
        ]

    return InferResult(
        ok=False, params={}, note=last_note, attempts=attempts, raw=reply, trail=trail
    )
