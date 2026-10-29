"""首字延迟探针（G3 / N1「首字响应 ≤2s」）—— 真实 SSE 链路逐事件打时间戳。

**为什么需要独立工具**：评测模块的客户端是**单次 POST（非流式）**，所以评测报告里
`first_token_avg_ms` 恒为 NULL（PRD 已如实记录）。首字延迟只能用真的走 SSE 来量。

**它同时能验证"是不是真流式"**：改造前所有事件会在链路结束后**同一毫秒**到达（攒批回放），
改造后应呈递增节奏（`status` → `slot` → `table` → `narrative` → `done`）。

用法：
    python 01.code/tools/sse_probe.py                      # 默认问题（简单问数，对齐 N2 口径）
    python 01.code/tools/sse_probe.py --question "按月份汇总全期的总用电量。"
    python 01.code/tools/sse_probe.py --user demo_ui --password demo12345
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

__all__ = ["probe"]

#: G3 目标：SSE 首个事件到达浏览器 ≤2s（PRD 6.1 N1）
FIRST_TOKEN_TARGET_MS = 2000
DEFAULT_QUESTION = "全期总用电量是多少？"


def probe(base_url: str, username: str, password: str, question: str) -> int:
    """跑一次真实对话并打印事件时间线；返回进程退出码（0 = 首字达标）。"""
    client = httpx.Client(timeout=180)
    login = client.post(
        f"{base_url}/auth/login", json={"username": username, "password": password}
    )
    if login.status_code != 200:
        print(f"登录失败：HTTP {login.status_code} {login.text[:200]}")
        return 2
    headers = {"Authorization": f"Bearer {login.json()['data']['token']}"}

    conversation = client.post(f"{base_url}/conversations", headers=headers, json={}).json()["data"]
    code = conversation["conversation_code"]
    print(f"会话：{code}\n问题：{question}\n")

    started = time.perf_counter()
    marks: list[tuple[str, float]] = []
    error_payload: str | None = None
    try:
        with client.stream(
            "POST",
            f"{base_url}/analysis/chat",
            headers=headers,
            json={"conversation_code": code, "message": question},
        ) as response:
            current = ""
            for line in response.iter_lines():
                elapsed_ms = (time.perf_counter() - started) * 1000
                if line.startswith("event:"):
                    current = line.split(":", 1)[1].strip()
                    marks.append((current, elapsed_ms))
                elif line.startswith("data:") and current == "error":
                    error_payload = line[:200]
    except Exception as exc:  # noqa: BLE001
        print(f"流式中断：{exc}")

    print("== 事件到达时刻（相对请求发出）==")
    for name, elapsed_ms in marks:
        print(f"  {name:<10} {elapsed_ms:>8.0f} ms")
    if error_payload:
        print(f"\n错误载荷：{error_payload}")
    if not marks:
        print("\n没有收到任何事件 ❌")
        return 1

    first = marks[0][1]
    ok = first <= FIRST_TOKEN_TARGET_MS
    print(
        f"\n**首字 {first:.0f} ms**（目标 ≤{FIRST_TOKEN_TARGET_MS} ms）"
        f"{'✅ 达标' if ok else '❌ 未达标'}｜端到端 {marks[-1][1]:.0f} ms"
    )
    # 攒批回放的特征：多个事件落在同一毫秒 —— 只有 1 个事件时不算
    same_ms = len({round(ms) for _, ms in marks}) == 1 and len(marks) > 1
    if same_ms:
        print("⚠️ 所有事件同时到达 —— 疑似「攒批回放」，SSE 不是真流式")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="SSE 首字延迟探针（G3/N1）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--user", default="demo_ui")
    parser.add_argument("--password", default="demo12345")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args()
    sys.exit(probe(args.base_url.rstrip("/"), args.user, args.password, args.question))


if __name__ == "__main__":
    main()
