# -*- coding: utf-8 -*-
"""规则快通道 **v2**（A 方案）的落地守卫（零额度、离线、不联网、不调模型）。

守的是「`SLOT_FASTPATH_V2` 这一个开关能被安全地打开/关上」：

| 用例 | 守的是什么 |
| --- | --- |
| `test_switch_defaults_on` | 交付即得 v2 口径（默认开；置 `false` 可一键回退） |
| `test_switch_routes_to_v1_when_off` / `..._to_v2_when_on` | 同一个入口 `slot_service.fast_slot()` 两态分流 |
| `test_v2_is_strict_superset_of_v1` | **零回退**：v1 能接管的 v2 一定也能（19 ⊆ 66） |
| `test_coverage_by_question_type` | 覆盖面读数与 `_rules_proto_eval.md` §2 逐题型一致（含 2026-10-19 补词） |
| `test_ablation_matrix` | 收益归因可复现：19 / 62 / 34 / 66（§4） |
| `test_generic_condition_keywords` | 「用电情况 / 用电状况」兜底：3 正例 + 2 反例（q107 修复的落地守卫） |
| `test_ablation_multi_metric_rounds` | 多指标放行贡献的正是 4 轮（q030/q036/q037/q038） |
| `test_metric_hardening_removes_extra_columns` | 指标加固的价值：去掉 8 处**多余指标列** |
| `test_granularity_stays_within_slot_system_domain` | 伴随边界①：粒度不越 `SLOT_SYSTEM` 值域 |
| `test_chart_hint_leaves_grouping_to_fallback` | 伴随边界②：分组题不把 `chart_hint` 猜死 |
| `test_dimension_never_invented_against_gold` | 最危险的失败模式：凭空造维度（§3.4，应为 0） |
| `test_representative_examples` | §3.3 的实例逐题固定（防"顺手改坏"） |

口径与 `_rules_proto_eval.py` **同一个**：题库 120 题按换行拆轮 → 135 轮；多轮题首轮
`slot_base={}`，其后继承上一轮槽位（继承用的是"被测版本自己"产出的完整 slot，否则读数不可复现）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import pytest

from app.core.config import DATA_DIR, Settings
from app.services import slot_rules_v2 as v2
from app.services import slot_service as ss

#: 题库快照（仓库产物，与 `test_eval.py` 同一依赖；文件缺失时整体跳过而非报红）
QUESTIONS_JSON = DATA_DIR / "eval" / "eval_questions.json"

pytestmark = pytest.mark.skipif(
    not QUESTIONS_JSON.is_file(),
    reason="题库资产缺失（02.data/eval/eval_questions.json）",
)

#: `(问题, 会话已有 slot) -> 增量 slot 或 None`
Adopter = Callable[[str, dict[str, Any]], dict[str, Any] | None]

#: `prompts.SLOT_SYSTEM` 写死的粒度值域 —— 规则**不得**越界（越界会被图表端点当非法输入）
GRANULARITY_DOMAIN = (None, "小时", "日", "月", "年", "全期")

#: 生产版加固实现（**模块导入期**捕获一次，供消融用例拿"加固前/后"做对照）
_HARDENED = v2.guess_metric_v2
#: 生产版结构化 block 判定（同上：消融用例要在被 patch 掉之后仍能拿到"原版"）
_ORIG_BLOCKED = v2._blocked


# ══════════════════════════════════════════════════════════════════════════
#  题库展开与覆盖统计（复刻评估脚本的口径）
# ══════════════════════════════════════════════════════════════════════════


def _items() -> list[dict[str, Any]]:
    return json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))["questions"]


def _rounds_of(item: dict[str, Any]) -> list[str]:
    """多轮题按换行拆轮（评测工具口径）。"""
    return [line.strip() for line in (item["question"] or "").splitlines() if line.strip()]


def _v1(question: str, base: dict[str, Any]) -> dict[str, Any] | None:
    """v1 快通道的原始实现（绕过开关，用于做"零回退"对照）。"""
    return ss._rule_increment(question, base, strict=True)


def _coverage(adopter: Adopter) -> dict[str, tuple[str, dict[str, Any]]]:
    """跑一遍 135 轮，返回 `{"q001r1": (题面, 增量)}`（只收接管的轮次）。"""
    hits: dict[str, tuple[str, dict[str, Any]]] = {}
    for item in _items():
        texts = _rounds_of(item)
        base: dict[str, Any] = {}
        for index, text in enumerate(texts):
            increment = adopter(text, base)
            if increment is not None:
                hits[f"{item['question_code']}r{index + 1}"] = (text, increment)
            # 多轮题：首轮结束即把"该版本自己"产出的完整槽位作为第二轮基线
            if item["qtype"] == "multi_turn" and index == 0:
                first = adopter(text, {})
                base = ss.normalize_slot(ss.merge_slot({}, first)) if first else {}
    return hits


def _by_qtype(hits: dict[str, tuple[str, dict[str, Any]]]) -> dict[str, int]:
    """按题型归类命中轮次（题号 → qtype 查库）；**6 个题型全部出现在结果里**（缺的记 0）。"""
    qtype = {item["question_code"]: item["qtype"] for item in _items()}
    counter: dict[str, int] = {kind: 0 for kind in set(qtype.values())}
    for key in hits:
        counter[qtype[key.split("r")[0]]] += 1
    return counter


def _metric_set(metric: str | None) -> set[str]:
    return {part for part in (metric or "").split("、") if part}


class _Flag:
    """极简单例替身：`fast_slot` 的分流只读 `slot_fastpath_v2` 一个字段。"""

    def __init__(self, on: bool) -> None:
        self.slot_fastpath_v2 = on


# ══════════════════════════════════════════════════════════════════════════
#  ① 开关本身
# ══════════════════════════════════════════════════════════════════════════


def test_switch_defaults_on() -> None:
    """**默认开**（2026-10-16 真模型评测确认后转正）：交付即得 v2 口径；置 `false` 一键回退。

    断言的是**字段默认值**、不是 `get_settings().slot_fastpath_v2`：后者受 `.env` 影响
    （本地可能被显式关掉做回退对照），不该把用例绑上去。
    """
    assert Settings.model_fields["slot_fastpath_v2"].default is True


def test_switch_routes_to_v1_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """关 → 与 v1 逐轮等价（不是"差不多"，是同一个集合）。"""
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Flag(False))
    switched = _coverage(ss.fast_slot)
    assert set(switched) == set(_coverage(_v1))
    assert len(switched) == 19


def test_switch_routes_to_v2_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """开 → 分流到 v2，且覆盖 66 轮。"""
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Flag(True))
    assert set(_coverage(ss.fast_slot)) == set(_coverage(v2.fast_slot))
    assert len(_coverage(ss.fast_slot)) == 66


# ══════════════════════════════════════════════════════════════════════════
#  ② 覆盖面：零回退 + 逐题型读数
# ══════════════════════════════════════════════════════════════════════════


def test_v2_is_strict_superset_of_v1() -> None:
    """**零回退**：v1 能接管的题 v2 一律仍接管（这是敢开开关的前提）。"""
    old, new = _coverage(_v1), _coverage(v2.fast_slot)
    assert len(old) == 19
    assert len(new) == 66
    assert set(old) <= set(new), f"v1 有、v2 丢了：{sorted(set(old) - set(new))}"
    assert len(set(new) - set(old)) == 47


def test_coverage_by_question_type() -> None:
    """逐题型读数（`_rules_proto_eval.md` §2 的表：135 轮 → v1 19 / v2 66）。"""
    old = _by_qtype(_coverage(_v1))
    new = _by_qtype(_coverage(v2.fast_slot))
    rounds = {"stat": 25, "agg": 25, "compare": 20, "anomaly": 20, "structure": 15, "multi_turn": 30}
    assert sum(rounds.values()) == 135
    assert new == {"stat": 11, "agg": 21, "compare": 5, "anomaly": 17, "structure": 2, "multi_turn": 10}
    assert old == {"stat": 11, "agg": 0, "compare": 0, "anomaly": 2, "structure": 2, "multi_turn": 4}


#: 「XX 的用电情况」兜底真正救下的 3 轮（同一族题面：2007 / 2008 / 2009）
_CONDITION_ROUNDS = ("q106r1", "q107r1", "q108r1")


def test_generic_condition_keywords() -> None:
    """**补词守卫**（2026-10-19，q107 修复）：口语化的「用电情况 / 用电状况」也必须认得出指标。

    病灶（实测，不是推测）：q107「按月看看 2008 年的用电情况。」规则已经认出时间（2008）与分组
    （月份），却认不出**指标** —— 题面既无"用电量"也无"总用电" ⇒ 交 LLM ⇒ LLM 槽位解析超时
    （`E_LLM`，两轮各 20 s）⇒ 整题失败，还被 `_attribute` 误记成「slot 继承错误」。
    补词后这 3 轮由规则直接接管，**不再给"超时"留出发生的机会**。
    """
    # ① 词面兜底本身命中（"用电情况"三个字不含任何具体指标词）
    assert ss.guess_metric_from_question("按月看看 2008 年的用电情况。") == "总用电量"
    assert ss.guess_metric_from_question("2023 年 12 月的用电状况如何") == "总用电量"

    # ② 端到端：3 轮都被 v2 接管，槽位口径与 gold 一致（gold 是 GROUP BY month）
    hits = _coverage(v2.fast_slot)
    for key in _CONDITION_ROUNDS:
        assert key in hits, f"{key} 未被接管（q107 会回到「交 LLM 等超时」那条路）"
        _text, increment = hits[key]
        assert increment["metric"] == "总用电量"
        assert increment["granularity"] == "月"
        assert increment["dimension"] == "月份"
        assert re.fullmatch(r"20\d{2}", increment["time_range"] or ""), increment["time_range"]

    # ③ **反例**（补词不许扩大误伤）：分组值本身是并列指标、或分不出维度 → 仍交 LLM
    assert v2.fast_slot("各用途的用电情况", None) is None
    assert v2.fast_slot("峰段和谷段的用电情况分别是多少？", None) is None
    # 注意：反例挡的是"接管"，不是"词面兜底"——兜底照样给得出指标，判分口径不因补词而变
    assert ss.guess_metric_from_question("各用途的用电情况") == "总用电量"


# ══════════════════════════════════════════════════════════════════════════
#  ③ 收益归因（消融矩阵）
# ══════════════════════════════════════════════════════════════════════════


def _legacy_blocked(text: str) -> str | None:
    """消融用：把"结构化 block"退回 v1 的**裸子串**词表（`FASTPATH_BLOCK_WORDS`）。

    复刻原型 `relax_blocks=False` 的语义 —— 结构化判定照旧生效，只是**再加**一层裸词判定。
    （用模块导入期捕获的 `_ORIG_BLOCKED`，否则 `v2._blocked` 已被 patch 成自己 → 无限递归。）
    """
    if _ORIG_BLOCKED(text):
        return "结构化"
    return "v1裸词" if any(w in text.lower() for w in ss.FASTPATH_BLOCK_WORDS) else None


def _reject_multi(text: str) -> str | None:
    """消融用：多指标一律拒绝（v1 的 strict 行为）。"""
    metric = _HARDENED(text)
    return None if (metric and "、" in metric) else metric


def _adopt_without_multi(question: str, base: dict[str, Any]) -> dict[str, Any] | None:
    """`allow_multi=False` 的接管器（等价于把 `guess_metric_v2` 换成 `_reject_multi`）。"""
    if _reject_multi(question) is None:
        return None
    return v2.fast_slot(question, base)


def test_ablation_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """四个开关逐项关掉 → 19 / 62 / 34 / 66，与 `_rules_proto_eval.md` §4 一致。

    关键结论（别被数字骗了）：**闸门是"结构化 block"** —— 只放宽 block 词、不抽维度 = 19；
    反过来只抽维度、不放宽 block 词 = 34，也到不了 66；两者必须一起上。
    指标词表加固贡献 **0 覆盖**，它的价值在"少给一列"（见下一个用例）。
    """
    assert len(_coverage(v2.fast_slot)) == 66

    with monkeypatch.context() as mp:
        mp.setattr(v2, "_blocked", _legacy_blocked)
        assert len(_coverage(v2.fast_slot)) == 19

    with monkeypatch.context() as mp:
        mp.setattr(v2, "guess_metric_v2", _reject_multi)
        assert len(_coverage(v2.fast_slot)) == 62

    with monkeypatch.context() as mp:
        mp.setattr(v2, "extract_dimension", lambda text: (None, "消融"))
        assert len(_coverage(v2.fast_slot)) == 34

    with monkeypatch.context() as mp:
        mp.setattr(v2, "guess_metric_v2", ss.guess_metric_from_question)
        assert len(_coverage(v2.fast_slot)) == 66

    # 关掉开关后 `v2.fast_slot` 必须原样复原（monkeypatch.context 已还原）
    assert v2.guess_metric_v2 is _HARDENED
    assert len(_coverage(v2.fast_slot)) == 66


def test_ablation_multi_metric_rounds() -> None:
    """多指标放行贡献的**正是这 4 轮**（q030 是"峰、平、谷被当成三个指标"的特例）。"""
    full = set(_coverage(v2.fast_slot))
    reduced = set(_coverage(_adopt_without_multi))
    assert reduced <= full
    assert full - reduced == {"q030r1", "q036r1", "q037r1", "q038r1"}


def test_metric_hardening_removes_extra_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉指标加固：覆盖**不变**（66），但 **9 轮**会多出无用的指标列 → 属"质量修复"。

    口径说明：评估报告 §4 写的"修掉 8 处"是**不同题面**口径（`q071`/`q114` 题面相同
    ——「全期有多少天被判定为用电异常？」），按**轮次**数是 9。本用例钉的是轮次口径。
    """
    hardened = _coverage(v2.fast_slot)
    with monkeypatch.context() as mp:
        mp.setattr(v2, "guess_metric_v2", ss.guess_metric_from_question)
        raw = _coverage(v2.fast_slot)

    assert set(hardened) == set(raw)
    extra = {
        key
        for key in hardened
        if _metric_set(hardened[key][1]["metric"]) < _metric_set(raw[key][1]["metric"])
    }
    assert extra == {
        "q061r1", "q063r1", "q071r1",
        "q081r1", "q082r1", "q083r1", "q084r1", "q085r1", "q114r1",
    }


# ══════════════════════════════════════════════════════════════════════════
#  ④ 两条伴随边界（落地检查项，别"顺手"改）
# ══════════════════════════════════════════════════════════════════════════


def test_granularity_stays_within_slot_system_domain() -> None:
    """`granularity` 只在 `小时/日/月/年/全期` 里取，或留 `None`（继承）。

    「星期 / 时段 / 异常原因 / 设备」**不是**时间粒度 —— 若把它们映射成粒度，
    图表端点（`charts.py` 的 `granularity` 查询参数）会拿到值域外的输入。
    """
    bad = [
        (key, increment["granularity"])
        for key, (_text, increment) in _coverage(v2.fast_slot).items()
        if increment["granularity"] not in GRANULARITY_DOMAIN
    ]
    assert bad == []


def test_chart_hint_leaves_grouping_to_fallback() -> None:
    """单值题给「表格」；**分组题留 `None`**，交 `chart_service._fallback_chart_type` 形状兜底。

    否则「按年份汇总总用电量」这类序列题会被固定成表格，而实测 LLM 给的是柱状。
    """
    for key, (_text, increment) in _coverage(v2.fast_slot).items():
        expected = "表格" if increment["dimension"] is None else None
        assert increment["chart_hint"] == expected, key


def test_dimension_never_invented_against_gold() -> None:
    """**凭空造维度 = 0**（`_rules_proto_eval.md` §3.4）。

    判据：规则给了 `dimension`、且该轮 `gold_sql` 可用（多轮题只认末轮）时，gold 必须真的有
    `GROUP BY`。用 sqlglot 解析（与判分器同一个库）。
    """
    sqlglot = pytest.importorskip("sqlglot")

    risk: list[str] = []
    for item in _items():
        texts = _rounds_of(item)
        base: dict[str, Any] = {}
        for index, text in enumerate(texts):
            increment = v2.fast_slot(text, base)
            gold_applicable = item["qtype"] != "multi_turn" or index == len(texts) - 1
            if increment is not None and increment["dimension"] and gold_applicable:
                tree = sqlglot.parse_one(item.get("gold_sql") or "", dialect="sqlite")
                if not tree.args.get("group"):
                    risk.append(f"{item['question_code']}r{index + 1}")
            if item["qtype"] == "multi_turn" and index == 0:
                first = v2.fast_slot(text, {})
                base = ss.normalize_slot(ss.merge_slot({}, first)) if first else {}
    assert risk == []


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ 代表性实例（§3.3）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("question", "metric", "dimension", "granularity", "chart_hint"),
    [
        # 分组值被误当三指标 → 收敛回泛化指标，同时抽到"时段"
        ("按峰、平、谷时段汇总总用电量（小时报）。", "总用电量", "时段", "全期", None),
        # 裸词 `功率` / 裸字 `峰` 的误吃被加固掉
        ("各年度的峰值功率分别是多少？", "最大功率", "年份", "年", None),
        # 多指标放行（v1 一律拒绝）
        ("各年度的厨房用电量与峰段用电量分别是多少？", "厨房用电量、峰段用电量", "年份", "年", None),
        # 「异常日」是过滤条件、不是并列指标
        ("异常日的总用电量是多少？", "总用电量", None, "全期", "表格"),
        ("2008 年异常日的用电量合计是多少？", "总用电量", None, "年", "表格"),
    ],
)
def test_representative_examples(
    question: str,
    metric: str,
    dimension: str | None,
    granularity: str | None,
    chart_hint: str | None,
) -> None:
    increment = v2.fast_slot(question, {})
    assert increment is not None, question
    assert increment["metric"] == metric
    assert increment["dimension"] == dimension
    assert increment["granularity"] == granularity
    assert increment["chart_hint"] == chart_hint


def test_v1_rejects_what_v2_accepts() -> None:
    """两个版本的分歧点（抽查一条，其余由覆盖集合的差集保证）。"""
    question = "各年度的厨房用电量与峰段用电量分别是多少？"
    assert _v1(question, {}) is None
    assert v2.fast_slot(question, {}) is not None
