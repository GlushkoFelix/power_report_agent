"""跑一次评测批次并打印摘要（真实模型 / 自检通用），顺带统计 token 消耗与报告路径。

用途（对应 PRD F5 与《实训6 执行测试与评测》步骤4/5）：
    - 先自检（不花 token）验证「题库 → 链路 → 判分 → 指标 → 报告」全通；
    - 再真实模型小样本（如 5–10 题）确认链路与耗时，最后跑完整批次。

用法（在项目根执行）：
    .venv\\Scripts\\python.exe 01.code\\tools\\eval_run.py --impl selfcheck --limit 5
    .venv\\Scripts\\python.exe 01.code\\tools\\eval_run.py --impl real --limit 5
    .venv\\Scripts\\python.exe 01.code\\tools\\eval_run.py --impl real --limit 120 --name "真实模型 120 题"

参数：
    --impl      auto | real | selfcheck（默认 real）
    --limit     抽题数量（默认 5；不传 = 全部 120 题）
    --user      登录用户名（默认 demo）
    --password  登录密码（默认 demo12345）
    --base      后端地址（默认 http://127.0.0.1:8000）
    --name      批次备注名（可选）
    --poll      轮询间隔秒（默认 3）
    --timeout   最长等待秒（默认 3600，120 题真实模型可能跑 20–40 分钟）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTROL_DB = PROJECT_ROOT / "02.data" / "power_agent.db"


def login(client: httpx.Client, base: str, user: str, password: str) -> str:
    """登录并返回 access token（先用登录，失败再尝试注册）。"""
    for path in ("/auth/login", "/auth/register"):
        resp = client.post(f"{base}/api/v1{path}", json={"username": user, "password": password})
        body = resp.json()
        if body.get("code") == 0:
            return body["data"]["token"]
    raise SystemExit(f"登录/注册失败：{resp.status_code} {resp.text[:200]}")


def token_usage(run_code: str) -> dict[str, object]:
    """从控制面库汇总该批次的 token 消耗（`llm_calls.eval_run_id` 在批次结束时回填）。"""
    if not CONTROL_DB.exists():
        return {"available": False}
    conn = sqlite3.connect(f"file:{CONTROL_DB.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("select id from eval_runs where run_code = ?", (run_code,)).fetchone()
        if not row:
            return {"available": False}
        run_id = row[0]
        total = conn.execute(
            "select coalesce(sum(total_tokens),0), coalesce(sum(prompt_tokens),0), "
            "coalesce(sum(completion_tokens),0), count(*) "
            "from llm_calls where eval_run_id = ?",
            (run_id,),
        ).fetchone()
        by_stage = conn.execute(
            "select stage, count(*), coalesce(sum(total_tokens),0) from llm_calls "
            "where eval_run_id = ? group by stage order by 3 desc",
            (run_id,),
        ).fetchall()
        return {
            "available": True,
            "total": total[0],
            "prompt": total[1],
            "completion": total[2],
            "calls": total[3],
            "by_stage": by_stage,
        }
    finally:
        conn.close()


def print_summary(payload: dict, usage: dict[str, object]) -> None:
    """打印批次摘要、分类正确率、归因与 token 消耗。"""
    print("=" * 72)
    print(f"批次：{payload.get('run_code')}  状态：{payload.get('status')}")
    print(f"模式：{payload.get('impl')}  模型：{payload.get('model_name')}  "
          f"温度：{payload.get('temperature')}  提示词：{payload.get('prompt_version')}")
    total = payload.get("question_total")
    correct = payload.get("exec_correct_count")
    rate = payload.get("exec_correct_rate")
    print(f"题量：{total}  执行正确：{correct}  正确率：{'—' if rate is None else f'{rate * 100:.1f}%'}")
    hit = payload.get("narrative_hit_rate")
    print(f"结论命中率：{'—' if hit is None else f'{hit * 100:.1f}%'}")
    print(f"平均耗时：{payload.get('avg_elapsed_ms')} ms  P95：{payload.get('p95_elapsed_ms')} ms  "
          f"重试：{payload.get('retry_total')}")
    if payload.get("error_message"):
        print(f"错误：{payload['error_message']}")

    metrics = payload.get("metrics") or {}
    for key, label in (("by_qtype", "按题型"), ("by_difficulty", "按难度"), ("by_trap", "陷阱题")):
        groups = metrics.get(key)
        if not groups:
            continue
        print(f"\n-- {label} --")
        for name, stat in groups.items():
            print(f"  {name:<12} {stat.get('total')} 题，正确 {stat.get('correct')}，"
                  f"正确率 {stat.get('rate')}")

    attribution = metrics.get("attribution")
    if attribution:
        print("\n-- bad case 归因 --")
        for name, count in attribution.items():
            print(f"  {name}: {count}")

    print("\n-- token 消耗 --")
    if not usage.get("available"):
        print("  （未取到：批次可能尚未回填 llm_calls.eval_run_id）")
    else:
        print(f"  合计 {usage['total']} tokens（prompt {usage['prompt']} + completion "
              f"{usage['completion']}），模型调用 {usage['calls']} 次")
        for stage, calls, tokens in usage["by_stage"]:  # type: ignore[misc]
            print(f"    {stage:<10} {calls} 次，{tokens} tokens")

    report_path = payload.get("report_path")
    if report_path:
        path = Path(report_path)
        path = path if path.is_absolute() else PROJECT_ROOT / path
        print(f"\n报告：{path}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="跑一次评测批次并打印摘要")
    parser.add_argument("--impl", default="real", choices=["auto", "real", "selfcheck"])
    parser.add_argument("--limit", type=int, default=5, help="抽题数量（0 或不传 = 全部）")
    parser.add_argument(
        "--codes",
        default=None,
        help="指定题号（逗号分隔，如 q001,q010,q052）——用于分层抽样验证；给了题号则忽略 limit",
    )
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="demo12345")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--name", default=None, help="批次备注名")
    parser.add_argument("--poll", type=float, default=3.0, help="轮询间隔秒")
    parser.add_argument("--timeout", type=float, default=3600.0, help="最长等待秒")
    args = parser.parse_args()

    with httpx.Client(timeout=60) as client:
        token = login(client, args.base, args.user, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        body: dict[str, object] = {"impl": args.impl, "include_narrative": True}
        codes = [item.strip() for item in (args.codes or "").split(",") if item.strip()]
        if codes:
            body["question_codes"] = codes
        elif args.limit and args.limit > 0:
            body["limit"] = args.limit
        if args.name:
            body["name"] = args.name

        created = client.post(f"{args.base}/api/v1/eval/runs", headers=headers, json=body).json()
        if created.get("code") != 0:
            print(f"发起失败：{created.get('message')}（hint: {created.get('data')}）")
            return 1
        run_code = created["data"]["run_code"]
        print(f"已发起批次 {run_code}（impl={args.impl}，limit={args.limit or '全部'}），开始轮询…")

        deadline = time.time() + args.timeout
        payload: dict = {}
        while time.time() < deadline:
            payload = client.get(
                f"{args.base}/api/v1/eval/runs/{run_code}",
                headers=headers,
                params={"include_questions": "false"},
            ).json()["data"]
            status = payload.get("status")
            print(f"  [{time.strftime('%H:%M:%S')}] status={status} "
                  f"题量={payload.get('question_total')}")
            if status in ("finished", "failed"):
                break
            time.sleep(args.poll)
        else:
            print("等待超时（批次仍在后台运行，可稍后再查）")

    if payload.get("status") == "finished":
        print_summary(payload, token_usage(run_code))
        return 0
    print(f"批次未完成：status={payload.get('status')} error={payload.get('error_message')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
