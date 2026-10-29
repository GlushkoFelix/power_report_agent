"""智电析报 · LLM 客户端抽象（PRD F2 / 10.3，交付物 4+9）。

**多厂商策略：只实现一种协议（OpenAI 兼容），换厂商只改 `.env`。**

| 实现 | 用途 | 说明 |
| --- | --- | --- |
| `OpenAICompatibleLLM` | 生产 | `POST {base_url}/chat/completions` + `Bearer` Key，用已装的 `httpx` 直调（不引入 `openai` SDK：它会拖进 `httpx2`/`httpcore2`/`jiter`/`truststore` 四个包，而这里只需一次普通 JSON POST）。适用：阿里云百炼（含 MaaS 专属端点）、DeepSeek、Moonshot、智谱、OpenAI、本地 vLLM / Ollama 等**所有 OpenAI 兼容端点** |
| `ScriptedLLM` | 测试 / 无 Key 环境 | 按 `purpose` 与问题关键字返回**预置**的 slot JSON / SQL / 解读文本，使整条链路在**离线状态**下可端到端跑通与断言 |

`get_llm()` 依据 `.env` 是否配置了 Key 自动选择（`LLM_API_KEY`，兼容旧名 `DASHSCOPE_API_KEY`）：
有 Key → OpenAI 兼容客户端（厂商标识取自 `LLM_PROVIDER_LABEL`，**仅用于展示**），
无 Key → Scripted（并在 `/system/health` 里如实回报 `configured=false`）。

**厂商差异的兜底**：少数端点不认 `response_format`（`json_mode`）会直接返回 400 →
自动去掉该参数重试一次（`LLM_JSON_MODE_FALLBACK`），避免"换个厂商就不可用"。
**不做**：Anthropic / Gemini 的原生协议（请求体、响应体、工具 schema 全不同，需独立 adapter），
属 P2，见 PRD 10.3。

**输出解析容错**（决策 7，三层）：`json.loads` → `json_repair` 修复 → 正则抓 ```json / 最外层花括号；
三层都失败由调用方转 `E_LLM`。
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import BizError, E_LLM

logger = logging.getLogger("zdxb.llm")

__all__ = [
    "LLMReply",
    "LLMClient",
    "OpenAICompatibleLLM",
    "DashScopeLLM",
    "ScriptedLLM",
    "extract_json",
    "get_llm",
    "set_llm",
    "llm_status",
]

#: 单次请求超时（秒）：主力模型生成 SQL 一般 <10s，留足余量
REQUEST_TIMEOUT = 60.0

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class LLMReply:
    """一次模型调用的结果（含计费与耗时，供 `llm_calls` 落库）。"""

    text: str
    model: str
    purpose: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    """模型客户端接口（`ScriptedLLM` 与 `DashScopeLLM` 均实现之）。"""

    name: str
    configured: bool

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMReply: ...


# ── 生产实现：任意 OpenAI 兼容厂商 ──────────────────────────────────────────
#   协议面只有一个：`POST {base_url}/chat/completions` + `Authorization: Bearer <key>` +
#   `messages`/`temperature`/（可选）`response_format`。DeepSeek / Moonshot / 智谱 / OpenAI /
#   本地 vLLM·Ollama / 阿里云百炼（含 MaaS 专属端点）都符合；**换厂商只改 `.env`**。
#   Anthropic / Gemini 的原生协议（请求体、响应体、工具schema 全不同）需要独立 adapter，
#   本版本不做（属 P2，见 PRD 10.3）。

class OpenAICompatibleLLM:
    """OpenAI 兼容客户端（httpx 直调 `{base_url}/chat/completions`）。

    `name` 取配置里的厂商标识（`LLM_PROVIDER_LABEL`），仅用于展示与日志——
    **不参与任何协议分支**，避免"厂商名硬编码"再次渗透进业务逻辑。
    """

    configured = True
    #: 支持模型分级（主/轻量模型）；`analysis_service._model_for` 依据该能力位决定是否传轻量模型
    supports_model_tiers = True

    def __init__(
        self, api_key: str, base_url: str, default_model: str, provider: str = "dashscope"
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self.name = provider or "dashscope"

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
        temperature: float | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        settings = get_settings()
        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 可选请求项（默认不发送，避免影响其他厂商）：
        #   - `enable_thinking=false` 关闭推理型模型的思考过程（实测 4.0s/195 tokens → 0.9s/5 tokens）；
        #   - `max_tokens` 兜底限制输出长度。
        # 用 `getattr` 兜底：单测里常用 SimpleNamespace 打桩 settings，缺这两个新字段时不应炸
        max_tokens = getattr(settings, "llm_max_tokens", 0)
        enable_thinking = getattr(settings, "llm_enable_thinking", None)
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if enable_thinking is not None:
            payload["enable_thinking"] = enable_thinking
        return payload

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            return await client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMReply:
        settings = get_settings()
        payload = self._payload(
            messages, model=model, temperature=temperature, json_mode=json_mode
        )

        started = time.perf_counter()
        try:
            response = await self._post(payload)
        except httpx.HTTPError as exc:
            raise BizError(
                500, E_LLM, f"模型调用失败（网络异常）：{exc}",
                {"purpose": purpose, "provider": self.name},
                hint="请检查网络、LLM_BASE_URL 是否可达，稍后重试",
            ) from exc

        # JSON 模式降级：少数自建/网关端点不认 `response_format` 会直接 400 →
        # 去掉该参数重试一次（三层解析容错仍能兜住输出格式），避免"换个厂商就不可用"
        if (
            response.status_code == 400
            and json_mode
            and settings.llm_json_mode_fallback
            and "response_format" in response.text.lower()
        ):
            logger.warning(
                "%s 端点不支持 response_format，已降级为普通模式重试（purpose=%s）",
                self.name, purpose,
            )
            payload.pop("response_format", None)
            try:
                response = await self._post(payload)
            except httpx.HTTPError as exc:
                raise BizError(
                    500, E_LLM, f"模型调用失败（网络异常）：{exc}",
                    {"purpose": purpose, "provider": self.name},
                    hint="请检查网络、LLM_BASE_URL 是否可达，稍后重试",
                ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            detail = response.text[:300]
            raise BizError(
                500, E_LLM, f"模型调用失败（HTTP {response.status_code}）",
                {"purpose": purpose, "provider": self.name, "detail": detail},
                hint="请检查 LLM_API_KEY（或旧的 DASHSCOPE_API_KEY）是否有效、"
                     "LLM_BASE_URL 与模型名是否匹配该厂商",
            )

        # ⚠️ 端点偶尔会在 **2xx** 下返回**非 JSON 正文**（空体 / 网关提示页 / HTML 错误页）——
        # 直接 `response.json()` 会抛 `JSONDecodeError` **裸穿到编排层**，表现为 `E_INTERNAL`
        # （实测有题 2.8s 就"失败"），既误导归因（看着像代码 bug 或模型问题），
        # 也让"上游可重试的一次抖动"变成"整题失败"（问题 P-039，2026-10-14）。
        try:
            body = response.json()
        except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
            head = (response.text or "")[:120].replace("\n", " ")
            raise BizError(
                502,
                E_LLM,
                f"端点返回了非 JSON 响应（HTTP {response.status_code}，正文 {len(response.text or '')} 字符）",
                {"purpose": purpose, "provider": self.name, "body_head": head},
                hint="多为上游网关抖动（空响应 / 错误页），稍后重试即可",
            ) from exc
        choices = body.get("choices") or []
        if not choices:
            raise BizError(
                500, E_LLM, "模型返回为空", {"purpose": purpose},
                hint="可稍后重试；若持续出现请检查模型名",
            )
        usage = body.get("usage") or {}
        return LLMReply(
            text=(choices[0].get("message") or {}).get("content") or "",
            model=body.get("model") or payload["model"],
            purpose=purpose,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            elapsed_ms=elapsed_ms,
            raw={"id": body.get("id"), "usage": usage},
        )


#: 向后兼容别名（历史上该类叫 `DashScopeLLM`；现在它服务**任意 OpenAI 兼容厂商**）
DashScopeLLM = OpenAICompatibleLLM


# ── 离线实现：脚本化假模型（测试 / 无 Key）──────────────────────────────────

class ScriptedLLM:
    """离线脚本化模型：按 `purpose` + 关键字返回预置结果，行为确定、可断言。

    用途：① pytest（不联网、不花 token、结果可复现）；② 未配置 Key 时的本地冒烟。
    可在测试里用 `set_llm(ScriptedLLM(rules={...}))` 覆写规则以构造失败分支。
    """

    name = "scripted"
    configured = False
    #: 不支持模型分级（没有"轻量模型"概念，传了也没用）
    supports_model_tiers = False

    #: 默认规则：问题关键字 → (slot, route, sql, narrative)，`table` 声明该 SQL 适用的报表表名。
    #: **表名感知**：同一问法在日报 / 月报上给出各自的 SQL（`_pick_rule` 依据提示词里的
    #: Schema 文本挑"表可用"的那条），否则离线演示在默认日报上问"按月趋势"会被守卫
    #: 按表名白名单拦掉（脚本模型硬编码表名所致，不是业务缺陷）。
    DEFAULT_RULES: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
        (
            ("月", "趋势", "月度"),
            {
                "table": "report_monthly",
                "slot": {"time_range": "全期", "granularity": "月", "metric": "总用电量"},
                "route": "sql",
                "sql": (
                    "SELECT year_month, SUM(consumption_kwh) AS consumption_kwh "
                    "FROM report_monthly GROUP BY year_month ORDER BY year_month"
                ),
                "narrative": "按月汇总的全期用电量趋势已生成，共 {{row_count}} 个月份。",
            },
        ),
        (
            # 关键字刻意避开单字"日"（否则"异常日""某月某日"都会命中）：同一问法靠"趋势"命中，
            # 再由 `_pick_rule` 按报表表名二选一
            ("趋势", "按天", "每日", "每天的"),
            {
                "table": "report_daily",
                "slot": {"time_range": "全期", "granularity": "日", "metric": "总用电量"},
                "route": "sql",
                "sql": (
                    "SELECT date, consumption_kwh FROM report_daily "
                    "ORDER BY date LIMIT 1000"
                ),
                "narrative": "按日汇总的用电量趋势已生成，共 {{row_count}} 天。",
            },
        ),
        (
            ("峰", "时段"),
            {
                "table": "report_daily",
                "slot": {"time_range": "全期", "granularity": "全期", "metric": "峰平谷用电量"},
                "route": "sql",
                "sql": (
                    "SELECT SUM(peak_kwh) AS peak_kwh, SUM(flat_kwh) AS flat_kwh, "
                    "SUM(valley_kwh) AS valley_kwh FROM report_daily"
                ),
                "narrative": "全期峰平谷用电量已汇总（峰 {{peak_kwh}} kWh）。",
            },
        ),
        (
            ("峰", "时段"),
            {
                "table": "report_monthly",
                "slot": {"time_range": "全期", "granularity": "月", "metric": "峰平谷用电量"},
                "route": "sql",
                "sql": (
                    "SELECT year_month, peak_kwh, flat_kwh, valley_kwh "
                    "FROM report_monthly ORDER BY year_month LIMIT 1000"
                ),
                "narrative": "按月列出峰平谷用电量，共 {{row_count}} 个月。",
            },
        ),
        (
            ("异常", "越界"),
            {
                "table": "report_daily",
                "slot": {"time_range": "全期", "granularity": "日", "metric": "异常日"},
                "route": "sql",
                "sql": (
                    "SELECT date, consumption_kwh FROM report_daily "
                    "WHERE is_anomaly = 1 ORDER BY date"
                ),
                "narrative": "共识别到 {{row_count}} 个异常日，详见表格。",
            },
        ),
        (
            ("总用电量", "总电量", "总量", "总计"),
            {
                "table": "report_daily",
                "slot": {"time_range": "全期", "granularity": "全期", "metric": "总用电量"},
                "route": "sql",
                "sql": "SELECT SUM(consumption_kwh) AS consumption_kwh FROM report_daily",
                "narrative": "全期总用电量为 {{consumption_kwh}} kWh。",
            },
        ),
        (
            ("总用电量", "总电量", "总量", "总计"),
            {
                "table": "report_monthly",
                "slot": {"time_range": "全期", "granularity": "全期", "metric": "总用电量"},
                "route": "sql",
                "sql": "SELECT SUM(consumption_kwh) AS consumption_kwh FROM report_monthly",
                "narrative": "全期总用电量为 {{consumption_kwh}} kWh。",
            },
        ),
        (
            ("厨房", "洗衣房", "未计量", "用途"),
            {
                "table": "report_daily",
                "slot": {"time_range": "全期", "granularity": "日", "metric": "厨房用电量"},
                "route": "sql",
                "sql": (
                    "SELECT date, sm1_kwh, sm2_kwh, sm3_kwh, unmetered_kwh "
                    "FROM report_daily ORDER BY date LIMIT 1000"
                ),
                "narrative": "按用途拆分（厨房 / 洗衣房 / 热水器与空调 / 未计量）已生成，共 {{row_count}} 天。",
            },
        ),
        (
            ("厨房", "洗衣房", "未计量", "用途"),
            {
                "table": "report_monthly",
                "slot": {"time_range": "全期", "granularity": "月", "metric": "厨房用电量"},
                "route": "sql",
                "sql": (
                    "SELECT year_month, sm1_kwh, sm2_kwh, sm3_kwh, unmetered_kwh "
                    "FROM report_monthly ORDER BY year_month LIMIT 1000"
                ),
                "narrative": "按用途拆分的月度数据已生成，共 {{row_count}} 个月。",
            },
        ),
    )

    #: 兜底规则（没有任何关键字命中时）
    FALLBACK: dict[str, Any] = {
        "slot": {"time_range": "全期", "granularity": "全期", "metric": "总用电量"},
        "sql": "SELECT SUM(consumption_kwh) AS consumption_kwh FROM report_daily",
        "narrative": "全期总用电量为 {{consumption_kwh}} kWh。",
    }

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        #: 自定义规则：`purpose` → 返回值（None 表示按目的走默认行为）。
        #: 值为 **list** 时表示同一 purpose 的多次调用依序取用（用于测试多轮 slot 继承链：
        #: 第一次返回增量 A、第二次返回增量 B……取完后重复最后一项）。
        self.rules = rules or {}
        self._counters: dict[str, int] = {}

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> LLMReply:
        text = self._render(messages, purpose, json_mode)
        return LLMReply(
            text=text,
            model=model or "scripted-qwen",
            purpose=purpose,
            prompt_tokens=sum(len(item.get("content", "")) for item in messages) // 4,
            completion_tokens=len(text) // 4,
            elapsed_ms=1,
        )

    @classmethod
    def _pick_rule(cls, question: str) -> dict[str, Any]:
        """按关键字 + 提示词里的表名挑规则（表名感知，见 `DEFAULT_RULES` 的注释）。

        1. 关键字命中的规则里，优先挑「表名出现在提示词中」的那条（同一问法在日报 / 月报上
           各有一份 SQL，靠 Schema 文本里的表名区分）；
        2. 关键字命中但表都不可用 → 用命中的第一条（保持既有行为：让守卫如实拦截）；
        3. 没有任何关键字命中 → 在「表可用」的规则里挑第一条，最后才兜底 `FALLBACK`。
        """
        hit = [
            rule
            for keywords, rule in cls.DEFAULT_RULES
            if any(keyword in question for keyword in keywords)
        ]
        if hit:
            return next(
                (rule for rule in hit if rule.get("table") and rule["table"] in question),
                hit[0],
            )
        return next(
            (
                rule
                for _keywords, rule in cls.DEFAULT_RULES
                if rule.get("table") and rule["table"] in question
            ),
            cls.FALLBACK,
        )

    def _render(self, messages: list[dict[str, str]], purpose: str, json_mode: bool) -> str:
        if purpose in self.rules:
            payload = self.rules[purpose]
            if isinstance(payload, list):
                index = self._counters.get(purpose, 0)
                self._counters[purpose] = index + 1
                payload = payload[index] if index < len(payload) else payload[-1]
            return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

        question = "\n".join(
            item.get("content", "") for item in messages if item.get("role") == "user"
        )
        matched = self._pick_rule(question)
        if purpose == "slot":
            return json.dumps(matched["slot"], ensure_ascii=False)
        if purpose in ("sql", "gen_sql"):
            return json.dumps(
                {"route": matched.get("route", "sql"), "sql": matched["sql"], "reason": "scripted"},
                ensure_ascii=False,
            )
        if purpose == "gen_code":
            # 换路/重写环节的兜底代码（测试用 rules={"gen_code": ...} 可覆盖）
            code = matched.get("code") or "result = df.head(5)"
            return json.dumps({"code": code, "reason": "scripted"}, ensure_ascii=False)
        if purpose == "narrative":
            return json.dumps(
                {
                    "text": self._fill_narrative(matched["narrative"], question),
                    "key_numbers": self._key_numbers(matched["narrative"], question),
                    "anomalies": [],
                },
                ensure_ascii=False,
            )
        return self._fill_narrative(matched["narrative"], question)

    #: 解读模板里的占位符：`{{row_count}}` / `{{consumption_kwh}}`（与结果列同名）
    _PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
    _ROW_COUNT_RE = re.compile(r"【结果行数】\s*(\d+)")
    _RESULT_ROWS_RE = re.compile(r"【结果数据】\s*(\[.*?\])(?:\n|$)", re.S)

    @classmethod
    def _result_rows(cls, prompt: str) -> list[dict[str, Any]]:
        """从解读提示词里取回结果数据（`【结果数据】` 是结果行的 Python repr，最多 50 行）。"""
        match = cls._RESULT_ROWS_RE.search(prompt)
        if not match:
            return []
        try:
            rows = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @classmethod
    def _fill_narrative(cls, template: str, prompt: str) -> str:
        """把解读模板里的 `{{…}}` 用**真实结果**填上。

        为什么要做这一步：脚本模型返回的是模板文本，若直接当解读展示，用户看到的是
        `共 {{row_count}} 个月份`——真实模型不会这样输出，所以离线链路必须自己"把话说完整"。
        取值规则：`row_count` 取提示词里的【结果行数】，其余 key 取首行同名列（数值带千分位）。
        """
        row_count = cls._ROW_COUNT_RE.search(prompt)
        rows = cls._result_rows(prompt)
        first = rows[0] if rows else {}

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "row_count":
                return row_count.group(1) if row_count else str(len(rows))
            value = first.get(key)
            if value is None:
                return "—"
            if isinstance(value, (int, float)):
                return f"{value:,.2f}".rstrip("0").rstrip(".")
            return str(value)

        return cls._PLACEHOLDER_RE.sub(replace, template)

    @classmethod
    def _key_numbers(cls, template: str, prompt: str) -> list[dict[str, Any]]:
        """把模板里出现的**数值型占位符**转成 `key_numbers`（供前端指标卡展示）。

        标签用中文列名（`peak_kwh` → 峰段电量），与图表 / 表格的展示口径保持一致。
        """
        from app.services.chart_service import COLUMN_LABELS  # 局部导入避免循环依赖

        rows = cls._result_rows(prompt)
        first = rows[0] if rows else {}
        numbers: list[dict[str, Any]] = []
        for key in cls._PLACEHOLDER_RE.findall(template):
            value = first.get(key)
            if isinstance(value, (int, float)):
                numbers.append(
                    {"label": COLUMN_LABELS.get(key, key), "value": value, "unit": "kWh"}
                )
        return numbers


# ── 选择与注入 ──────────────────────────────────────────────────────────────

_CLIENT: LLMClient | None = None


def get_llm() -> LLMClient:
    """按配置返回模型客户端：配置了 Key → OpenAI 兼容客户端；否则离线脚本模型。

    厂商由 `.env` 决定（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_PROVIDER_LABEL`），
    代码里**没有任何厂商分支**。
    """
    global _CLIENT
    if _CLIENT is None:
        settings = get_settings()
        if settings.llm_configured:
            _CLIENT = OpenAICompatibleLLM(
                settings.api_key,
                settings.llm_base_url,
                settings.llm_model_main,
                provider=settings.llm_provider_label,
            )
        else:
            logger.warning("未配置 LLM_API_KEY：使用 ScriptedLLM（离线脚本模型）")
            _CLIENT = ScriptedLLM()
    return _CLIENT


def set_llm(client: LLMClient | None) -> None:
    """注入/重置模型客户端（测试用）。"""
    global _CLIENT
    _CLIENT = client


def llm_status() -> dict[str, Any]:
    """`GET /system/health` 用：如实回报模型配置与当前实现。

    - `provider`：配置的厂商标识（`LLM_PROVIDER_LABEL`），未配置 Key 时为 `scripted`；
    - `protocol`：恒为 `openai-compatible`——**代码只实现这一种协议**（Anthropic/Gemini 原生
      协议需独立适配，本版本不做）；
    - `key_source`：Key 来自哪个变量（`LLM_API_KEY` / `DASHSCOPE_API_KEY`），换厂商时排错用。
    """
    settings = get_settings()
    client = get_llm()
    if settings.llm_api_key.strip():
        key_source = "LLM_API_KEY"
    elif settings.dashscope_api_key.strip():
        key_source = "DASHSCOPE_API_KEY"
    else:
        key_source = None
    return {
        "provider": settings.llm_provider_label if settings.llm_configured else "scripted",
        "protocol": "openai-compatible",
        "impl": client.name,
        "configured": settings.llm_configured,
        "key_source": key_source,
        "base_url": settings.llm_base_url,
        "model_main": settings.llm_model_main,
        "model_light": settings.llm_model_light,
        # 当前编排方式（`legacy` / `langgraph`）——健康检查里如实暴露，便于演示时自证
        # （延迟导入避免与 agent.dispatch 形成环）
        "orchestrator": __import__("app.agent.dispatch", fromlist=["x"]).orchestration_mode(),
    }


# ── 输出解析（决策 7：三层容错）────────────────────────────────────────────

def extract_json(text: str) -> dict[str, Any]:
    """把模型输出解析为 dict：`json.loads` → `json_repair` → 正则抓代码块/最外层花括号。

    三层都失败抛 500 `E_LLM`（调用方落 `status='failed'`，不静默）。
    """
    raw = (text or "").strip()
    if not raw:
        raise BizError(500, E_LLM, "模型输出为空", {}, hint="请稍后重试")

    # ① 直接解析
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    # ② json-repair 修复（尾逗号、单引号、缺引号键等）
    try:
        from json_repair import repair_json

        repaired = repair_json(raw, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            logger.debug("模型输出经 json-repair 修复后解析成功")
            return repaired
    except Exception as exc:  # noqa: BLE001 —— 修复失败继续走第三层
        logger.debug("json-repair 修复失败：%s", exc)

    # ③ 正则抓 ```json 代码块 / 最外层花括号
    for candidate in _candidates(raw):
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                logger.debug("模型输出经代码块/花括号提取后解析成功")
                return payload
        except json.JSONDecodeError:
            continue

    raise BizError(
        500, E_LLM, "模型输出不是可解析的 JSON",
        {"head": raw[:200]},
        hint="已尝试直接解析、自动修复与代码块提取；可重试或调整提问措辞",
    )


def _candidates(raw: str) -> list[str]:
    """候选片段：先代码块内容，再首尾花括号之间的子串。"""
    candidates = [match.strip() for match in _JSON_FENCE.findall(raw) if match.strip()]
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    return candidates
