"""智电析报 · pandas 代码 AST 预检（PRD F2.3 / S1，交付物 5 沙箱/守卫批次）。

两道防线里的**第一道**（第二道是 `sandbox_service` 的子进程隔离）：执行前对 LLM 生成的
Python 代码做**语法树检查**——与 SQL 守卫同理（ADR-004），不做关键字黑名单，
字符串拼接、大小写、换行都绕不过。

拦截面（PRD F2.3 字面 + 实战补充）：

1. **import 白名单**：允许 `math` / `statistics` / `datetime`，以及沙箱**本来就预置**的
   `pandas` / `numpy` / `plotly`（v1.19.12 放行，见下方说明）；其余（`os` / `socket` /
   `subprocess` / 相对导入）一律拒绝；

   > **为什么放行 `pandas` / `numpy` / `plotly`**：模型（尤其轻量模型）习惯性写
   > `import pandas as pd`，而 `pd` 早已在命名空间里——这句是**冗余而非危险**（拿到的是同一个模块
   > 对象，不带来任何新能力）。原先一律拒绝的后果是：**"模型多写一句 import" → 整题以
   > `E_CODE_FORBIDDEN` 失败**（用户实测在"重跑"里踩到）。提示词里已明确"不要写 import"，
   > 白名单这边作为**兜底**放行，两道保险都不再因为一句冗余 import 丢掉整题。
2. **双下划线属性**：任何含 `__` 的名称/属性（`__class__` / `__globals__` /
   `__builtins__`…）——这是 RestrictedPython 的同款规则，反射逃逸链
   （`().__class__.__mro__`）的第一环，直接掐断；
3. **危险调用**：`open` / `eval` / `exec` / `compile` / `__import__` / `getattr` /
   `setattr` / `delattr` / `globals` / `locals` / `vars` / `input` / `breakpoint` /
   `super` / `exit` / `quit`，以及属性形式的 `.system` / `.popen` / `.remove` /
   `.unlink` 等（防 `os` 经由非 import 途径进入命名空间）；
4. **危险模块名作裸名**：`os` / `sys` / `subprocess` / `socket` / `builtins` /
   `importlib` / `ctypes` / `shutil`——即使 import 被禁，也杜绝
   `pd.os` 之类的属性探测与同名遮蔽。

纯函数、无第三方依赖：本模块在主进程与子进程里都会被使用（子进程里是第二道复核）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["CodeGuardResult", "guard_code"]

#: import 白名单（PRD F2.3：「禁止 import 非白名单模块」）
#: 后三个是**沙箱预置库**：放行冗余 import，不让"多写一句 import"毁掉整题（v1.19.12）
ALLOWED_IMPORTS = frozenset({"math", "statistics", "datetime", "pandas", "numpy", "plotly"})

#: 禁止调用的内建/名称（PRD F2.3 字面 + 反射与干扰面）
FORBIDDEN_CALLS = frozenset({
    "open", "eval", "exec", "compile", "__import__",
    "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "dir",
    "input", "breakpoint", "super", "exit", "quit",
    "help", "memoryview",
})

#: 禁止的属性调用（`x.system()` / `y.unlink()` 形态；os/subprocess 无法 import，
#: 但防它们经由其它对象进入命名空间）
FORBIDDEN_ATTR_CALLS = frozenset({
    "system", "popen", "spawnl", "spawnv",
    "remove", "unlink", "rmdir", "rename", "mkdir", "makedirs",
    "kill", "terminate", "fork", "execv", "execve", "system_command",
    "write_text", "write_bytes", "chmod", "chown",
})

#: 禁止作裸名/属性出现的危险模块名（防同名遮蔽与属性探测）
FORBIDDEN_MODULE_NAMES = frozenset({
    "os", "sys", "subprocess", "socket", "builtins", "importlib",
    "ctypes", "shutil", "pathlib", "pickle", "shelve", "marshal",
})


@dataclass
class CodeGuardResult:
    """代码预检结果：`allowed=False` 时 `rule_id` / `reason` 用于审计与重试提示。"""

    allowed: bool
    reason: str | None = None
    rule_id: str | None = None
    line_no: int | None = None


def _deny(reason: str, rule_id: str, line_no: int | None = None) -> CodeGuardResult:
    return CodeGuardResult(allowed=False, reason=reason, rule_id=rule_id, line_no=line_no)


def _import_target(node: ast.Import | ast.ImportFrom) -> str | None:
    """取 import 的顶层模块名（`import a.b.c` → `a`；`from x import y` → `x`）。"""
    if isinstance(node, ast.ImportFrom):
        if node.level and node.level > 0:
            return "."  # 相对导入一律拒绝
        return (node.module or "").split(".")[0]
    for alias in node.names:
        return alias.name.split(".")[0]
    return None


def guard_code(code: str | None) -> CodeGuardResult:
    """AST 预检：通过返回 `allowed=True`；不通过给出 `rule_id` / `reason` / `line_no`。"""
    text = (code or "").strip()
    if not text:
        return _deny("代码为空", "codeguard:empty")
    if len(text) > 20_000:
        return _deny(f"代码过长（{len(text)} 字符，上限 20000）", "codeguard:too_long")

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return _deny(f"代码语法无法解析：{exc.msg}", "codeguard:parse_error", exc.lineno)

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)

        # ① import 白名单
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            target = _import_target(node)
            if target not in ALLOWED_IMPORTS:
                return _deny(
                    f"不允许 import 模块「{target}」（白名单："
                    f"{'、'.join(sorted(ALLOWED_IMPORTS))}；"
                    f"`df`/`pd`/`np`/`px`/`go` 已预置，无需 import）",
                    "codeguard:import_not_allowed",
                    line,
                )

        # ② 双下划线（反射逃逸的第一环）
        if isinstance(node, ast.Name) and "__" in node.id:
            return _deny(
                f"不允许使用双下划线名称「{node.id}」", "codeguard:dunder", line
            )
        if isinstance(node, ast.Attribute) and "__" in node.attr:
            return _deny(
                f"不允许访问双下划线属性「{node.attr}」", "codeguard:dunder", line
            )

        # ③ 危险调用
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                return _deny(f"禁止调用「{func.id}()」", "codeguard:forbidden_call", line)
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTR_CALLS:
                return _deny(
                    f"禁止调用方法「.{func.attr}()」", "codeguard:forbidden_call", line
                )
            if isinstance(func, ast.Attribute) and "__" in func.attr:  # 已被 ② 覆盖，双保险
                return _deny(
                    f"不允许访问双下划线属性「{func.attr}」", "codeguard:dunder", line
                )

        # ④ 危险模块名作裸名
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_MODULE_NAMES:
            return _deny(
                f"不允许引用模块「{node.id}」（不在沙箱白名单内）",
                "codeguard:forbidden_name",
                line,
            )
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_MODULE_NAMES:
            return _deny(
                f"不允许访问属性「.{node.attr}」（不在沙箱白名单内）",
                "codeguard:forbidden_name",
                line,
            )

    return CodeGuardResult(allowed=True)
