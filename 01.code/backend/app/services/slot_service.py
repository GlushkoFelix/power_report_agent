"""智电析报 · slot 服务（交付物 4 第四批：多轮 slot 继承 + 澄清回路，PRD F4.1 / F4.4 / F2.6）。

职责与定案（第十八/十九/二十轮 grilling）：

| 函数 | 定案依据 |
| --- | --- |
| `merge_slot` | 决策 18-4：标量非 null 即覆盖、null 即继承；`filters` **同 field 替换、异 field 追加** |
| `normalize_slot` | 决策 19-5：`time_range` 仍缺 → 默认「全期」；`intent=澄清` 归一化时重置为 null（不能带着澄清意图生成 SQL） |
| `validate_slot_patch` | 决策 19-3 / 20-1：键白名单（8 字段）+ 类型/长度校验，**不做值域校验**；chat 与 clarify 共用 |
| `resolve_clarify` | 决策 18-1：模型 `intent=澄清`（missing_slot 合法）优先，规则「metric 缺失」兜底 |
| `build_clarify_card` | 决策 18-5：选项**纯规则查表生成**（零 LLM），value 值域绝对合法，点选即可直接合入 |

澄清轮（`run_once` 内）的完整行为见 `analysis_service`：事件序列
`status → slot(部分) → clarify → meta → done(record_id=null)`，落 messages×2、不落 analyses、
**不回写 conversations.slot_json**（决策 18-2 / 20-2）。

规则快通道有**两版**，同由 `fast_slot()` 入口分发（`Settings.slot_fastpath_v2` 开关，**默认开**）：
v1 = 本文件的 `_rule_increment(strict=True)`；v2 = `app.services.slot_rules_v2`（评估见
`02.data/tmp/_rules_proto_eval.md`）。两者返回形状同构，可安全互换。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.errors import validation_error

__all__ = [
    "SLOT_KEYS",
    "DEFAULT_TIME_RANGE",
    "CLARIFY_INTENT",
    "MISSING_SLOT_WHITELIST",
    "METRIC_SYNONYMS",
    "guess_metric_from_question",
    "FASTPATH_ENABLED",
    "FASTPATH_BLOCK_WORDS",
    "fast_slot",
    "fallback_slot",
    "merge_slot",
    "normalize_slot",
    "validate_slot_patch",
    "resolve_clarify",
    "available_metric_labels",
    "build_clarify_card",
]

#: PRD F2.1 的 8 个 slot 字段（`slot_json` 的唯一形状）
SLOT_KEYS: tuple[str, ...] = (
    "time_range", "granularity", "metric", "dimension", "filters", "compare",
    "intent", "chart_hint",
)
#: 标量字段（覆盖即替换；`filters` 是数组、单独合并）
_SCALAR_KEYS: tuple[str, ...] = (
    "time_range", "granularity", "metric", "dimension", "compare", "intent", "chart_hint",
)

#: time_range 的缺省口径（UIUX 6.3：清除后回到默认「全期」；决策 18-1②）
DEFAULT_TIME_RANGE = "全期"

#: 模型发起澄清的 intent 取值（SLOT_SYSTEM 教它何时输出）
CLARIFY_INTENT = "澄清"

#: 模型 `missing_slot` 的合法取值（超出视为无效 → 落回规则检查，决策 20-3）
MISSING_SLOT_WHITELIST: tuple[str, ...] = (
    "metric", "time_range", "granularity", "report", "dimension",
)

#: 指标**同义写法 → 标准指标名**（v1.19.6「过澄清」动态兜底用）。
#: 顺序即优先级：具体指标在前，「总用电量」作为最后兜底；命中多个不同指标时用 `、` 连接
#: （如「峰段、平段、谷段分别是多少」→ 一次性给三个指标，交给 SQL 生成阶段选列）。
METRIC_SYNONYMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("未计量", "其他电器"), "未计量用电量"),
    (("厨房",), "厨房用电量"),
    (("洗衣房", "洗衣机"), "洗衣房用电量"),
    (("热水器", "空调", "热水"), "热水空调用电量"),
    # 功率必须排在「峰/平/谷」之前：否则「平均功率」会被裸字 `平` 抢成「平段用电量」
    (("最大功率", "功率峰值", "峰值功率"), "最大功率"),
    (("平均功率", "功率"), "平均功率"),
    (("峰段", "峰电量", "峰时", "峰期", "峰"), "峰段用电量"),
    # 「平段」不放裸字 `平`（会误吃「平均…」），只认明确的写法
    (("平段", "平电量"), "平段用电量"),
    (("谷段", "谷电量", "谷时", "谷"), "谷段用电量"),
    (("电压",), "电压"),
    (("电流",), "电流"),
    (("覆盖率", "覆盖比例"), "覆盖率"),
    (("负载率", "负荷率"), "负载率"),
    (("异常",), "异常日"),
    # 「缺采 / 有效采集**分钟**」必须是带"分钟"的完整写法：实测「有效采集时间不足的天数」
    # 这类问题问的是**天数**，若用裸词「有效采集」会把它抢成"有效采集分钟"（v2.1）
    (("缺采", "漏采", "缺失分钟", "缺采分钟"), "缺采分钟"),
    (("有效采集分钟", "有效分钟", "采集分钟"), "有效采集分钟"),
    (("多少天", "记录天数", "天数"), "记录天数"),
    (("多少个月", "记录月数", "月数", "几个月"), "记录月数"),
    # 元数据类指标：问"表里有多少行 / 有多少条记录"。它**不是用电指标**，但确实是可执行的问题
    # （`COUNT(*)`），属于「记录天数 / 记录月数」同一族。历史上靠**模型自己**产出
    # `metric="记录行数"` 通过；一旦模型不给（v1.19.26 实测 v3.9 × qwen3.8-max 与
    # deepseek-v4-flash **两批都发生**），规则就会按"metric 缺失"发澄清卡 → 该题**永远拿不到结果**
    # （澄清轮不落 analyses，评测端记成 `empty_result`）。补上词面兜底即可让链路照常执行。
    (("多少行", "几行", "行数", "多少条", "条数", "记录数"), "记录行数"),
)

#: **泛化指标词（兜底）**：只有在上面一个具体指标都没命中时才使用 ——
#: 否则「峰段、平段、谷段的用电量」会因为"用电量"三字再拖一个「总用电量」进来。
#: 说明：「占比 / 同比 / 环比 / 排名」是**表达方式**（intent/compare），不算指标，故不在此表。
GENERIC_METRIC_KEYWORDS: tuple[str, ...] = (
    "总用电", "总电量", "用电量", "耗电量", "多少电", "用了多少", "电量",
    # 「XX 的用电情况 / 用电状况」是**中文最常见的口语化问电量**写法，字面上一个具体指标词都没有
    # （实测 q107「2024 年各月的用电情况」既没"用电量"也没"总用电"）。不补这三个字，规则认出
    # 时间与分组、却认不出指标 ⇒ 交 LLM；LLM 一旦超时（E_LLM）就整题失败。
    # 补进来的效果（2026-10-19 135 轮离线实测）：v2 覆盖 66/135、**误伤 0** ——
    # 「各用途的用电情况」「峰段和谷段的用电情况…」仍为 None（先被分组维度/多段指标收敛掉，
    # 走不到本兜底），v1 关态 Δ=0（v1 的 `FASTPATH_BLOCK_WORDS` 含「各」）。
    "用电情况", "用电状况",
)
GENERIC_METRIC = "总用电量"


def guess_metric_from_question(question: str | None) -> str | None:
    """按**词面**猜指标（过澄清动态兜底）：命中返回标准指标名（多个用 `、` 连接），否则 None。

    定位（真实模型评测暴露）：模型会把「全期平均电压是多少伏？」「峰段、平段、谷段的用电量分别是多少？」
    这类**指标写得很清楚**的问题判成 `missing_slot=metric` 并返回澄清卡，一次评测里占全量 **15%**。
    规则只做兜底、不抢模型的活：模型给出合法 metric 时永远以模型为准。
    """
    text = (question or "").strip()
    if not text:
        return None
    hits: list[str] = []
    for keywords, metric in METRIC_SYNONYMS:
        if metric in hits:
            continue
        if any(keyword in text for keyword in keywords):
            hits.append(metric)
    if not hits:
        # 一个具体指标都没命中 → 才用泛化兜底（避免"用电量"三字把「峰段用电」也拖成两个指标）
        if any(keyword in text for keyword in GENERIC_METRIC_KEYWORDS):
            return GENERIC_METRIC
        return None
    return "、".join(hits)

#: filters.op 的合法取值（与 PRD 9.2 slot 事件一致）
FILTER_OPS: tuple[str, ...] = ("=", "!=", ">", "<", ">=", "<=", "in", "between")

#: 回填值（标量）的长度上限
_PATCH_VALUE_MAX_LEN = 200


# ══════════════════════════════════════════════════════════════════════════
#  规则快通道（v1.19.11，延迟专项优化 ②；v2 见 `SLOT_FASTPATH_V2`）
# ══════════════════════════════════════════════════════════════════════════

#: 快通道总开关（A/B 与回归用；关掉即完全回到"每次都调 LLM"的旧行为）
FASTPATH_ENABLED = True

#: 出现这些词就**不走快通道**（含多轮 / 对比 / 分组 / 占比 / 趋势 / 过滤语义，交 LLM 更稳）
FASTPATH_BLOCK_WORDS: tuple[str, ...] = (
    # 多轮与对比
    "比", "对比", "同比", "环比", "那", "呢", "改成", "换成", "再看", "同样", "以及", "还有",
    # 分组与粒度
    "按", "各", "每", "分", "逐", "排名", "排行", "top", "最高", "最低", "前几",
    # 占比与趋势
    "占比", "比例", "比重", "趋势", "变化", "走势", "波动",
    # 过滤与派生
    "异常", "越界", "缺失", "缺采", "不完整", "覆盖率", "负载率", "周末", "工作日", "部分",
)

#: 时间范围的规则抽取（够用即可：`20xx 年` / `20xx 年 M 月`；顺序无关，月优先）
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")
_MONTH_RANGE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")

#: 时间范围 → 粒度（与 LLM 路径的取值域一致）
_GRANULARITY_BY_RANGE = {"全期": "全期"}


def _rule_increment(
    question: str,
    current_slot: dict[str, Any] | None,
    *,
    strict: bool,
    allow_inherit: bool = False,
) -> dict[str, Any] | None:
    """规则抽取增量 slot 的**公共实现**：`strict=True` 用于快通道（高置信才出手），
    `strict=False` 用于超时兜底（宁可粗一点也要给出结果，避免整题失败）。

    返回与 LLM 路径**同构**的增量（只填 `time_range` / `granularity` / `metric` / `intent` /
    `chart_hint`，其余留空由继承与归一化补齐）；拿不到指标时返回 None。

    **多指标的两种态度（v1.19.20 修正）**：快通道（strict）**拒绝**多指标——它要的是"高置信、不出错"，
    多指标交给 LLM 分别处理更稳；**兜底模式允许**多指标——此时 LLM 已经指望不上，而 SQL 路径
    本来就支持"多指标一列一个"的宽表（见 `SQL_SYSTEM` 第 11 条），把一个**词面确实识别出来**的
    多指标（如「峰段、平段、谷段的用电量」）判成失败，是拿"唯一性"换"整题作废"，不划算。
    """
    text = (question or "").strip()
    if not text:
        return None
    base = current_slot or {}
    metric = guess_metric_from_question(text)
    if not metric:
        # **追问轮可以继承指标**（2026-10-14 补，实测 q119 第二轮「只看工作日。」）：
        # 这类追问本身不含指标词，但会话里早就有指标（多轮语义就是"未提及即继承"）。
        # 以前这里一律返回 None → 超时兜底判"认不出指标"→ 整题失败（还连带撞了写库约束）。
        # 兜底模式（strict=False）且会话确有指标时，允许**指标留空、由合并继承**，
        # 其余能规则认出的字段（时间范围/粒度）照常返回。
        if strict or not allow_inherit or not base.get("metric"):
            return None
    if strict:
        if not FASTPATH_ENABLED:
            return None
        if "、" in metric:  # 多指标：快通道要"高置信"，一律交 LLM（兜底模式则允许，见下）
            return None
        lowered = text.lower()
        if any(word in lowered for word in FASTPATH_BLOCK_WORDS):
            return None
        if base.get("filters") or base.get("compare"):
            return None

    time_range: str | None = None
    month_match = _MONTH_RANGE_RE.search(text)
    year_match = _YEAR_RE.search(text)
    if month_match:
        time_range = f"{month_match.group(1)}-{int(month_match.group(2)):02d}"
    elif year_match:
        time_range = year_match.group(1)
    elif DEFAULT_TIME_RANGE in text or not base.get("time_range"):
        time_range = DEFAULT_TIME_RANGE  # 「全期」明说，或会话也没有 → 按默认口径

    # 粒度按 time_range 反推；time_range 留空（继承会话）时粒度也留空——**一起继承**，
    # 不要想当然填「全期」（否则会把会话里的「2007 年」错标成全期口径）
    granularity = (
        _GRANULARITY_BY_RANGE.get(time_range)
        or ("年" if time_range and len(time_range) == 4 else None)
        or ("月" if time_range and len(time_range) == 7 else None)
    )
    return {
        "time_range": time_range,
        "granularity": granularity,
        "metric": metric,
        "intent": "查数",
        "chart_hint": "表格",
    }


def fast_slot(question: str, current_slot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """**规则快通道**：问题足够简单明确时直接产出增量 slot，省掉一次 LLM 往返（约 1.5–2.0s）。

    **两个版本、一个入口**（A 方案的灰度开关，见 `Settings.slot_fastpath_v2`）：

    - `SLOT_FASTPATH_V2=true`（**默认**，2026-10-16 真模型评测确认后转正）→ 分流到
      `app.services.slot_rules_v2.fast_slot`（结构化 block + 分组维度抽取 + 多指标放行 +
      指标词表加固），覆盖口径 19 → 66/135；
    - `SLOT_FASTPATH_V2=false` → 本文件下方的 v1 规则（`_rule_increment(strict=True)`），
      覆盖口径 = 题库 19/135，**一键回退**到历史口径与历史成绩。
      开关**只在调用时读**（`大修过程中的问题和难点` D-003），改 `.env` 重启即生效。

    v1 的出手条件（**高置信**才出手，其余一律交 LLM，保证澄清 / 多轮 / 分组语义不退化）：

    1. **指标唯一**：`guess_metric_from_question` 恰好命中一个指标（多指标 → 交 LLM）；
    2. **无复杂语义**：不含 `FASTPATH_BLOCK_WORDS` 的任何词（对比 / 分组 / 占比 / 趋势 / 过滤 …）；
    3. **会话条件干净**：现有 slot 没有 `filters`、没有 `compare`（多轮叠加时规则想不全）；
    4. **时间范围可规则确定**：问题里有明确 `20xx 年[/月]` 或「全期」，或会话已有时间范围（本轮不必再提）。

    不满足条件返回 None（调用方照常走 LLM）。
    """
    # 分流点：**唯一**的生产改动处（`slot_rules_v2` 与本模块单向依赖，故用函数内导入避免环）
    from app.core.config import get_settings

    if getattr(get_settings(), "slot_fastpath_v2", False):
        from app.services import slot_rules_v2

        return slot_rules_v2.fast_slot(question, current_slot)
    return _rule_increment(question, current_slot, strict=True)


def fallback_slot(
    question: str, current_slot: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """**超时兜底**（v1.19.11 延迟专项）：slot LLM 超时/不可用时按规则构造增量，避免整题失败。

    比快通道宽松：① 不查 `FASTPATH_BLOCK_WORDS`（"按月份汇总"这类问法也能兜住）；
    ② **允许多指标**（v1.19.20 修正：实测「峰段、平段、谷段的用电量分别是多少？」被"多指标一律拒绝"
    判成整题失败，而 SQL 路径本来就产出宽表）；
    ③ **追问轮允许继承指标**（2026-10-14 补）：本轮问句里没有指标、但**会话已有指标**时
    （如第二轮只写「只看工作日。」），不再判"认不出指标"整题失败，而是返回"指标留空"的增量、
    由 `merge_slot` 从会话继承。
    只有当**本轮认不出、会话也没有**指标时才返回 None，由调用方如实失败（**不编造**指标）。
    """
    return _rule_increment(question, current_slot, strict=False, allow_inherit=True)


# ══════════════════════════════════════════════════════════════════════════
#  合并与归一化（纯函数）
# ══════════════════════════════════════════════════════════════════════════


def _clean_filters(filters: Any) -> list[dict[str, Any]]:
    """filters 规整：只保留 `{field, op, value}` 三键，丢弃畸形项。"""
    if not isinstance(filters, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        op = item.get("op")
        if not isinstance(field, str) or not field.strip() or op not in FILTER_OPS:
            continue
        if "value" not in item:
            continue
        cleaned.append({"field": field, "op": op, "value": item["value"]})
    return cleaned


def merge_slot(base: dict[str, Any] | None, increment: dict[str, Any] | None) -> dict[str, Any]:
    """把「本轮增量 slot」合并进基线（PRD F4.1，决策 18-4）。

    - 标量字段：增量**非 null 即覆盖**、null 即继承（未提及）；
    - `filters`：增量非空时**同 field 替换、异 field 追加**（「只看周末的」再问「只看工作日的」
      → 替换 is_weekend 那条，不会同时存在两条矛盾过滤）；增量为空/缺省 → 继承；
    - 输出仍是 8 键形状，但**不填默认值**（「全期」缺省在 `normalize_slot` 里做，
      澄清轮要展示真实的"部分 slot"）。
    """
    base = base or {}
    increment = increment or {}
    merged: dict[str, Any] = {key: base.get(key) for key in SLOT_KEYS}

    for key in _SCALAR_KEYS:
        value = increment.get(key)
        if value is not None:
            merged[key] = value

    increment_filters = _clean_filters(increment.get("filters"))
    if increment_filters:
        base_filters = _clean_filters(merged.get("filters"))
        replaced_fields = {item["field"] for item in increment_filters}
        kept = [item for item in base_filters if item.get("field") not in replaced_fields]
        merged["filters"] = [*kept, *increment_filters]
    else:
        merged["filters"] = _clean_filters(merged.get("filters"))
    return merged


def normalize_slot(slot: dict[str, Any] | None) -> dict[str, Any]:
    """归一化成 8 键形状（落库 / 事件 / 芯片展示的唯一形状）。

    - `time_range` 仍缺 → 默认「全期」（决策 18-1②）；
    - `intent=澄清` → 重置为 null（决策 19-4：不能带着澄清意图生成 SQL；
      澄清判断发生在 `resolve_clarify`，用的是归一化**前**的原始增量）。
    """
    merged = merge_slot(slot, None)
    if not merged.get("time_range"):
        merged["time_range"] = DEFAULT_TIME_RANGE
    if merged.get("intent") == CLARIFY_INTENT:
        merged["intent"] = None
    return merged


# ══════════════════════════════════════════════════════════════════════════
#  slot_patch 校验（chat 与 clarify 共用，决策 19-3 / 20-1）
# ══════════════════════════════════════════════════════════════════════════


def _validate_scalar(key: str, value: Any) -> str:
    """标量回填值：bool 拒收、数字/字符串统一转字符串、限长 200。"""
    if isinstance(value, bool):
        raise validation_error(f"slot_patch.{key} 不能是布尔值")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise validation_error(f"slot_patch.{key} 必须是字符串")
    text = value.strip()
    if not text:
        raise validation_error(f"slot_patch.{key} 不能为空")
    if len(text) > _PATCH_VALUE_MAX_LEN:
        raise validation_error(f"slot_patch.{key} 过长（上限 {_PATCH_VALUE_MAX_LEN} 字符）")
    return text


def validate_slot_patch(patch: dict[str, Any] | None) -> dict[str, Any]:
    """校验并规整 `slot_patch`（chat 可选字段与 clarify 请求体共用）。

    - 键白名单：只能是 8 个 slot 字段，未知键 400（防脏字段进 `slot_json` 再进提示词）；
    - 标量值：bool 拒收、数字转字符串、≤200 字符；
    - `filters`：对象数组且每项含合法 `field`/`op`/`value`；
    - 值为 null / filters 为空数组 → 视为「未提及」忽略（本批无清除语义，决策 18-4③）；
    - **不做值域校验**（metric 是否在 schema 清单等）——合法选项由后端规则生成，
      值域交给 SQL 生成兜底（决策 19-3）。
    """
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise validation_error("slot_patch 必须是对象")
    invalid = [key for key in patch if key not in SLOT_KEYS]
    if invalid:
        raise validation_error(
            f"slot_patch 含未知字段：{'、'.join(map(str, invalid))}（仅支持 {'、'.join(SLOT_KEYS)}）"
        )

    cleaned: dict[str, Any] = {}
    for key in _SCALAR_KEYS:
        if key in patch and patch[key] is not None:
            cleaned[key] = _validate_scalar(key, patch[key])

    if "filters" in patch:
        filters = patch["filters"]
        if filters is None:
            filters = []
        if not isinstance(filters, list):
            raise validation_error("slot_patch.filters 必须是数组")
        cleaned_filters = _clean_filters(filters)
        if len(cleaned_filters) != len(filters):
            raise validation_error(
                "slot_patch.filters 每项必须是 {field, op, value} 且 op 取 "
                f"{'/'.join(FILTER_OPS)}"
            )
        if cleaned_filters:
            cleaned["filters"] = cleaned_filters
    return cleaned


# ══════════════════════════════════════════════════════════════════════════
#  澄清：触发判断与选项生成（决策 18-1 / 18-5）
# ══════════════════════════════════════════════════════════════════════════


def resolve_clarify(
    *,
    increment_intent: str | None,
    model_missing: str | None,
    merged: dict[str, Any],
    question: str | None = None,
) -> str | None:
    """判断本轮是否需要澄清，返回 `missing_slot`（不需要则 None）。

    优先级（决策 18-1；第 5 条为 v1.19.6 新增的过澄清兜底）：
    1. 模型输出 `intent=澄清` 且 `missing_slot` 在白名单内 → 按模型说的问（语义级歧义，
       规则覆盖不了）；
    2. 规则兜底：合并后 `metric` 仍缺失 → 必澄清（UIUX 6.3：metric 必填不可清除）；
    3. `time_range` 缺失**不澄清**（继承 / 默认「全期」）；
    4. 模型说澄清但 `missing_slot` 非法 → 落回规则检查，规则也不命中就照常执行
       （不让模型任意卡住流程，决策 20-3）；
    5. **词面兜底（v1.19.6）**：问题里已出现指标词（`guess_metric_from_question` 命中）时，
       **不采信** `missing_slot=metric` —— 实测模型把「平均电压」「峰段用电」这类明确指标
       也判成缺失，一次评测里 15% 的题因此只返回澄清卡、没出结果。
       注意：此时调用方需用 `guess_metric_from_question` 把 metric 补进 slot（见 `analysis_service`）。
    """
    guessed = guess_metric_from_question(question)
    if increment_intent == CLARIFY_INTENT and model_missing in MISSING_SLOT_WHITELIST:
        if model_missing == "metric" and guessed:
            return None
        return model_missing
    if not merged.get("metric"):
        if guessed:
            return None
        return "metric"
    return None


def available_metric_labels(schema_columns: list[dict[str, Any]] | None) -> list[str]:
    """从 schema 列清单提取「可用指标」的中文名（agg=sum 且非派生列），去重保序。"""
    from app.services.chart_service import COLUMN_LABELS  # 局部导入避免环

    labels: list[str] = []
    for column in schema_columns or []:
        if column.get("agg") != "sum" or column.get("type") not in ("REAL", "INTEGER"):
            continue
        if column.get("derived"):
            continue
        name = str(column.get("name") or "")
        origin = str(column.get("origin_name") or "").strip()
        label = origin or COLUMN_LABELS.get(name) or name
        if label and label not in labels:
            labels.append(label)
    return labels


def build_clarify_card(
    missing_slot: str,
    *,
    schema_columns: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按 `missing_slot` 查表生成澄清卡片（决策 18-5：**纯规则、零 LLM**）。

    选项的 `value` 与 slot 合入值**严格同域**（点选即可直接合入；`value=null` 表示
    「自定义/不限」，由前端再收集具体值后通过 `slot_patch` 提交）。
    `reports` 为用户可见报表的 `[{report_code, filename}]`（调用方查库后传入）。
    """
    if missing_slot == "metric":
        # v1.19.6：选项由「前 3 个可求和列」改为**全量指标清单**（上限 8，防止卡片过长）——
        # 此前只给 3 个，既误导用户以为系统只认这几个指标，也不利于澄清后回填
        labels = available_metric_labels(schema_columns)[:8]
        return {
            "question": "你想看哪个用电指标？可以从下面选择，或直接输入指标名称。",
            "options": [{"label": label, "value": label} for label in labels],
            "missing_slot": "metric",
        }
    if missing_slot == "time_range":
        return {
            "question": "这个条件想看哪个时间范围？",
            "options": [
                {"label": "全期", "value": "全期"},
                {"label": "今年", "value": "今年"},
                {"label": "近 30 天", "value": "近 30 天"},
                {"label": "自定义", "value": None},
            ],
            "missing_slot": "time_range",
        }
    if missing_slot == "granularity":
        return {
            "question": "按什么粒度汇总？",
            "options": [
                {"label": "小时", "value": "小时"},
                {"label": "日", "value": "日"},
                {"label": "月", "value": "月"},
                {"label": "年", "value": "年"},
            ],
            "missing_slot": "granularity",
        }
    if missing_slot == "dimension":
        return {
            "question": "按什么维度分组？",
            "options": [
                {"label": "不分", "value": None},
                {"label": "月份", "value": "月份"},
                {"label": "星期", "value": "星期"},
                {"label": "时段", "value": "时段"},
                {"label": "设备（分表）", "value": "设备"},
            ],
            "missing_slot": "dimension",
        }
    if missing_slot == "report":
        options = [
            {"label": f"{item['report_code']}（{item['filename']}）", "value": item["report_code"]}
            for item in (reports or [])[:3]
        ]
        return {
            "question": "你指的是哪张报表？",
            "options": options,
            "missing_slot": "report",
        }
    # 白名单兜底（理论上到不了：resolve_clarify 已过滤）
    return {
        "question": "请补充说明你的问题，以便我给出准确结果。",
        "options": [],
        "missing_slot": missing_slot,
    }
