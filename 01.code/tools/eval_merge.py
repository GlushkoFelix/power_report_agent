"""评测批次合并报告工具（`python 01.code/tools/eval_merge.py run_a run_b ...`）。

**为什么需要它**：单批 120 题在真实模型下约 60–70 万 tokens，额度紧张时只能**分批跑**
（如 A 批 q001–q060 + B 批 q061–q120）。两个批次同模型、同提示词、同规则时，合并后的
"120 题正确率"才是对外该报的口径。本工具把多个批次的逐题明细（`eval_runs.result_json`）
合并成**一份 Markdown 报告**，指标算法与 `eval_service._aggregate` 一致。

**同题去重（v1.19.20 补）**：**补跑**（把失败题单独再跑一遍）是这个工具最常见的用法之一——
此时同一题会出现在两个批次里。口径定为**取"最后一批"的结果**（即命令行里越靠后的 run_code 越优先，
所以补跑批次写在后面即可），并在报告里如实写明替换了多少题。没有这条规则时会把补跑题重复计数，
"120 题"会被算成 125 题（本项目实测踩到）。

**如实标注**：报告头部写明"本报告由 A/B 批次合并生成（非单批跑出）"，避免与系统单批报告混淆。

用法：
    python 01.code/tools/eval_merge.py run_20260917_10 run_20260917_11
    python 01.code/tools/eval_merge.py run_20260917_10 run_20260917_11 --out 02.data/eval/reports/merged.md
    # 补跑：把失败的 5 题单独跑成 run_fix，然后把它放在最后一个参数位（同题以后者为准）
    python 01.code/tools/eval_merge.py run_a run_b run_fix
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "01.code" / "backend"))

from app.core.config import get_settings  # noqa: E402

__all__ = ["load_items", "merge_items", "render_report", "main"]

QTYPE_ORDER = ("stat", "agg", "compare", "anomaly", "structure", "multi_turn")
DIFFICULTY_ORDER = ("easy", "medium", "hard")


def load_items(
    conn: sqlite3.Connection, run_codes: list[str]
) -> tuple[list[dict], list[dict], list[str]]:
    """读取各批次的逐题明细；返回（合并后明细, 批次元信息, 被补跑替换的题号）。

    **同题去重**：同一 `question_code` 出现在多个批次（补跑）时**取最后一批**的结果，
    并在批次元信息里记下被替换的题号（`replaced`），供报告如实说明。
    """
    items: list[dict] = []
    meta: list[dict] = []
    for run_code in run_codes:
        row = conn.execute(
            "select run_code, name, model_name, prompt_version, status, question_total,"
            " exec_correct_count from eval_runs where run_code = ?",
            (run_code,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"批次不存在：{run_code}")
        raw = conn.execute(
            "select result_json from eval_runs where run_code = ?", (run_code,)
        ).fetchone()[0]
        if not raw:
            raise SystemExit(f"批次 {run_code} 没有逐题明细（可能被中止）")
        payload = json.loads(raw)
        batch_items = payload.get("questions") or []
        for item in batch_items:
            item["_run_code"] = run_code
        items.extend(batch_items)
        meta.append(
            {
                "run_code": row[0], "name": row[1], "model_name": row[2],
                "prompt_version": row[3], "status": row[4],
                "total": row[5], "correct": row[6], "items": len(batch_items),
            }
        )

    # 同题去重（补跑口径）：**后出现的批次覆盖先出现的**，因为命令行里补跑批次写在后面
    deduped: dict[str, dict] = {}
    replaced: list[str] = []
    for item in items:
        code = str(item.get("question_code"))
        if code in deduped:
            replaced.append(code)
        deduped[code] = item
    return list(deduped.values()), meta, replaced


def merge_items(items: list[dict]) -> dict[str, Any]:
    """合并后的指标（口径与 `eval_service._aggregate` 一致）。"""
    total = len(items)
    correct = sum(1 for item in items if item.get("is_correct"))
    elapsed = [item.get("elapsed_ms") or 0 for item in items]
    elapsed_sorted = sorted(elapsed)
    p95 = elapsed_sorted[min(len(elapsed_sorted) - 1, int(len(elapsed_sorted) * 0.95))] if elapsed else 0

    def rate(bucket: list[dict]) -> dict[str, Any]:
        hit = sum(1 for item in bucket if item.get("is_correct"))
        return {"total": len(bucket), "correct": hit, "rate": hit / len(bucket) if bucket else None}

    by_qtype = {key: rate([i for i in items if i.get("qtype") == key]) for key in QTYPE_ORDER}
    by_difficulty = {
        key: rate([i for i in items if i.get("difficulty") == key]) for key in DIFFICULTY_ORDER
    }
    traps = [i for i in items if i.get("is_trap")]
    rules = sorted({str(i.get("trap_rule")) for i in traps if i.get("trap_rule")})
    keys = [i.get("key_hit_rate") for i in items if i.get("key_hit_rate") is not None]
    return {
        "total": total,
        "correct": correct,
        "exec_rate": correct / total if total else 0.0,
        "key_rate": (sum(keys) / len(keys)) if keys else None,
        "avg_elapsed": int(sum(elapsed) / len(elapsed)) if elapsed else 0,
        "p95_elapsed": p95,
        "retries": sum(int(i.get("retries") or 0) for i in items),
        "by_qtype": by_qtype,
        "by_difficulty": by_difficulty,
        "traps": {**rate(traps), "rules": rules},
        "diff_types": Counter(i.get("diff_type") for i in items if not i.get("is_correct")),
        "errors": Counter(i.get("error_code") for i in items if i.get("error_code")),
        "tiers": Counter(i.get("matched_tier") for i in items if i.get("matched_tier")),
        "failed": [i for i in items if not i.get("is_correct")],
    }


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def render_report(
    meta: list[dict], items: list[dict], metrics: dict[str, Any], replaced: list[str] | None = None
) -> str:
    models = sorted({str(m["model_name"]) for m in meta})
    prompts = sorted({str(m["prompt_version"]) for m in meta})
    lines = [
        "# 评测报告（批次合并） · " + " + ".join(m["run_code"] for m in meta),
        "",
        "> **本报告由多个批次合并生成**（非单批跑出）：单批 120 题在真实模型下约需 **86–113 万 tokens**",
        "> （取决于模型档位：实测 deepseek-v4-flash 957,305 / kimi-k3 995,533 / qwen-max 族 ≈113 万），",
        "> 额度受限时分两批执行（如 q001–q060 / q061–q120），本报告把两批逐题明细合并为对外口径。",
        "> 工具：`01.code/tools/eval_merge.py`；指标算法与 `eval_service._aggregate` 一致。",
        "",
        "- 合并批次："
        + "、".join(f'{m["run_code"]}（{m["correct"]}/{m["total"]}）' for m in meta),
        f"- 模型：{'、'.join(models)}；提示词版本：{'、'.join(prompts)}",
        f"- 题量：{metrics['total']}；执行正确率：**{metrics['exec_rate']:.1%}**"
        f"（{metrics['correct']}/{metrics['total']}，目标 ≥85%）",
        f"- 结论命中率：**{metrics['key_rate']:.1%}**" if metrics["key_rate"] is not None
        else "- 结论命中率：未采集",
        f"- 平均耗时：{metrics['avg_elapsed']} ms；P95 耗时：{metrics['p95_elapsed']} ms（目标 ≤8000 ms）",
        f"- 重试总次数：{metrics['retries']}",
    ]
    if replaced:
        lines.append(
            f"- **补跑替换**：{'、'.join(sorted(replaced))} 共 {len(replaced)} 题—"
            "这些题在多个批次里出现，按**取最后一批**的口径合并（题量仍为去重后的数量）"
        )
    lines += [
        "",
        "## 一、分类正确率",
        "",
        "### 按题型",
        "",
    ]
    lines += _table(
        ["题型", "题量", "正确", "正确率"],
        [
            [key, bucket["total"], bucket["correct"],
             f"{bucket['rate']:.1%}" if bucket["rate"] is not None else "—"]
            for key, bucket in metrics["by_qtype"].items()
        ],
    )
    lines += ["", "### 按难度", ""]
    lines += _table(
        ["难度", "题量", "正确", "正确率"],
        [
            [key, bucket["total"], bucket["correct"],
             f"{bucket['rate']:.1%}" if bucket["rate"] is not None else "—"]
            for key, bucket in metrics["by_difficulty"].items()
        ],
    )
    lines += ["", "### 按陷阱题（口径红线）", ""]
    traps = metrics["traps"]
    lines += _table(
        ["分组", "题量", "正确", "正确率", "覆盖红线"],
        [[
            "陷阱题", traps["total"], traps["correct"],
            f"{traps['rate']:.1%}" if traps["rate"] is not None else "—",
            "、".join(traps["rules"]) or "—",
        ]],
    )
    lines += ["", "## 二、失败分布", "", "| 差异类型 | 次数 |", "| --- | --- |"]
    if metrics["diff_types"]:
        lines += [f"| {key} | {count} |" for key, count in metrics["diff_types"].most_common()]
    else:
        lines += ["| （无） | 0 |"]
    lines += ["", "| 错误码 | 次数 |", "| --- | --- |"]
    if metrics["errors"]:
        lines += [f"| {key} | {count} |" for key, count in metrics["errors"].most_common()]
    else:
        lines += ["| （无） | 0 |"]
    lines += [
        "",
        f"> 判分档命中分布：第一档 {metrics['tiers'].get(1, 0)} 题、"
        f"第二档 {metrics['tiers'].get(2, 0)} 题、第三档 {metrics['tiers'].get(3, 0)} 题",
        "",
        "## 三、逐题明细",
        "",
    ]
    lines += _table(
        ["题号", "来源批次", "题型", "难度", "陷阱", "结果", "判分档", "差异类型", "耗时(ms)", "重试"],
        [
            [
                item.get("question_code"), item.get("_run_code"), item.get("qtype"),
                item.get("difficulty"), "R" if item.get("is_trap") else "—",
                "✅" if item.get("is_correct") else "❌",
                item.get("matched_tier") or "—", item.get("diff_type") or "—",
                item.get("elapsed_ms"), item.get("retries"),
            ]
            for item in items
        ],
    )
    lines += ["", "## 四、错题复盘（生成 vs 标准）", ""]
    for item in metrics["failed"]:
        lines += [
            f"### {item.get('question_code')} · {str(item.get('question') or '').replace(chr(10), ' / ')}",
            "",
            f"- 标准 SQL：`{item.get('gold_sql')}`",
            f"- 生成 SQL/代码：`{str(item.get('generated_sql_or_code') or '—')[:400]}`",
            f"- 差异：{item.get('diff_detail') or item.get('diff_type')}"
            + (f"（错误码：{item.get('error_code')}）" if item.get("error_code") else ""),
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="把多个评测批次合并成一份报告")
    parser.add_argument("run_codes", nargs="+", help="批次编号，如 run_20260917_10 run_20260917_11")
    parser.add_argument("--out", default=None, help="输出路径（默认写入 02.data/eval/reports/）")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{get_settings().power_agent_db}?mode=ro", uri=True)
    items, meta, replaced = load_items(conn, args.run_codes)
    metrics = merge_items(items)
    report = render_report(meta, items, metrics, replaced)

    out = Path(args.out) if args.out else (
        ROOT / "02.data" / "eval" / "reports" / f"merged_{'_'.join(args.run_codes)}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    key_rate = "—" if metrics["key_rate"] is None else f"{metrics['key_rate']:.1%}"
    print(f"已生成合并报告：{out}")
    print(
        f"合计 {metrics['correct']}/{metrics['total']} = {metrics['exec_rate']:.1%}"
        f"｜结论命中率 {key_rate}｜平均 {metrics['avg_elapsed']}ms"
    )


if __name__ == "__main__":
    main()
