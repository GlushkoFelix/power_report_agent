"""v1.19.6 真实模型评测迭代的回归测试（三处改动，各对应一类实测错题）。

背景：第一次真实模型 120 题评测只有 52.5%（目标 ≥85%），逐题复盘后定位到三类**可修**问题：

1. **过澄清**（18 题，15%）：模型把「全期平均电压是多少伏？」「峰段、平段、谷段的用电量分别是多少？」
   判成 `missing_slot=metric` 并只返回澄清卡 → 用「指标词面兜底」救场（`slot_service`）；
2. **解读编造**（全量）：解读里援引上传阶段的质量信息（"存在 25979 行整行缺失"）→
   检测"未授权数字"并留痕告警（`analysis_service`）；
3. **年月形态差异**（6 题）：模型用 `STRFTIME('%Y-%m')` 给 `'2006-12'`、gold 用整数 `month=12`，
   值对但被判 `type_mismatch` → 判分增加年月等价（`eval_service`）。
"""

from __future__ import annotations

import asyncio
import time

import anyio
import pytest

from app.services import slot_service
from app.services.analysis_service import _unauthorized_numbers
from app.services.eval_service import _cell_equal, score_result_sets


# ══════════════════════════════════════════════════════════════════════════
#  ① 指标词面兜底（过澄清）
# ══════════════════════════════════════════════════════════════════════════

def test_guess_metric_recognizes_explicit_wordings() -> None:
    """**指标写得清楚就必须认出来**——这些正是实测中"被澄清掉"的原题。"""
    cases = {
        "全期平均电压是多少伏？": "电压",
        "峰段、平段、谷段的用电量分别是多少？": "峰段用电量、平段用电量、谷段用电量",
        "各年度的厨房用电量是多少？": "厨房用电量",
        "全期未计量用电量合计是多少？": "未计量用电量",
        "日报里一共记录了多少天？": "记录天数",
        "全期异常原因分别出现了多少次？": "异常日",
        "平均每天用多少电？": "总用电量",
    }
    for question, expected in cases.items():
        assert slot_service.guess_metric_from_question(question) == expected, question


def test_guess_metric_power_before_period() -> None:
    """「平均功率」不能被裸字 `平` 抢成「平段用电量」（同义词表顺序即优先级）。"""
    assert slot_service.guess_metric_from_question("各小时的平均功率是多少？") == "平均功率"
    assert slot_service.guess_metric_from_question("平段用电量是多少？") == "平段用电量"


def test_guess_metric_minutes_vs_days() -> None:
    """「缺采**分钟**」与「有效采集时间不足的**天数**」不能混：裸词「有效采集」会互相抢（v2.1）。"""
    assert slot_service.guess_metric_from_question("全期一共缺采了多少分钟？") == "缺采分钟"
    assert slot_service.guess_metric_from_question(
        "全期有效采集时间不足自然时长的天数有多少？"
    ) == "记录天数"


def test_guess_metric_coverage_and_load_factor() -> None:
    """覆盖率 / 负载率也必须有词面兜底：实测这两类问题曾因缺词被判"缺指标"而只返回澄清卡。"""
    assert slot_service.guess_metric_from_question("各年度的有效数据覆盖率分别是多少？") == "覆盖率"
    assert slot_service.guess_metric_from_question("各年度的平均负载率分别是多少？") == "负载率"


def test_sql_system_carries_result_shape_rules() -> None:
    """**结果形状铁律必须在系统提示词里**（v3.2）。

    背景：这些约束原先只写在用户消息的注入文本（Schema 说明）里，flash 模型遵从度不足，
    实测 q095/q102/q103/q112/q115 五题都是"形状写坏"（多带过滤列、UNION 长表、附带分子分母）
    而判错。移到 system 角色后遵循度明显不同——**别把它们挪回注入文本**。
    """
    from app.agent import prompts

    for keyword in ("结果形状铁律", "UNION ALL", "只返回比率列", "时间维度题分两类", "各期数值"):
        assert keyword in prompts.SQL_SYSTEM, keyword


def test_sql_system_separates_axis_from_dimension_grouping() -> None:
    """**「按 X 汇总」与「看变化 / 趋势」必须分两类**（v3.6）。

    实测（`run_20260919_04`）：q027 / q033 / q106 三题把「按月份汇总」写成了
    `substr(date, 1, 7) AS year_month`（生成 48 / 24 行 vs gold 12 行）。根因不是模型随机错，
    而是 v3.5 第 14 条**只给了"按月看变化 → 48 行"的例子、没写触发条件**，弱模型把该例子
    套到了"按月份汇总"上。题库口径可佐证：**只有 4 题**（q032 / q111 / q113 / q117，且都是
    追问里继承了趋势意图）需要保留年份，其余 **42 题**是纯维度汇总。

    因此本条钉住：① 维度汇总的正面例子（`GROUP BY month` 得 12 行）、② 趋势题保留年份的写法、
    ③ 判别口诀 —— 三样都在，缺一样模型就可能再退回去。
    """
    from app.agent import prompts

    system = prompts.SQL_SYSTEM
    assert "时间维度题分两类" in system
    assert "GROUP BY month" in system and "12 行" in system  # ① 维度汇总（不带年）
    assert "substr(date, 1, 7)" in system                    # ② 趋势题保留年份
    assert "才保留年份" in system and "只按 X 分组" in system   # ③ 判别口诀
    # ④ 先看目标表粒度（v3.9）：月粒度表本身有 year_month 列 → 「按月」直接用它的行，
    #    不能再跨年合并成 12 行（实测 q111：月报上月汇总被写成 12 行 vs gold 48 行）
    assert "先看目标表的粒度" in system
    assert "year_month" in system and "report_monthly" in system


@pytest.mark.anyio
async def test_stream_run_pushes_first_frame_before_pipeline_ends() -> None:
    """`_stream_run` 必须**边跑边推**（v1.19.11 真流式）——首帧不得等到链路结束。

    为什么直接测生成器而不是走 HTTP：`httpx` 的 ASGITransport **会把整个响应体缓冲下来**，
    进程内的 SSE 测试天然看不到"增量到达"，因此这里直接对生成器断言；
    端到端的首字延迟由 `02.data/tmp/_sse_probe.py`（真实 uvicorn）验证。
    """
    from app.api.v1.endpoints import analysis as analysis_ep
    from app.services.analysis_service import EventSink

    async def runner(push):
        sink = EventSink(push)
        sink.append({"event": "status", "data": {"stage": "parsing"}})
        await anyio.sleep(0.8)  # 模拟慢链路（真实为 3 次 LLM 调用 ≈ 6–9s）
        sink.append({"event": "done", "data": {"record_id": "a_1"}})
        return "ok"

    stream = analysis_ep._stream_run(runner)
    started = time.perf_counter()
    first = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
    elapsed = time.perf_counter() - started

    assert "parsing" in first["data"] and elapsed < 0.5, f"首帧耗时 {elapsed:.2f}s（应远早于链路结束）"
    frames = [frame async for frame in stream]
    assert frames and frames[-1]["event"] == "done"


def test_slot_system_keeps_subject_period_on_comparison_followup() -> None:
    """对比型追问**不得覆盖主体期**（v3.4）。

    实测 q116：第一轮「2009 年的总用电量是多少？」把会话 `time_range` 设为 2009，
    第二轮「比 2008 年变化多少？」槽位把 `time_range` 覆盖成 2008 → 模型算的是 2007→2008 的变化，
    丢了主体期 2009，整题判错。修法：基期写进 `compare`，`time_range` 填 null（主体期靠会话继承）。
    """
    from app.agent import prompts

    assert "对比型追问" in prompts.SLOT_SYSTEM
    assert "绝不要让基期覆盖主体期" in prompts.SLOT_SYSTEM
    assert "主体期与基期都要返回" in prompts.SQL_SYSTEM


def test_slot_system_distinguishes_switch_from_comparison() -> None:
    """「那 X 呢？」是**换主体**，不是对比（v3.7）。

    实测（`run_20260919_06`）：q106 第二轮「那 2008 年呢？」被抽成
    `compare=与 2008 年对比`（配 `time_range=2007`），于是 SQL 返回 2007+2008 两年、判错；
    正确槽位应是 `time_range=2008` + `compare=null`（粒度「月」从会话继承）。
    与 v3.6 修的是**同一种病**：规则给了例子却没定义触发条件 → 弱模型把例子套错场景。

    本题库的两个方向都要保住：q116「比 2008 年变化多少？」= 对比（v3.4 规则），
    q106/q107/q108「那 X 年呢？」= 换主体（v3.7 规则）。
    """
    from app.agent import prompts

    system = prompts.SLOT_SYSTEM
    assert "只有句子里出现明确的对比措辞才算对比" in system
    assert "别把" in system and "换主体" in system
    assert "那 2008 年呢" in system          # 反例（换主体）
    assert "环比" in system and "同比" in system  # 判别口诀里保留对比措辞


def test_fast_slot_hits_simple_metric_questions() -> None:
    """快通道只认**明确且简单**的问数（命中即可省掉一次 LLM 往返 ~1.5–2s）。"""
    hit = slot_service.fast_slot("全期总用电量是多少？")
    assert hit and hit["metric"] == "总用电量"
    assert hit["time_range"] == "全期" and hit["granularity"] == "全期"

    year = slot_service.fast_slot("2006 年的总用电量是多少？")
    assert year and year["time_range"] == "2006" and year["granularity"] == "年"

    month = slot_service.fast_slot("2006 年 8 月的总用电量是多少？")
    assert month and month["time_range"] == "2006-08" and month["granularity"] == "月"

    # 会话已有时间范围、本轮没提 → 时间与粒度**一起留空继承**（不能想当然填「全期」）
    inherit = slot_service.fast_slot("总用电量是多少？", {"time_range": "2007", "granularity": "年"})
    assert inherit and inherit["time_range"] is None and inherit["granularity"] is None


def test_slot_system_keeps_ratio_semantics_on_dimension_switch() -> None:
    """占比语义要抽成比率指标，且**换维度的追问必须继承主体语义**（v3.8）。

    实测 q115（120 题里唯一错题）：第一轮「全期各用途用电量的占比是多少？」、
    第二轮「**按时段再看一遍。**」——第二轮 `intent` 掉成 `查数`、`metric` 掉成 `总用电量`，
    于是 SQL 返回 `SUM(peak_kwh), SUM(flat_kwh), SUM(valley_kwh)` 三个**绝对量**，
    而 gold 要的是 `peak_ratio, flat_ratio, valley_ratio`（R6 红线：比率必须分子分母重算）。
    第二轮问题里**没有"占比"二字**，SQL 层无从补救 ⇒ 只能在槽位层继承语义。
    """
    from app.agent import prompts

    system = prompts.SLOT_SYSTEM
    assert "比率指标" in system                    # ① 占比抽成比率指标
    assert "峰段占比" in system
    assert "换维度的追问必须继承上一轮的主体语义" in system  # ② 追问继承
    assert "不要退回 `查数`" in system
    for word in ("占比", "趋势", "排行"):           # 需要继承的语义种类都点到
        assert word in system


def test_fallback_slot_accepts_multi_metric_but_fastpath_does_not() -> None:
    """兜底允许**多指标**，快通道仍拒绝（v1.19.20 修正）。

    实测（`run_20260919_11/12/13`，kimi-k3）：5 题因 slot 调用超 6s 走兜底，
    而兜底当时"多指标一律拒绝" → 直接判 `E_LLM` 整题失败；其中
    「峰段、平段、谷段的用电量分别是多少？」的指标**词面本来就能识别**（三指标），
    且 SQL 路径**本来就产出宽表**（`SQL_SYSTEM` 第 11 条），把它判成失败是用"唯一性"换"整题作废"。
    """
    question = "峰段、平段、谷段的用电量分别是多少？"
    fallback = slot_service.fallback_slot(question, {})
    assert fallback is not None, "兜底不该把词面可辨的多指标判成失败"
    assert fallback["metric"] == "峰段用电量、平段用电量、谷段用电量"

    # 快通道仍拒绝多指标（它要"高置信、不出错"，多指标交 LLM 分别处理更稳）
    assert slot_service.fast_slot(question, {}) is None


def test_fast_slot_rejects_complex_questions() -> None:
    """对比 / 分组 / 占比 / 过滤 / 多指标 —— 一律交 LLM（快通道不冒险）。"""
    for question in (
        "比 2008 年变化多少？",
        "按月份汇总全期的总用电量。",
        "各年度的厨房用电量分别是多少？",
        "峰段、平段、谷段的用电量分别是多少？",
        "全期峰段用电量占总用电量的比例是多少？",
        "异常原因分别出现了多少次？",
        "全期日均电压最低的那天是哪天，电压是多少？",
        "全期有效数据覆盖率分别是多少？",
        "帮我看看数据",  # 没有指标语义 → 交 LLM（会走澄清）
    ):
        assert slot_service.fast_slot(question) is None, question


def test_fast_slot_switch_off(monkeypatch) -> None:
    """开关关掉即完全回到旧行为（A/B 与回归用）。"""
    monkeypatch.setattr(slot_service, "FASTPATH_ENABLED", False)
    assert slot_service.fast_slot("全期总用电量是多少？") is None


def test_guess_metric_returns_none_without_metric_words() -> None:
    """完全没有指标语义 → 不猜（让澄清照常发生，这是澄清功能该发挥作用的地方）。"""
    assert slot_service.guess_metric_from_question("帮我看看数据") is None
    assert slot_service.guess_metric_from_question("") is None


def test_guess_metric_recognizes_row_count_questions() -> None:
    """「表里有多少行 / 多少条记录」也是可执行的问题（`COUNT(*)`），必须有词面兜底（v1.19.26）。

    实测 **q096「结构报表一共有多少行（含全期合计行）？」跨模型两批都错**
    （v3.5 × `deepseek-v4-flash`、v3.9 × `qwen3.8-max-0902`），现象完全一致：
    模型没给 metric → 规则判"指标缺失" → **澄清轮**（澄清轮不落 analyses）→
    评测端看不到结果，被判 `empty_result`。翻库发现历史上这题能过，靠的是**模型自己**
    给了 `metric="记录行数"` —— 也就是说它一直"看模型心情"。补上词面兜底后由规则保证。
    """
    question = "结构报表一共有多少行（含全期合计行）？"
    assert slot_service.guess_metric_from_question(question) == "记录行数"
    # 有词面兜底 → 规则不再发澄清卡
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="metric", merged={}, question=question
    ) is None
    # 快通道也能命中（省掉一次 slot LLM 往返）
    hit = slot_service.fast_slot(question, {})
    assert hit and hit["metric"] == "记录行数" and hit["time_range"] == "全期"
    # 与「记录天数 / 记录月数」同族但不能串味
    assert slot_service.guess_metric_from_question("日报里一共记录了多少天？") == "记录天数"
    assert slot_service.guess_metric_from_question("一共记录了几个月？") == "记录月数"


def test_resolve_clarify_rejects_model_missing_metric_when_words_present() -> None:
    """模型报 `missing_slot=metric`，但问题里已有指标词 → **不采信**、继续执行（过澄清兜底）。"""
    # 模型说"缺指标"，问题其实写了"平均电压"
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="metric", merged={}, question="全期平均电压是多少伏？"
    ) is None
    # 模型说的确实是别的缺失项（如 granularity）→ 照常澄清，规则不越权
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="granularity", merged={"metric": "总用电量"},
        question="按月看用电量",
    ) == "granularity"
    # 问题里没有指标词 → 保持原有行为：澄清
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="metric", merged={}, question="帮我看看数据"
    ) == "metric"
    # 现有条件里已有 metric → 不需要澄清
    assert slot_service.resolve_clarify(
        increment_intent=None, model_missing=None, merged={"metric": "总用电量"}, question="再看看"
    ) is None


def test_clarify_card_metric_options_are_full_list() -> None:
    """澄清卡选项由「前 3 个」改为**全量指标清单**（上限 8）。"""
    columns = [
        {"name": f"c{index}", "type": "REAL", "agg": "sum", "unit": "kWh"} for index in range(10)
    ]
    card = slot_service.build_clarify_card("metric", schema_columns=columns)
    assert len(card["options"]) == 8
    assert card["missing_slot"] == "metric"


# ══════════════════════════════════════════════════════════════════════════
#  ② 解读「未授权数字」检测
# ══════════════════════════════════════════════════════════════════════════

def test_unauthorized_numbers_flags_fabricated_quality_info() -> None:
    """实测编造："数据质量提示显示存在 25979 行整行缺失" —— 数字不在结果里 → 抓出来。"""
    table = {"columns": [{"name": "total_kwh"}], "rows": [{"total_kwh": 37283.7477}], "row_count": 1}
    text = "全期总用电量为 37283.7477 kWh。另外，数据质量提示显示存在 25979 行整行缺失。"
    assert _unauthorized_numbers(text, table, []) == ["25979"]


def test_unauthorized_numbers_allows_rounded_and_result_numbers() -> None:
    """结果里的数字、四舍五入后的写法、以及小计数都不应误报。"""
    table = {
        "columns": [{"name": "avg_kwh"}, {"name": "date"}],
        "rows": [{"avg_kwh": 26.0179676902, "date": "2006-12-23"}],
        "row_count": 1,
    }
    # 26.02 是 26.0179676902 的展示级舍入；2006 来自日期；3 是小计数
    text = "全期日均用电 26.02 kWh，最高出现在 2006 年，共 3 个数据点。"
    assert _unauthorized_numbers(text, table, []) == []


def test_unauthorized_numbers_accepts_key_numbers() -> None:
    """解读自己给出的 key_numbers 也算"授权"（它们是表格里的数，只是被复述）。"""
    table = {"columns": [{"name": "c"}], "rows": [{"c": 1.0}], "row_count": 1}
    keys = [{"label": "合计", "value": 123456.78}]
    assert _unauthorized_numbers("合计 123456.78 kWh", table, keys) == []


# ══════════════════════════════════════════════════════════════════════════
#  ③ 判分：年月形态等价
# ══════════════════════════════════════════════════════════════════════════

def test_cell_equal_month_and_year_forms() -> None:
    """`'2006-12'` ≈ `12`（模型 STRFTIME 写法）且 `'2006'` ≈ `2006`。"""
    assert _cell_equal("2006-12", 12) == (True, False)
    assert _cell_equal(12, "2006-12") == (True, False)
    # 非法月份（13）不套用等价规则；两侧都含数字 → 按"取值不一致"而非"类型冲突"归因
    assert _cell_equal("2006-13", 13) == (False, False)
    assert _cell_equal("2006", 2006) == (True, False)


def test_score_year_month_question_passes() -> None:
    """真实错题 q040 的形态：值一致、只有年/月写法不同 → 应判对（而非 type_mismatch）。"""
    gold = {"columns": ["month", "consumption_kwh"],
            "rows": [{"month": 12, "consumption_kwh": 696.8880333333}]}
    gen = {"columns": ["year_month", "total_consumption_kwh"],
           "rows": [{"year_month": "2006-12", "total_consumption_kwh": 696.89}]}
    result = score_result_sets(gold, gen)
    assert result["is_correct"] and result["tier"] == 1


class _StubQuestion:
    """`_run_question` 需要的字段子集（避免为一条断言去建库）。"""

    question_code = "q_stub"
    question = "全期总用电量是多少？\n按年拆开看看。"
    qtype = "multi_turn"
    difficulty = "easy"
    is_trap = 0
    trap_rule = None
    report_table = "report_daily"
    gold_sql = "SELECT 1"
    gold_answer_json = '{"columns": ["c"], "rows": [{"c": 1.0}]}'
    key_numbers_json = "[]"


@pytest.mark.anyio
async def test_eval_first_round_ignores_conversation_slot(monkeypatch) -> None:
    """评测工具缺陷回归（v1.19.6）：单轮题**不得继承**上一题留下的 slot。

    背景：整批共用一个会话做留痕，而 `run_once` 默认把会话 `slot_json` 当继承基线，
    于是 q051 的 `is_weekend = 1`、q074 的 `consumption_kwh = 0` 会串到后面的题上
    （实测 3 题因此判错，**不是模型能力问题**）。修法：首轮显式 `slot_base={}`，
    多轮题第 2 轮起仍继承（F4.2 的会话内继承照测）。
    """
    from app.services import eval_service
    from app.services.analysis_service import AnalysisResult

    seen: list[dict] = []

    async def _fake_run_once(user_id, conversation_code, text, **kwargs):
        seen.append({"text": text, **kwargs})
        # 返回 success：多轮题才会走到第 2 轮，从而断言"第 1 轮不继承、第 2 轮继承"
        return AnalysisResult(
            record_code="", conversation_code=conversation_code, report_code="r_daily",
            table_name="report_daily", question=text, status="success",
        )

    # 打桩入口随编排改造变化（2026-10-13）：评测链路现在经 `dispatch.run_once_dispatch`
    # 分流到 legacy / langgraph 两条编排，所以要在**分发层**打桩，而不是替换 legacy 函数本身
    # （用例意图不变：断言首轮 `slot_base={}`、第 2 轮起继承）。
    monkeypatch.setattr(eval_service.dispatch, "run_once_dispatch", _fake_run_once)
    await eval_service._run_question(
        1, "c_eval_test", _StubQuestion(), {"report_daily": "r_daily"},
        include_narrative=False, mode="real",
    )

    assert [item["slot_base"] for item in seen] == [{}, None], seen


def test_cell_equal_text_with_unit_is_numeric_compare() -> None:
    """带单位的文本按**第一个数值**比较：`'12.5 kWh'` ≈ 12.5；纯文本仍按类型冲突处理。"""
    assert _cell_equal("12.5 kWh", 12.5) == (True, False)
    assert _cell_equal("12.5 kWh", 99.0) == (False, False)
    assert _cell_equal("没有数据", 1.0) == (False, True)
