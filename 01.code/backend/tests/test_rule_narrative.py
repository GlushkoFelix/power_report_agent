"""规则化解读（自动摘要）的用例（2026-10-14）。

背景：需求方明确"**解读是本项目的交付物**"，而供应商慢时段的模型解读会超过 8s 超时
（实测 3/3 被跳过 → 界面无解读）。`analysis_service._rule_narrative()` 用**结果表能直接算出来的事实**
把结论补完整，并如实标注「自动摘要」。

这里守住四条口径：
1. 正文必须**自报家门**（以「自动摘要」开头、结尾说明来源），不能冒充模型输出；
2. 数字**只能来自结果表** —— 用项目自己的 `_unauthorized_numbers()` 反向校验（同一把尺子量自己）；
3. 比率列按**百分比**展示（库里是 0.0621 这种小数）；
4. 空结果 / 截断要如实说明，不编造原因。
"""

from __future__ import annotations

import pytest

from app.services import analysis_service as svc

_COLUMNS = [{"name": "year_month", "type": "TEXT"}, {"name": "total_kwh", "type": "REAL"}]
_TABLE = {
    "columns": _COLUMNS,
    "rows": [
        {"year_month": "2009-01", "total_kwh": 1200.5},
        {"year_month": "2009-02", "total_kwh": 1800.0},
        {"year_month": "2009-03", "total_kwh": 900.25},
    ],
    "row_count": 3,
    "truncated": False,
}


def test_timeseries_summary_has_extremes_and_identifiers() -> None:
    """时序结果：给首末与极值，并带出标识列的值。"""
    narrative = svc._rule_narrative(_TABLE, reason="模型解读超时")
    text = narrative["text"]
    assert text.startswith("自动摘要：")
    assert "1,200" in text and "900.25" in text           # 首 / 末（千分位由 `_fmt_number` 统一）
    assert "1,800" in text and "最高" in text and "最低" in text
    assert "2009-01" in text and "2009-02" in text        # 标识列（哪个月）
    assert "模型解读超时" in text                          # 如实说明生成方式
    assert narrative["auto"] is True


def test_single_row_summary_lists_columns() -> None:
    """单行结果：逐列给值（列名走中文标签词典）。"""
    table = {
        "columns": [{"name": "kitchen_kwh"}, {"name": "laundry_kwh"}],
        "rows": [{"kitchen_kwh": 12.5, "laundry_kwh": 7.25}],
        "row_count": 1,
    }
    text = svc._rule_narrative(table)["text"]
    assert "厨房" in text and "洗衣房" in text             # COLUMN_LABELS 的中文名
    assert "12.5" in text and "7.25" in text


def test_ratio_column_shown_as_percent() -> None:
    """比率列（库里是小数）按百分比展示 —— 与人工核对口径一致。"""
    table = {
        "columns": [{"name": "kitchen_ratio"}],
        "rows": [{"kitchen_ratio": 0.0621}],
        "row_count": 1,
    }
    narrative = svc._rule_narrative(table)
    assert "6.21%" in narrative["text"]
    assert narrative["key_numbers"][0]["value"] == pytest.approx(6.21, abs=0.01)


def test_empty_result_is_stated_honestly() -> None:
    """空结果如实说明，不编造原因。"""
    narrative = svc._rule_narrative(
        {"columns": _COLUMNS, "rows": [], "row_count": 0}, reason="模型解读超时"
    )
    assert "未返回数据" in narrative["text"]
    assert narrative["key_numbers"] == []


def test_truncated_note_is_kept() -> None:
    """截断要如实说明。"""
    table = dict(_TABLE, truncated=True)
    assert "截断" in svc._rule_narrative(table, reason="模型解读超时")["text"]


def test_summary_never_uses_numbers_outside_the_table() -> None:
    """**核心口径**：摘要里的数字必须全部来自结果表 —— 用项目自己的未授权数字检测反向校验。"""
    narrative = svc._rule_narrative(_TABLE, reason="模型解读超时")
    stray = svc._unauthorized_numbers(narrative["text"], _TABLE, narrative["key_numbers"])
    assert stray == [], f"自动摘要出现表外数字：{stray}（说明规则化解读引入了编造）"


def test_thousands_separator_is_not_flagged_as_fabrication() -> None:
    """千分位不应被拆成两个数字而误报（2026-10-14 实测：`1,200` 被拆成 `1` 与 `200`）。"""
    table = {"columns": [{"name": "total_kwh"}], "rows": [{"total_kwh": 1200.5}], "row_count": 1}
    assert svc._unauthorized_numbers("本期合计 1,200.5 kWh", table, []) == []
    # 反向：真正的表外数字仍要被抓出来（修归一化不能放宽这条红线）
    assert svc._unauthorized_numbers("共 25,979 行缺失", table, []) == ["25979"]
