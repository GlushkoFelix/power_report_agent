"""延迟分析工具（`python 01.code/tools/latency_report.py run_xxx [run_yyy ...]`）。

**为什么需要它**：PRD 的 G2/N2 是「单轮平均响应 ≤8s」、G3/N1 是「首字 ≤2s」，但评测报告只给端到端
平均值与 P95，**看不出时间花在哪个阶段**。本工具按 `llm_calls` 把耗时拆到 slot / gen_sql / narrative，
并给出"模型调用占比"，用于定位瓶颈与验证优化效果（优化前后各跑一次同一批题即可对比）。

指标口径：
- 端到端取 `analyses.elapsed_ms`（含本地开销）；
- 分阶段取 `llm_calls.elapsed_ms`（按批次时间窗聚合）；
- 「模型占比」= 分阶段总和 / 端到端总和（>100% 说明端到端里有并行或被截断，正常应接近 100%）。

用法：
    python 01.code/tools/latency_report.py run_20260918_07
    python 01.code/tools/latency_report.py run_20260918_07 run_20260918_03
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "01.code" / "backend"))

from app.core.config import get_settings  # noqa: E402

__all__ = ["report", "main"]


def _pct(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]


def report(run_codes: list[str]) -> None:
    conn = sqlite3.connect(f"file:{get_settings().power_agent_db}?mode=ro", uri=True)
    for run_code in run_codes:
        row = conn.execute(
            "select id, name, model_name, started_at, coalesce(finished_at, datetime('now','localtime')),"
            " question_total, exec_correct_count from eval_runs where run_code = ?",
            (run_code,),
        ).fetchone()
        if row is None:
            print(f"批次不存在：{run_code}")
            continue
        _, name, model, started, finished, total, correct = row
        print(f"\n===== {run_code}｜{name}｜{model} =====")
        print(f"  题量 {total}｜正确 {correct}｜窗口 {started} → {finished}")

        print("\n  == 分阶段（llm_calls）==")
        stage_total = 0
        rows = conn.execute(
            """
            select stage, count(*), cast(avg(elapsed_ms) as int), cast(avg(prompt_tokens) as int),
                   cast(avg(completion_tokens) as int), cast(max(elapsed_ms) as int)
              from llm_calls where created_at >= ? and created_at <= ?
             group by stage order by 3 desc
            """,
            (started, finished),
        ).fetchall()
        for stage, calls, avg_ms, avg_in, avg_out, max_ms in rows:
            stage_total += avg_ms * calls
            print(
                f"    {stage:<10} {calls:>4} 次｜均 {avg_ms:>5}ms｜最大 {max_ms:>6}ms"
                f"｜输入 {avg_in:>5} tok｜输出 {avg_out:>4} tok"
            )

        elapsed = [
            item[0]
            for item in conn.execute(
                "select elapsed_ms from analyses where created_at >= ? and created_at <= ?",
                (started, finished),
            )
        ]
        if elapsed:
            calls = sum(item[1] for item in rows)
            total_ms = sum(elapsed)
            print(
                f"\n  == 端到端（analyses）==\n"
                f"    条数 {len(elapsed)}｜平均 {total_ms // len(elapsed)}ms"
                f"｜P50 {_pct(elapsed, 0.5)}ms｜P95 {_pct(elapsed, 0.95)}ms｜最大 {max(elapsed)}ms"
            )
            print(f"    每问模型调用 {calls / max(len(elapsed), 1):.1f} 次"
                  f"｜模型耗时占端到端 {(stage_total / max(total_ms, 1)) * 100:.0f}%")
            print(f"    G2 目标（平均 ≤8000ms）：{'✅ 达标' if total_ms / len(elapsed) <= 8000 else '❌ 未达标'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="按批次拆解分析链路耗时")
    parser.add_argument("run_codes", nargs="+")
    args = parser.parse_args()
    report(args.run_codes)


if __name__ == "__main__":
    main()
