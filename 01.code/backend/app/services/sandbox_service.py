"""智电析报 · pandas 沙箱执行服务（PRD F2.3，交付物 5 沙箱/守卫批次）。

执行架构（本轮定案：**子进程隔离 + AST 预检**）：

1. **AST 预检**（`app/core/code_guard.py`，主进程）：import 白名单 / 双下划线 / 危险调用，
   不过预检的代码**根本不会进入沙箱**；
2. **子进程执行**（本模块，spawn 进程池常驻 1 个 worker）：超时用 `async_result.get(timeout)`
   强制（超时即 terminate worker，主进程永不挂）；进程崩溃（OOM 等）由池重建兜底——
   **内存上限在 Windows 上没有进程级强制手段**（`resource.setrlimit` 仅 POSIX），
   属"尽力而为"：进程隔离保证崩溃不影响服务，G6 验收以预检拦截 + 超时强杀为主；
3. **数据供给**：worker 里从 `reports.db`（`mode=ro`）把活跃报表整表读成 DataFrame 预置为
   `df`（与 SQL 路径同一个只读库，与 PRD F2.3「沙箱内预置 df」一致），并预置
   `pd` / `np` / `px` / `go`；
4. **结果回传**：代码把最终结果赋值给 `result`（DataFrame/Series/标量）、图表赋值给 `fig`
   （可选，plotly Figure）→ worker 内序列化为 JSON 安全结构（结果 ≤5000 行，超出截断）。

关于进程池的取舍：Windows spawn 冷启动 + pandas import 约 1–2s，因此 worker
**常驻**（`maxtasksperchild=None`），首次调用付一次成本，后续任务复用已导入 pandas 的进程；
超时或崩溃后池被丢弃，下次调用自动重建（重新付冷启动成本，但主进程安全优先）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.code_guard import guard_code
from app.core.errors import BizError, E_SANDBOX_TIMEOUT, code_exec, code_forbidden

logger = logging.getLogger("zdxb.sandbox")

__all__ = [
    "SandboxOutcome",
    "run_pandas_code",
    "warm_sandbox",
    "EXEC_TIMEOUT_SECONDS",
    "MAX_RESULT_ROWS",
    "MEMORY_LIMIT_MB",
]

#: 执行超时（PRD F2.3：默认 10s，超时强杀 worker）
EXEC_TIMEOUT_SECONDS = 10.0

#: 结果集上限（PRD F2.3：≤5000 行，超出截断并标记）
MAX_RESULT_ROWS = 5000

#: 内存软上限（POSIX 用 setrlimit 强制；Windows 无进程级手段，仅文档承诺）
MEMORY_LIMIT_MB = 512

#: 失败重试不需要完整堆栈，只回传末尾几行
TRACE_TAIL_LINES = 4

_SANDOX_FRAME_RE = re.compile(r'[Ff]ile "<sandbox>", line (\d+)')

_POOL: "multiprocessing.pool.Pool | None" = None
_POOL_CTX = multiprocessing.get_context("spawn")


@dataclass
class SandboxOutcome:
    """一次沙箱执行的结果（成功时带表结构与行数据，失败时带归因信息）。"""

    ok: bool
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    #: 结果超过 5000 行被截断（与 SQL 路径的 `truncated` 同义）
    truncated: bool = False
    #: `fig` 的 plotly figure JSON（未产出图表时为 None）
    figure: dict[str, Any] | None = None
    #: 结果对象本身的类型（dataframe / series / scalar），供解读环节提示
    result_kind: str = ""
    error_type: str | None = None
    message: str | None = None
    line_no: int | None = None
    trace_tail: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


# ── 子进程 worker（模块级函数，spawn 可 pickle）─────────────────────────────

def _apply_memory_limit() -> None:
    """POSIX 上按 PRD 强制内存上限；Windows 无对应机制，静默跳过。"""
    try:
        import resource  # noqa: PLC0415 —— 仅 POSIX

        limit = MEMORY_LIMIT_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:  # noqa: BLE001 —— Windows / 不可用环境直接跳过
        pass


def _dtype_name(dtype: Any) -> str:
    """pandas dtype → 结果表类型名（与 SQL 路径的 INTEGER/REAL/TEXT 口径一致）。"""
    import pandas as pd  # noqa: PLC0415

    if pd.api.types.is_bool_dtype(dtype) or pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_float_dtype(dtype):
        return "REAL"
    return "TEXT"


def _serialize_result(result: Any, max_rows: int) -> dict[str, Any]:
    """`result` 变量 → 表结构 + 行数据（JSON 安全）；支持 DataFrame / Series / 标量。"""
    import numpy as np  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    if isinstance(result, pd.Series):
        name = str(result.name) if result.name is not None else "value"
        df = result.to_frame(name=name).reset_index()
    elif isinstance(result, pd.DataFrame):
        df = result
    elif isinstance(result, (int, float, bool, str)) or isinstance(result, np.generic):
        value = result.item() if isinstance(result, np.generic) else result
        return {
            "columns": [{"name": "value", "type": "INTEGER" if isinstance(value, bool) else
                         "REAL" if isinstance(value, (int, float)) else "TEXT"}],
            "rows": [{"value": value}],
            "row_count": 1,
            "truncated": False,
            "result_kind": "scalar",
        }
    else:
        try:
            df = pd.DataFrame(result)
        except Exception:  # noqa: BLE001 —— 无法成表的对象按文本呈现
            return {
                "columns": [{"name": "value", "type": "TEXT"}],
                "rows": [{"value": str(result)}],
                "row_count": 1,
                "truncated": False,
                "result_kind": "scalar",
            }

    truncated = len(df) > max_rows
    if truncated:
        df = df.head(max_rows)
    rows = json.loads(
        df.to_json(orient="records", force_ascii=False, date_format="iso", double_precision=10)
    )
    return {
        "columns": [{"name": str(col), "type": _dtype_name(df[col].dtype)} for col in df.columns],
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "result_kind": "dataframe" if isinstance(result, pd.DataFrame) else "series",
    }


def _serialize_figure(fig: Any) -> dict[str, Any] | None:
    """`fig` 变量 → plotly figure JSON；不是 plotly Figure 时如实返回 None。"""
    if fig is None or not hasattr(fig, "to_plotly_json"):
        return None
    payload = fig.to_plotly_json()
    layout = payload.get("layout")
    if isinstance(layout, dict):
        layout.pop("template", None)  # 与图表服务同口径：不带内置主题
    return payload


def _execute_task(payload: dict[str, Any]) -> dict[str, Any]:
    """在子进程里执行一段（已过 AST 预检的）pandas 代码并回传可序列化结果。

    本函数运行在 spawn 子进程里：**惰性导入** pandas/plotly——主进程 import 本模块
    不加载这些重依赖，worker 常驻后只付一次导入成本。
    """
    import sqlite3  # noqa: PLC0415

    _apply_memory_limit()
    started = time.perf_counter()
    try:
        import numpy as np  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        import plotly.express as px  # noqa: PLC0415,F401
        import plotly.graph_objects as go  # noqa: PLC0415,F401

        conn = sqlite3.connect(f"file:{payload['db']}?mode=ro", uri=True)
        try:
            df = pd.read_sql_query(f'SELECT * FROM "{payload["table"]}"', conn)
        finally:
            conn.close()

        env: dict[str, Any] = {"df": df, "pd": pd, "np": np, "px": px, "go": go, "__name__": "__sandbox__"}
        exec(compile(payload["code"], "<sandbox>", "exec"), env)  # noqa: S102 —— AST 预检 + 子进程隔离双防线

        if "result" not in env or env["result"] is None:
            return {
                "ok": False,
                "error_type": "NoResult",
                "message": "代码没有把最终结果赋值给变量 result",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }

        payload_out = {"ok": True, **_serialize_result(env["result"], payload["max_rows"])}
        payload_out["figure"] = _serialize_figure(env.get("fig"))
        payload_out["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return payload_out
    except Exception as exc:  # noqa: BLE001 —— 执行错误回传归因，不炸 worker
        trace_lines = traceback.format_exc().strip().splitlines()
        line_match = None
        for line in reversed(trace_lines):
            match = _SANDOX_FRAME_RE.search(line)
            if match:
                line_match = int(match.group(1))
                break
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
            "line_no": line_match,
            "trace_tail": trace_lines[-TRACE_TAIL_LINES:],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


# ── 进程池管理（常驻 + 崩溃重建）────────────────────────────────────────────

def _get_pool() -> "multiprocessing.pool.Pool":
    global _POOL
    if _POOL is None:
        _POOL = _POOL_CTX.Pool(processes=1, maxtasksperchild=None)
    return _POOL


def _discard_pool() -> None:
    global _POOL
    if _POOL is not None:
        try:
            _POOL.terminate()
        except Exception:  # noqa: BLE001 —— 池已经死了就不纠结
            pass
        _POOL = None


def _run_sync(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """阻塞执行（在线程池里跑，不堵事件循环）：超时/崩溃都转化为结构化结果。"""
    started = time.perf_counter()
    pool = _get_pool()
    try:
        async_result = pool.apply_async(_execute_task, (payload,))
        return async_result.get(timeout=timeout)
    except multiprocessing.TimeoutError:
        _discard_pool()  # worker 还在跑死循环，只能整个丢掉重建
        return {
            "ok": False,
            "error_type": "Timeout",
            "message": f"沙箱执行超过 {timeout:g} 秒，已被强制终止",
            "timed_out": True,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 —— worker 被 OOM 杀死等，池已损坏
        logger.warning("沙箱进程池异常，已重建：%s", exc)
        _discard_pool()
        return {
            "ok": False,
            "error_type": "SandboxCrash",
            "message": f"沙箱进程异常退出（{type(exc).__name__}），已重建执行环境",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


async def run_pandas_code(
    code: str | None,
    table_name: str,
    *,
    db_path: str | Path,
    timeout: float = EXEC_TIMEOUT_SECONDS,
    max_rows: int = MAX_RESULT_ROWS,
) -> SandboxOutcome:
    """pandas 沙箱执行主入口：**预检 → 子进程执行 → 结构化结果**。

    - 预检不过 → 400 `E_CODE_FORBIDDEN`（与 SQL 路径的 `E_SQL_FORBIDDEN` 对应）；
    - 执行超时 → 500 `E_SANDBOX_TIMEOUT`（worker 已被强杀，池已重建）；
    - 运行报错 → 500 `E_CODE_EXEC`（带 `error_type` / `line_no` / `trace_tail`，供 F2.4 重写）；
    - 成功 → `SandboxOutcome`（columns/rows 与 SQL 路径同构，figure 与图表服务同构）。
    """
    guard = guard_code(code)
    if not guard.allowed:
        raise code_forbidden(guard.reason or "", guard.rule_id or "")

    payload = {
        "code": (code or "").strip(),
        "table": table_name,
        "db": Path(db_path).as_posix(),
        "max_rows": max_rows,
    }
    result = await asyncio.to_thread(_run_sync, payload, timeout)

    if result.get("timed_out"):
        raise BizError(
            500,
            E_SANDBOX_TIMEOUT,
            result.get("message") or f"沙箱执行超过 {timeout:g} 秒",
            {"timeout_seconds": timeout},
            hint="可缩小统计范围（如限定时间段）后重试，或改用 SQL 查询",
        )
    if not result.get("ok"):
        raise code_exec(
            result.get("error_type") or "Unknown",
            result.get("message") or "未知错误",
            line_no=result.get("line_no"),
            trace_tail=result.get("trace_tail"),
        )

    return SandboxOutcome(
        ok=True,
        columns=result.get("columns") or [],
        rows=result.get("rows") or [],
        row_count=int(result.get("row_count") or 0),
        truncated=bool(result.get("truncated")),
        figure=result.get("figure"),
        result_kind=result.get("result_kind") or "",
        elapsed_ms=int(result.get("elapsed_ms") or 0),
    )


async def warm_sandbox() -> None:
    """预热进程池（应用启动时调用）：提前付 spawn + pandas import 的冷启动成本。

    失败只记日志不抛——预热是优化项，首次真实调用会自然完成同样的事。
    """
    try:
        await asyncio.to_thread(_get_pool)
        logger.info("沙箱进程池已预热")
    except Exception:  # noqa: BLE001
        logger.warning("沙箱进程池预热失败（将在首次执行时重试）", exc_info=True)
