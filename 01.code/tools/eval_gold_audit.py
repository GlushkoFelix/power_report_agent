"""题库 gold 审计工具（`python 01.code/tools/eval_gold_audit.py`）。

**为什么需要它**：题库的 gold 是"对真实库反算"出来的（`04_build_eval_set.py`），一旦 gold 本身有问题
（口径错、踩 SQL 陷阱），会把**正确回答判成错误**、污染正确率统计，而且极难发现——它表现为"模型答错"。

**它抓什么**：

1. **结果集含 NULL**：SQLite 下 `ORDER BY x ASC` 把 NULL 排最前，于是「最低的那天」会选中**缺失日**
   （实测 `report_daily` 有 9 天是 NULL 行）。本题库据此修了 q004 / q075 / q118 三题；
   但**不是所有含 NULL 的 gold 都错**（如「异常日清单」里的缺失日用电量为 NULL 是事实描述），
   所以本工具**只报告、不自动改**，由人工判断。
2. **gold 不可执行**：列名/表名写错、函数不存在等（这类会让整题永远判错）。
3. **结果为空**：gold 返回 0 行（多为过滤条件写错）。

用法：
    python 01.code/tools/eval_gold_audit.py            # 全量审计（只读）
    python 01.code/tools/eval_gold_audit.py --codes q004,q075   # 只看指定题
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "01.code" / "backend"))

from app.core.config import get_settings  # noqa: E402

__all__ = ["audit", "main"]


def audit(codes: list[str] | None = None) -> int:
    """审计题库 gold；返回**硬错误**题数（gold 不可执行 / 结果为空）。

    区分两类（便于放进 CI）：
    - **硬错误**（返回 >0）→ 这类题永远判错，必须修；
    - **待确认**（含 NULL，返回值不计）→ 可能是真实缺陷（极值题踩 NULL 排序），
      也可能是事实描述（如「异常日清单」里缺失日用电量为 NULL），需人工判断。
    """
    settings = get_settings()
    agent = sqlite3.connect(f"file:{settings.power_agent_db}?mode=ro", uri=True)
    reports = sqlite3.connect(f"file:{settings.reports_db}?mode=ro", uri=True)
    reports.row_factory = sqlite3.Row

    sql = "select question_code, qtype, question, gold_sql from eval_questions"
    params: list[object] = []
    if codes:
        sql += f" where question_code in ({','.join('?' * len(codes))})"
        params = list(codes)
    sql += " order by question_code"
    rows = agent.execute(sql, params).fetchall()

    errors: list[str] = []
    warnings: list[str] = []
    print(f"审计 {len(rows)} 题 gold…\n")
    for code, qtype, question, gold in rows:
        try:
            result = reports.execute(gold).fetchall()
        except sqlite3.Error as exc:
            errors.append(code)
            print(f"[{code} · {qtype}] ❌ gold 不可执行：{exc}")
            print(f"  {question.replace(chr(10), ' / ')}")
            print(f"  {gold}\n")
            continue
        if not result:
            errors.append(code)
            print(f"[{code} · {qtype}] ❌ gold 返回 0 行（检查过滤条件）")
            print(f"  {question.replace(chr(10), ' / ')}\n")
            continue
        null_rows = [dict(row) for row in result if any(value is None for value in tuple(row))]
        if null_rows:
            warnings.append(code)
            print(f"[{code} · {qtype}] ⚠️ 结果含 NULL（{len(null_rows)}/{len(result)} 行）——确认是否踩了 NULL 排序")
            print(f"  {question.replace(chr(10), ' / ')}")
            print(f"  gold: {gold}")
            print(f"  样例: {null_rows[:2]}\n")

    if errors:
        print(f"❌ 硬错误 {len(errors)} 题：{','.join(errors)}")
    if warnings:
        print(f"⚠️ 待人工确认（含 NULL）{len(warnings)} 题：{','.join(warnings)}")
    if not errors and not warnings:
        print("全部干净 ✅")
    return len(errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="题库 gold 审计（NULL / 不可执行 / 空结果）")
    parser.add_argument("--codes", default=None, help="只审计指定题号（逗号分隔）")
    args = parser.parse_args()
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()] or None
    sys.exit(1 if audit(codes) else 0)


if __name__ == "__main__":
    main()
