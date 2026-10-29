# -*- coding: utf-8 -*-
"""规则快通道 **v2**（A 方案）：把"能由规则唯一确定"的问句接管的范围从 19/135 扩到 66/135。

定位与开关
----------
本模块是**默认路径**（`Settings.slot_fastpath_v2` 默认 `true`；2026-10-16 真模型评测确认后转正）。
`slot_service.fast_slot()` 在 `SLOT_FASTPATH_V2=true` 时分流到这里；置 `false` ⇒ 完全走既有规则，
**一键回退**到历史口径与历史成绩。

评估依据（**0 额度**，未花一分钱）：`02.data/tmp/_rules_proto_eval.md`
（脚本 `_rules_proto.py` / `_rules_proto_eval.py`，连跑两次输出逐字节一致）。核心结论：

| 项 | 结果 |
| --- | --- |
| 覆盖面 | 现有 19 / 135 轮 → v2 **66 / 135** 轮（14% → 49%），**对现有规则零回退** |
| 接管风险 | 重字段有差异的 28 轮里，规则的槽在 gold 口径下 **28/28 全合规**，**"规则比对照更差" = 0** |
| 多分组风险 | 0（不存在"规则凭空造维度"） |
| 下游反查 | 历史真 SQL 实跑 + 项目判分器：**判对 39 / 判错 1**（唯一例外 q088 历史 SQL 多一列） |
| 收益 | **省 1 次 slot 调用**（实测均 2029 prompt + 256 completion tokens、5.5 s/题）——**不是提正确率** |
| 归因 | 闸门是「结构化 block + 维度抽取」**必须同时上**（单上 19 / 34，全上 66）；多指标放行 +4；词表加固 0 覆盖但修掉 9 轮（8 个题面）多余指标列 |

> 口径变更记录（2026-10-19）：`slot_service.GENERIC_METRIC_KEYWORDS` 补入「用电情况 / 用电状况」
> ⇒ 覆盖 63 → **66**；新增的正是**同一族的 3 轮**（q106/q107/q108 第 1 轮「按月看看 20XX 年的
> 用电情况。」），槽位 `{time_range: 20XX, granularity: 月, metric: 总用电量, dimension: 月份}`；
> 误伤 **0**（「各用途的用电情况」「峰段和谷段的用电情况…」仍为 None），v1 关态 Δ=0。
> 动机不是"多接管几轮"，而是 q107 真的**因为规则认不出指标→交 LLM→LLM 超时**整题失败
> （见 `02.data/tmp/_q107_诊断与修复方案.md`）。

相对 v1（`slot_service._rule_increment(strict=True)`）的四处改动
------------------------------------------------------------

1. **分组维度**：v1 完全不抽 `dimension`，遇"按/各/每/分/逐"一律拒绝；v2 新增 7 条维度规则，
   **只有维度识别得出**才放行（识别不出仍交 LLM —— 这正是 v1 block 词的合理内核）。
   依据：实测 LLM 在这些题上的 `dimension` 常为 `null`（q026/q045/q059），说明 gen 阶段本就靠题面推断。
2. **多指标**：v1 的 strict 一律拒绝；v2 放行。
   依据：实测 LLM 在这些题上给的就是多指标（q052 = 峰/平/谷三段用电量），规则输出与之一致；
   `SQL_SYSTEM` 第 11 条本就支持"多指标一列一个"的宽表。
3. **block 词**：v1 用 35 个**裸子串**匹配，`按/各/每/分/比/那/呢` 这类单字误伤严重；
   v2 换成**结构化语义判定**，只在"规则表达不了"的语义上拒绝
   （TopN 极值 / 占比比率 / 趋势差分 / 模糊派生 / 多轮指代 / 报表专属过滤）。
   依据：q011「…分别是多少」被 `分` 挡住、q021「平均每天」被 `每` 挡住 —— 都不是真复杂。
4. **指标词表加固**：v1 里 `平均功率` 的裸词 `功率` 会把「最大功率/峰值功率」也拖成两指标。
   依据：实测 q054/q061 的 **LLM** slot 就错成了"最大功率、平均功率" —— 规则应当避开这个坑。

两条**必须**遵守的边界（落地检查项，别"顺手"改）
------------------------------------------------

* `granularity` **只给 `小时 / 日 / 月 / 年 / 全期`**（`prompts.py` 的 `SLOT_SYSTEM` 值域）。
  「星期 / 时段 / 异常原因 / 设备」不是时间粒度 → 一律留 `None`，否则图表端点会拿到域外值。
* 单值题 `chart_hint="表格"`；**分组题留 `None`**，交 `chart_service._choose_chart_type()` 形状兜底
  （否则「按月份汇总」这类序列题会被固定成表格，而实测 LLM 给的是柱状）。

不动的部分：`time_range` / `intent` 与 v1 同口径；`filters` / `compare` 一律留空
（与 LLM 在多数题上的实际行为一致，见评估脚本统计）。
"""

from __future__ import annotations

import re
from typing import Any

from app.services import slot_service as ss

__all__ = ["BLOCK_RULES", "DIMENSION_RULES", "extract_dimension", "guess_metric_v2", "fast_slot"]


# ══════════════════════════════════════════════════════════════════════════
#  ③ 结构化 block：只拒绝"规则表达不了"的语义（逐条有实测依据）
# ══════════════════════════════════════════════════════════════════════════

#: (类别名, 正则)。命中即交 LLM。
BLOCK_RULES: dict[str, str] = {
    # TopN / 极值：需要 ORDER BY + LIMIT 与 NULL 排除（`SQL_SYSTEM` 第 15 条），
    # slot 里没有对应字段，且"最大/最低"会污染指标词表（实测 q054/q061）
    "排序/极值": r"最高|最低|最大|最小|最多|最少|前几|前\s*\d+|排名|排行|第几|\btop\b",
    # 占比 / 比率：答案列是已存储的 `*_ratio`（`SQL_SYSTEM` 第 16 条），规则认不出该列
    "占比/比率": r"占比|比例|比重|比率",
    # 趋势 / 差分：口径上"是否保留年份"必须区分（`SQL_SYSTEM` 第 14 条 ①②），规则判不了
    "趋势/差分": r"趋势|走势|波动|变化|相差|差多少",
    # 模糊 / 派生：题面带前提，或需要表达式比较，规则不该猜。
    # ⚠️ 实测修正：「不足」**不能**当 block —— q073「有效采集时间不足自然时长的天数」的
    #    真实 LLM 槽就是 `记录天数`（与本规则同输出）且那题判对；挡掉只是白白退回 LLM。
    "模糊/派生": r"部分|不完整|越界|日均",
    # 过滤语义超出词表：**「不含全期合计行」这类排除条件**需要 `is_total_row` 过滤（实测 q099）。
    # 注意 q096「一共有多少行（含全期合计行）」**不加**过滤、v1 与本规则都给 `记录行数` 且判对
    # —— 所以只在出现"排除"字样时才挡。
    "报表专属条件": r"(不含|不包含|排除|剔除)[^。？]{0,8}(合计行|全期合计)|各\s*用途|用途用电量",
    # 多轮指代：本轮语义依赖上文（"那…呢""改成…""只看…"），交 LLM 更稳
    "多轮指代": r"那|呢|改成|换成|再看|同样|还有|只要|只看|这些",
}


# ══════════════════════════════════════════════════════════════════════════
#  ① 分组维度抽取（v1 完全没有）
# ══════════════════════════════════════════════════════════════════════════

#: (维度名, 触发正则)。**只认明确的"按 X / 各 X / 每 X / 逐 X"写法**——
#: 不用裸字（裸字 `年` 会误吃"2006 年是多少"，那属于 time_range、不是维度）。
DIMENSION_RULES: tuple[tuple[str, str], ...] = (
    ("年份", r"按\s*年|各\s*年度|各\s*年|分\s*年|逐\s*年|每\s*年|年度(?:的)?\s*(?:总|用|峰|平|谷|厨房|洗衣|热水|未计量|平均|峰)"),
    ("月份", r"按\s*月|各\s*月|分\s*月|逐\s*月|每\s*个月|每个月|按\s*月份"),
    ("星期", r"按\s*星期|按\s*周|星期几|周几|各\s*星期|每\s*周|逐\s*周"),
    ("小时", r"按\s*小时|各\s*小时|逐\s*小时|每\s*小时|分\s*小时"),
    ("时段", r"按[^，。？]{0,8}时段|时段汇总|按时段"),
    ("异常原因", r"异常原因|异常类型|异常的理由"),
    ("设备", r"按\s*设备|各\s*设备|按\s*分表|分表"),
)

_MONTH_RANGE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")

#: 维度 → 粒度。**只能给 `SLOT_SYSTEM` 写死的值域**（`小时/日/月/年/全期`）：
#: 「星期 / 时段 / 异常原因 / 设备」不是时间粒度，映射成"星期"这种非法值会让图表端点
#: 拿到值域外的输入 —— 一律留 None。
_GRANULARITY_BY_DIMENSION = {"年份": "年", "月份": "月", "小时": "小时"}

#: 「按时段 / 按峰平谷汇总」里的峰/平/谷是**分组值**、不是三个并列指标（实测 q030 的坑）
_PERIOD_GROUP_RE = re.compile(r"按[^，。？]{0,10}(时段|峰[、和]?\s*平[、和]?\s*谷)|按峰平谷|峰平谷时段")


def extract_dimension(text: str) -> tuple[str | None, str]:
    """抽分组维度。返回 `(dimension, 命中原因)`；`dimension=None` 表示"识别不出/有冲突"。

    两种"识别不出"都记 `None`：① 一条都没命中（无分组语义）；
    ② **命中多条且维度不同**（如"按年按月"）→ 规则吃不准，交 LLM。
    """
    hits: list[str] = []
    for name, pattern in DIMENSION_RULES:
        if re.search(pattern, text) and name not in hits:
            hits.append(name)
    if not hits:
        return None, "无分组语义"
    if len(hits) > 1:
        return None, "维度冲突(" + "、".join(hits) + ")"
    return hits[0], "维度=" + hits[0]


# ══════════════════════════════════════════════════════════════════════════
#  ④ 指标抽取加固
# ══════════════════════════════════════════════════════════════════════════

#: `METRIC_SYNONYMS` 里"平均功率"的裸词，会误吃「最大功率 / 峰值功率」
_BARE_POWER_KEYWORD = "功率"
#: 题面**明写**这几个词时，按显式指标把「总用电量」计入（加固三）
_EXPLICIT_TOTAL = ("总用电", "总电量", "合计用电")


def guess_metric_v2(text: str) -> str | None:
    """在 `slot_service.guess_metric_from_question` 之上做 6 处加固。

    加固全部来自实测：`METRIC_SYNONYMS` 里几个**裸字**关键词（`功率` / `峰` / `天`）在复合问句里
    会把无关指标拖进来（实测 q054/q061/q063/q077/q081-q085 都因此产出多余指标列）。
    **顺序有讲究**（加固三必须排在四/五之前），改动前先看评估报告。
    """
    metrics = ss.guess_metric_from_question(text)
    if not metrics:
        return None
    hits = metrics.split("、")
    # 加固一：出现「最大功率 / 峰值功率」时，裸词 `功率` 不再追加「平均功率」
    if "最大功率" in hits and "平均功率" in hits:
        if _BARE_POWER_KEYWORD in text and "平均功率" not in text:
            hits = [h for h in hits if h != "平均功率"]
    # 加固二：出现功率类指标时，裸字 `峰` 拖进来的「峰段用电量」要撤掉
    #         （实测 q061「各年度的峰值功率」被拖成"最大功率、峰段用电量"）
    if ("最大功率" in hits or "平均功率" in hits) and "峰段用电量" in hits:
        if "峰段" not in text and "峰电量" not in text and "峰时" not in text:
            hits = [h for h in hits if h != "峰段用电量"]
    # 加固三：题面明写「总用电量 / 总电量」→ 按**显式指标**计入（泛化词兜底在"已有具体指标"时
    #         不生效，会把「全期总用电量**和**最大功率分别是多少」的总用电量丢掉）。
    #         必须排在加固四/五**之前**：先补齐并列指标，才能判断「异常日」是不是真的单独成列。
    if any(word in text for word in _EXPLICIT_TOTAL) and "总用电量" not in hits:
        hits = ["总用电量", *hits]
    # 加固四：「被判定为异常的天数」里的"多少天/天数"与「异常日」是同一件事，
    #         同时保留会多出一列（实测 q063/q081-q085 的 gold 只有一列）
    if "异常日" in hits and "记录天数" in hits:
        hits = [h for h in hits if h != "记录天数"]
    # 加固五：「异常日的总用电量」里的「异常日」是**过滤条件**而非并列指标
    #         （gold 是 SUM(consumption_kwh) WHERE is_anomaly=1，只有一列）
    if "异常日" in hits and len(hits) > 1:
        hits = [h for h in hits if h != "异常日"]
    # 加固六：同上，但题面只有「异常日」一个具体指标、**泛化词（用电量/电量）才是真指标**
    #         （实测 q078「异常日的平均用电量」、q086-q090「异常日的用电量合计」
    #          —— gold 都是 consumption_kwh 的聚合，「异常日」只出现在 WHERE 里）
    if hits == ["异常日"] and any(word in text for word in ss.GENERIC_METRIC_KEYWORDS):
        hits = [ss.GENERIC_METRIC]
    return "、".join(hits) if hits else None


# ══════════════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════════════


def _blocked(text: str) -> str | None:
    """命中哪一类 block（返回类别名）；未命中返回 None。"""
    for name, pattern in BLOCK_RULES.items():
        if re.search(pattern, text):
            return name
    return None


def _special_time_range(text: str, base: dict[str, Any]) -> str | None:
    """时间范围（与 v1 同口径：年月 > 年 > 「全期」明说 > 会话继承）。"""
    month_match = _MONTH_RANGE_RE.search(text)
    year_match = _YEAR_RE.search(text)
    if month_match:
        return f"{month_match.group(1)}-{int(month_match.group(2)):02d}"
    if year_match:
        return year_match.group(1)
    if ss.DEFAULT_TIME_RANGE in text or not base.get("time_range"):
        return ss.DEFAULT_TIME_RANGE
    return None  # 会话已有时间范围且本轮没提 → 留空继承


def fast_slot(question: str, current_slot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """**规则快通道 v2**：能完整表达就产出增量 slot，否则 `None`（交 LLM）。

    返回形状与 `slot_service.fast_slot` / `fallback_slot` **完全同构**（增量，非完整 slot），
    可直接替换调用点做 A/B。仅在 `SLOT_FASTPATH_V2=true` 时被 `slot_service.fast_slot` 分流到这里。

    不放行的四类（对应 v1 block 词的合理内核，逐条有实测依据）：见 `BLOCK_RULES`。
    另加一条**兜底守卫**：题面出现分组/汇总语义（`按|各|每|逐|汇总|分组|分别`）而维度识别不出或冲突
    → 交 LLM（不猜）。
    """
    text = (question or "").strip()
    if not text:
        return None
    base = current_slot or {}

    if _blocked(text):
        return None

    metric = guess_metric_v2(text)
    if not metric:
        return None  # 认不出指标 → 与 v1 一致，交 LLM（不编造）

    dimension, _reason = extract_dimension(text)
    # 「按时段 / 按峰平谷汇总」：分组值被误当成三个并列指标 → 收敛回泛化指标
    # （老路径 `guess_metric_from_question` 会给"峰段用电量、平段用电量、谷段用电量"；
    #   这里按**未加固**的原始结果判断，正是为了识别这种"三指标其实是分组值"的形态。）
    if dimension == "时段" and _PERIOD_GROUP_RE.search(text):
        raw_hits = (ss.guess_metric_from_question(text) or metric).split("、")
        if raw_hits and all("段用电量" in h for h in raw_hits):
            metric = ss.GENERIC_METRIC

    # 有分组语义但维度识别不出（或冲突）→ 交 LLM
    if re.search(r"按|各|每|逐|汇总|分组|分别", text) and dimension is None:
        return None

    time_range = _special_time_range(text, base)
    granularity = (
        _GRANULARITY_BY_DIMENSION.get(dimension or "") if dimension else None
    ) or (
        ("全期" if time_range == ss.DEFAULT_TIME_RANGE else None)
        or ("年" if time_range and len(time_range) == 4 else None)
        or ("月" if time_range and len(time_range) == 7 else None)
    )
    return {
        "time_range": time_range,
        "granularity": granularity,
        "metric": metric,
        "dimension": dimension,
        "intent": "查数",
        # 单值题给「表格」是对的；**分组题不要瞎猜** —— 交形状兜底
        # （`chart_service._choose_chart_type` 优先用它，`_fallback_chart_type` 兜底），
        # 否则「按月份汇总总用电量」这类序列题会被固定成表格（实测 LLM 给的是柱状）。
        "chart_hint": "表格" if dimension is None else None,
    }
