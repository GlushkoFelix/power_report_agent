"""从中断的评测批次**重建**评分与报告（0 额度）。

为什么需要它
------------
评测批次是**单进程内存态**：逐题结果只在批次收尾时写入 `eval_runs.result_json`
（逐题明细同时落 `analyses` / `llm_calls`）。若批次跑到一半进程被中断
（机器休眠、服务被停、`Ctrl+C`），**聚合与报告会丢**，但**逐题落库还在**——
本工具就用这份落库结果**本地重算判分**（复用 `eval_service` 的同一套判分函数），
把已完成的题目"救回来"，从而**只需补跑剩余题目**，不必整批重跑。

判分口径与在线批次**完全同源**：
- 结果集判分 `score_result_sets(gold, generated)`；结论数字判分 `score_key_numbers(keys, narrative)`；
- `generated` 取该题**最后一轮**的 `result_columns_json` / `result_rows_json`（与 `_run_question` 一致）；
- 唯一差异：`elapsed_ms` 取该题各轮 `analyses.elapsed_ms` 之和（在线批次含轮间开销，差异毫秒级）。

用法
----
    # 先干跑看指标（不写库、不写报告）
    .venv\\Scripts\\python.exe 01.code\\tools\\eval_rebuild.py --from-run run_20261013_03 --dry-run

    # 正式重建（写新批次 + 报告；并把源批次标记为 failed 说明中断）
    .venv\\Scripts\\python.exe 01.code\\tools\\eval_rebuild.py --from-run run_20261013_03 --mark-source-failed

产物：新的 `eval_runs` 行（run_code 自动取当天下一个空号）+ `02.data/eval/reports/<新 run_code>.md`
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "01.code" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.eval_service import (  # noqa: E402
    _aggregate,
    _write_report,
    score_key_numbers,
    score_result_sets,
)

CONTROL_DB = PROJECT_ROOT / "02.data" / "power_agent.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONTROL_DB)
    conn.row_factory = sqlite3.Row
    return conn


def next_run_code(conn: sqlite3.Connection, date_tag: str) -> str:
    """当天下一个空号（`run_<日期>_NN`）。"""
    rows = conn.execute(
        "SELECT run_code FROM eval_runs WHERE run_code LIKE ?", (f"run_{date_tag}_%",)
    ).fetchall()
    used = {int(r["run_code"].rsplit("_", 1)[1]) for r in rows if r["run_code"].rsplit("_", 1)[1].isdigit()}
    n = 1
    while n in used:
        n += 1
    return f"run_{date_tag}_{n:02d}"


def load_bank(conn: sqlite3.Connection) -> dict[str, dict]:
    """题库：question_code → {rounds, gold, keys, qtype, ...}。"""
    bank: dict[str, dict] = {}
    for q in conn.execute("SELECT * FROM eval_questions"):
        rounds = [line.strip() for line in (q["question"] or "").splitlines() if line.strip()]
        bank[q["question_code"]] = {
            "rounds": rounds,
            "gold": json.loads(q["gold_answer_json"] or "{}"),
            "keys": json.loads(q["key_numbers_json"] or "[]"),
            "row": dict(q),
        }
    return bank


def rebuild(conn: sqlite3.Connection, run_code: str) -> tuple[list[dict], dict[str, int]]:
    """按落库结果重建逐题明细；返回（明细列表, 统计）。"""
    run = conn.execute("SELECT * FROM eval_runs WHERE run_code = ?", (run_code,)).fetchone()
    if run is None:
        raise SystemExit(f"找不到批次：{run_code}")
    conv_code = "c_eval_" + run_code[len("run_"):]
    conv = conn.execute(
        "SELECT id FROM conversations WHERE conversation_code = ?", (conv_code,)
    ).fetchone()
    if conv is None:
        raise SystemExit(f"找不到批次会话：{conv_code}")

    bank = load_bank(conn)
    text2q: dict[str, tuple[str, int]] = {}
    for code, info in bank.items():
        for idx, text in enumerate(info["rounds"]):
            text2q.setdefault(text, (code, idx))

    rows = conn.execute(
        "SELECT * FROM analyses WHERE conversation_id = ? ORDER BY id", (conv["id"],)
    ).fetchall()
    by_q: dict[str, dict[int, sqlite3.Row]] = {}
    unmatched = 0
    for row in rows:
        hit = text2q.get((row["question"] or "").strip())
        if not hit:
            unmatched += 1
            continue
        code, idx = hit
        by_q.setdefault(code, {})[idx] = row

    details: list[dict] = []
    for code in sorted(by_q):
        rounds_map = by_q[code]
        info = bank[code]
        if len(rounds_map) < len(info["rounds"]):
            continue  # 轮次不全的题交给"补跑"，不重建
        last = rounds_map[max(rounds_map)]
        columns = [c.get("name") for c in json.loads(last["result_columns_json"] or "[]")]
        generated = {
            "columns": columns,
            "rows": json.loads(last["result_rows_json"] or "[]") if last["status"] == "success" else [],
        }
        verdict = score_result_sets(info["gold"], generated)
        narrative = (last["narrative"] or "")[:500]
        key_score = (
            score_key_numbers(info["keys"], narrative)
            if info["keys"]
            else {"total": len(info["keys"]), "hit": 0, "hit_rate": None, "missed": []}
        )
        details.append({
            "question_code": code,
            "question": info["row"]["question"],
            "qtype": info["row"]["qtype"],
            "difficulty": info["row"]["difficulty"],
            "is_trap": bool(info["row"]["is_trap"]),
            "trap_rule": info["row"]["trap_rule"],
            "gold_sql": info["row"]["gold_sql"],
            "rounds": len(rounds_map),
            "status": last["status"],
            "route": last["route"],
            "generated": "sql" if not last["code_text"] else "pandas",
            "generated_sql_or_code": last["sql_text"] or last["code_text"],
            "is_correct": verdict["is_correct"],
            "matched_tier": verdict["tier"],
            "diff_type": verdict["diff_type"],
            "diff_detail": verdict["detail"],
            "clarify": None,
            "key_hit_rate": key_score["hit_rate"],
            "key_missed": key_score["missed"],
            "elapsed_ms": sum(int(r["elapsed_ms"] or 0) for r in rounds_map.values()),
            "retries": int(last["retries"] or 0),
            "path_switched": bool(last["path_switched"]),
            "error_code": last["error_code"],
            "analysis_ids": [int(r["id"]) for r in rounds_map.values()],
            "narrative": narrative,
        })
    stats = {"analyses": len(rows), "rebuilt": len(details), "unmatched": unmatched}
    return details, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="从中断批次重建评分与报告（0 额度）")
    parser.add_argument("--from-run", required=True, help="被中断的批次 run_code")
    parser.add_argument("--name", default=None, help="新批次备注名")
    parser.add_argument("--dry-run", action="store_true", help="只打印指标，不写库不写报告")
    parser.add_argument("--mark-source-failed", action="store_true", help="把源批次标为 failed 并写明中断原因")
    args = parser.parse_args()

    conn = connect()
    source = conn.execute("SELECT * FROM eval_runs WHERE run_code = ?", (args.from_run,)).fetchone()
    details, stats = rebuild(conn, args.from_run)
    if not details:
        print("没有可重建的题目（轮次不全或没落库）")
        return 2
    metrics = _aggregate(details)
    hit = metrics.get("narrative_hit_rate")
    print(f"源批次 {args.from_run}：analyses {stats['analyses']} 条"
          f"（未匹配 {stats['unmatched']}）→ 重建 {stats['rebuilt']} 题")
    print(f"重建成绩：{metrics['exec_correct_count']}/{metrics['question_total']} = "
          f"{metrics['exec_correct_rate'] * 100:.1f}% ；结论命中率 "
          f"{'—' if hit is None else f'{hit * 100:.1f}%'}"
          f" ；avg/p95 {metrics['avg_elapsed_ms']}/{metrics['p95_elapsed_ms']} ms；重试 {metrics['retry_total']}")
    print("按题型：", {k: f"{v['correct']}/{v['total']}" for k, v in metrics["by_type"].items()})

    if args.dry_run:
        print("（--dry-run：未写库、未写报告）")
        return 0

    date_tag = args.from_run.split("_")[1]
    new_code = next_run_code(conn, date_tag)
    report_path = _write_report(new_code, "real", metrics, details)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO eval_runs (run_code, user_id, name, model_name, temperature, prompt_version,
               schema_version, question_total, exec_correct_count, exec_correct_rate,
               narrative_hit_rate, avg_elapsed_ms, p95_elapsed_ms, retry_total, result_json,
               report_path, status, started_at, finished_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'finished', ?, ?, ?)""",
        (new_code, source["user_id"], args.name or f"{args.from_run} 已完成题重建",
         source["model_name"], source["temperature"], source["prompt_version"],
         source["schema_version"], metrics["question_total"], metrics["exec_correct_count"],
         metrics["exec_correct_rate"], metrics.get("narrative_hit_rate"),
         metrics["avg_elapsed_ms"], metrics["p95_elapsed_ms"], metrics["retry_total"],
         json.dumps({"impl": "real", "metrics": metrics, "questions": details},
                    ensure_ascii=False),
         report_path, source["started_at"], now, now),
    )
    # 把源批次的逐题 llm_calls 关联到新批次（在线批次收尾时做的那一步，这里补上）
    analysis_ids = [aid for item in details for aid in item["analysis_ids"]]
    placeholders = ",".join("?" for _ in analysis_ids)
    conn.execute(
        f"UPDATE llm_calls SET eval_run_id = (SELECT id FROM eval_runs WHERE run_code = ?) "
        f"WHERE analysis_id IN ({placeholders}) AND eval_run_id IS NULL",
        [new_code, *analysis_ids],
    )
    if args.mark_source_failed:
        conn.execute(
            "UPDATE eval_runs SET status='failed', finished_at=?, error_message=? WHERE run_code=?",
            (now, f"进程被中断（已完成 {stats['rebuilt']} 题）；结果已由 eval_rebuild.py 重建为 {new_code}",
             args.from_run),
        )
    conn.commit()
    print(f"已写入新批次：{new_code}（报告：{report_path}）")
    if args.mark_source_failed:
        print(f"源批次 {args.from_run} 已标记 failed（保留逐题留痕）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
