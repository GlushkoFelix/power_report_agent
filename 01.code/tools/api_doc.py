"""智电析报 · API 接口文档的**事实源同步工具**（交付物 10）。

为什么要有它：接口文档最容易"写完就过时"——PRD 9.1 的清单是手写的，改代码不会改它。
本工具直接**从 FastAPI 的 OpenAPI schema 取事实**（路径 / 方法 / 摘要 / 鉴权 / 请求体 /
路径参数 / 查询参数），把结果写进《智电析报_API接口文档.md》的
`<!-- BEGIN:ROUTES -->` 与 `<!-- END:ROUTES -->` 两标记之间；
文档其余章节（通用约定 / 错误码 / SSE 事件契约 / 示例）仍由人写，工具不覆盖。

为什么走 OpenAPI 而不是遍历 `app.routes`：新版 FastAPI 把 `include_router` 变成了惰性包装
（`fastapi.routing._IncludedRouter`），路由对象不再平铺在 `app.routes` 里；而 OpenAPI schema
是**面向外部的稳定契约**，字段含义有规范兜底，也不会随框架内部结构调整而失效。

用法（默认**预览**，加 `--write` 才落盘）：

    python 01.code/tools/api_doc.py            # 打印生成的表格，核对用
    python 01.code/tools/api_doc.py --write    # 写回文档（幂等）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "01.code" / "backend"))

DOC = ROOT / "04.实训指导书" / "AI共创过程文档" / "智电析报_API接口文档.md"
BEGIN = "<!-- BEGIN:ROUTES -->"
END = "<!-- END:ROUTES -->"

#: 分组顺序（与 PRD 9.1 的模块划分一致）；key 是 openapi 里的 tag
TAG_LABELS = {
    "auth": "认证与个人中心",
    "reports": "报表管理",
    "conversations": "会话",
    "analysis": "对话与分析（含 SSE 流式）",
    "records": "分析记录",
    "charts": "图表",
    "eval": "评测中心",
    "system": "系统与运维",
}
TAG_ORDER = {name: index for index, name in enumerate(TAG_LABELS)}

METHOD_ORDER = {"GET": 0, "POST": 1, "PATCH": 2, "PUT": 3, "DELETE": 4}

#: 免鉴权端点（openapi 里 security 为空即免鉴权，这里只用于**交叉校验**
#: —— 若某条本应免鉴权的端点意外带上了 security，工具会提示，避免文档把它写成"要登录"）
PUBLIC_EXPECTED = {"/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/system/health"}


def _ref_name(schema: dict | None) -> str:
    """从 schema 里取模型名；数组/联合类型尽量给出可读形式。"""
    if not schema:
        return "—"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return f"{_ref_name(schema.get('items'))}[]"
    if "anyOf" in schema:
        names = [_ref_name(item) for item in schema["anyOf"] if item.get("type") != "null"]
        names = [name for name in names if name != "—"]
        return "/".join(dict.fromkeys(names)) or "—"
    return "—"


def collect() -> tuple[list[dict[str, str]], list[str]]:
    from app.main import app  # 延迟导入：确保 sys.path 已就绪

    schema = app.openapi()
    rows: list[dict[str, str]] = []
    warnings: list[str] = []

    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            method = method.upper()
            if method not in METHOD_ORDER:
                continue
            tags = operation.get("tags") or ["其它"]
            tag = tags[0]
            summary = (operation.get("summary") or "").strip()
            if not summary:
                description = (operation.get("description") or "").strip()
                summary = description.splitlines()[0] if description else ""
            summary = re.sub(r"\s+", " ", summary).rstrip("。")

            params = operation.get("parameters") or []
            path_params = [p["name"] for p in params if p.get("in") == "path"]
            query_params = [p["name"] for p in params if p.get("in") == "query"]
            body = _ref_name(
                (operation.get("requestBody") or {}).get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            secure = bool(operation.get("security"))
            if path in PUBLIC_EXPECTED and secure:
                warnings.append(f"{path} 本应免鉴权，但 OpenAPI 里带了 security")

            rows.append(
                {
                    "method": method,
                    "path": path.replace("/api/v1", ""),
                    "path_params": "、".join(f"`{name}`" for name in path_params) or "—",
                    "query_params": "、".join(f"`{name}`" for name in query_params) or "—",
                    "summary": summary or "—",
                    "auth": "是" if secure else "**否**",
                    "body": body,
                    "tag": tag,
                }
            )

    rows.sort(
        key=lambda row: (
            TAG_ORDER.get(row["tag"], 99),
            row["path"],
            METHOD_ORDER.get(row["method"], 9),
        )
    )
    return rows, warnings


def render(rows: list[dict[str, str]]) -> str:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(row["tag"], []).append(row)

    lines: list[str] = []
    for tag in sorted(groups, key=lambda name: TAG_ORDER.get(name, 99)):
        label = TAG_LABELS.get(tag, tag)
        lines.append(f"#### {label}（{tag}）")
        lines.append("")
        lines.append("| 方法 | 路径 | 说明 | 鉴权 | 路径参数 | 查询参数 | 请求体 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in groups[tag]:
            lines.append(
                f"| `{row['method']}` | `{row['path']}` | {row['summary']} | {row['auth']} "
                f"| {row['path_params']} | {row['query_params']} | {row['body']} |"
            )
        lines.append("")

    total = len(rows)
    public = sum(1 for row in rows if row["auth"] != "是")
    lines.append(
        f"> 本表由 `01.code/tools/api_doc.py` 从 **OpenAPI schema 自动生成**（共 {total} 条端点，"
        f"其中免鉴权 {public} 条），与代码同源。改接口后重跑 "
        "`python 01.code/tools/api_doc.py --write` 即可同步，避免手工清单过时。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步《API 接口文档》的端点总表")
    parser.add_argument("--write", action="store_true", help="写回文档（默认只预览）")
    args = parser.parse_args()

    rows, warnings = collect()
    block = render(rows)
    for warning in warnings:
        print(f"[提示] {warning}", file=sys.stderr)

    if not args.write:
        print(block)
        print(f"\n[预览] 共 {len(rows)} 条端点；加 --write 写回 {DOC.name}")
        return 0

    if not DOC.exists():
        print(f"文档不存在：{DOC}", file=sys.stderr)
        return 1
    text = DOC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"文档缺少标记 {BEGIN} / {END}，请先补上", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(f"{head}{BEGIN}\n\n{block}\n\n{END}{tail}", encoding="utf-8")
    print(f"已写入 {DOC}（{len(rows)} 条端点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
