"""评测模块的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 9/10 节）。

两张表的分工（交付物 8 定案，TBD6 结论：**不拆 `eval_run_items`**）：
- `eval_questions`：题库（120 题，含 gold_sql / gold 答案 / 关键数值 / 陷阱标记）；
- `eval_runs`：评测批次与指标汇总，**逐题明细以 JSON 存 `result_json`**
  （按题型/难度的聚合在 service 内读 JSON 完成，不依赖 SQL 分组）。
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class EvalQuestion(Base):
    __tablename__ = "eval_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: stat / agg / compare / anomaly / structure / multi_turn
    qtype: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    #: 目标报表物理表名（跑批时据此激活会话活跃报表；多轮题的每一轮都用它）
    report_table: Mapped[str] = mapped_column(Text, nullable=False)
    gold_sql: Mapped[str | None] = mapped_column(Text)
    gold_code: Mapped[str | None] = mapped_column(Text)
    #: `{"columns": [...], "rows": [...], "row_count": n}`（对真实库执行 gold_sql 得到）
    gold_answer_json: Mapped[str | None] = mapped_column(Text)
    #: `[{"label", "value", "unit", "kind": number|label}]`（结论准确性判分用）
    key_numbers_json: Mapped[str | None] = mapped_column(Text)
    is_trap: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: R1 量纲 / R3 正交 / R6 比率 / R7 全期行
    trap_rule: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)
    sort_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)
    #: 真实模式 = 百炼模型名；自检模式 = `scripted`
    model_name: Mapped[str | None] = mapped_column(Text)
    temperature: Mapped[float | None] = mapped_column(Float, default=0)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    question_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exec_correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exec_correct_rate: Mapped[float | None] = mapped_column(Float)
    narrative_hit_rate: Mapped[float | None] = mapped_column(Float)
    avg_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    p95_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    #: 单次 POST 调用无流式首字时间，本版本恒 NULL（见 PRD F5.3 实现注记）
    first_token_avg_ms: Mapped[int | None] = mapped_column(Integer)
    retry_total: Mapped[int | None] = mapped_column(Integer, default=0)
    #: 逐题明细（题目/生成 SQL 或代码/答案/对错/diff_type/耗时/错误码/命中判分档）
    result_json: Mapped[str | None] = mapped_column(Text)
    report_path: Mapped[str | None] = mapped_column(Text)
    #: pending / running / finished / failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
