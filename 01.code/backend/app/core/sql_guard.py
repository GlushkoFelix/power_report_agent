"""智电析报 · SQL 只读守卫（PRD F2.2 / S1 / 数据库设计 S1，ADR-004）。

**只放行「单条 SELECT」**，其余一律拒绝并落审计。实现要点：

1. **按语法树判定，不做关键字黑名单**（ADR-004）：`sqlglot` 真实解析 SQLite 方言后遍历 AST，
   注释、大小写、换行、字符串里的关键字都无法绕过；
2. **表名白名单**：白名单 = 当前会话已注册报表的物理表名（`uploaded_reports.table_name`），
   子查询/UNION/JOIN 里的表同样受检；拒绝跨库限定名（`other_db.report_daily`）；
3. **危险函数拦截**：`load_extension` / `readfile` / `writefile` 属 SQLite 扩展面，即使只读也拒绝；
4. **强制 LIMIT**：最外层查询没有 LIMIT 时自动补 `LIMIT 1000`（PRD N7），
   响应里以 `limit_applied` 回显，便于前端提示"结果已截断"；
5. **R3 混加检测（交付物 5 沙箱/守卫批次）**：**同一个加法表达式**里同时出现「用途拆分」列
   （kitchen/laundry/…）与「时段拆分」列（peak/flat/valley）→ 拒绝（PRD 红线 R3：
   两套拆分正交，相加得到的是无物理意义的数）。**并列选择多列不违规**（结构报表本就两套列共存），
   只有表达式内相加才拦。列语义映射与图表层共用同一份来源（`chart_service.PURPOSE_SERIES` /
   `PERIOD_SERIES`），杜绝两处口径漂移；
6. **Top-N 极值自动排除 NULL（v1.19.23，确定性修复，不消耗模型调用）**：`ORDER BY 某列 LIMIT k`
   且 **k ≤ 5**（= 求极值 / 排名）时，自动补 `AND 该列 IS NOT NULL`。
   依据是 PRD 红线口径：**NULL 表示"没有数据"，不是最小/最大**——SQLite 下 `ORDER BY x ASC`
   会把 NULL 排在最前，"最低的那天"就会选中缺失日（实测 q118 在速度档上因此判错，
   而提示词第 15 条已明文要求排除 NULL ⇒ **能在代码里保证的事就不该指望模型**）。
   只在"显式小 LIMIT"时生效：**罗列式排序**（取全表 / 大 LIMIT）不补，避免改变"按序查看"的语义。

落地位置对应架构文档 5.3「语句层」与 5.2 的写通道隔离：守卫在**执行前**运行，
执行走 `mode=ro` 只读连接，两道防线互相独立。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logger = logging.getLogger("zdxb.sqlguard")

__all__ = ["GuardResult", "guard_sql", "DEFAULT_LIMIT", "FORBIDDEN_FUNCTIONS"]

#: 强制 LIMIT（PRD N7：默认 LIMIT 1000 行）
DEFAULT_LIMIT = 1000

#: SQL 文本长度上限（防超长输入拖垮解析）
MAX_SQL_LENGTH = 20_000

#: 明确拒绝的函数（SQLite 扩展面）
FORBIDDEN_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit"})

#: Top-N 判定阈值（v1.19.23）：`ORDER BY 列 LIMIT k` 且 k 不超过该值时，视为"求极值 / 排名"，
#: 守卫会自动补 `AND 该列 IS NOT NULL`（NULL 表示没有数据，不该当极值）。
TOP_N_LIMIT_MAX = 5

#: 明确拒绝的语句类型（只允许 SELECT / 带 WITH 的 SELECT）
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Attach,
    exp.Detach, exp.Pragma, exp.Command, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Grant, exp.Copy, exp.Merge,
)


@dataclass
class GuardResult:
    """守卫结果：`allowed` 为 False 时 `rule_id` / `reason` 用于审计与错误提示。"""

    allowed: bool
    sql: str = ""
    reason: str | None = None
    rule_id: str | None = None
    tables: list[str] = field(default_factory=list)
    limit_applied: bool = False
    #: 为 Top-N 极值自动补过 `IS NOT NULL` 的列名（v1.19.23；便于测试与审计）
    null_guarded: list[str] = field(default_factory=list)


def _deny(reason: str, rule_id: str) -> GuardResult:
    return GuardResult(allowed=False, reason=reason, rule_id=rule_id)


def _function_name(node: exp.Func) -> str:
    """取函数名：`Anonymous`（sqlglot 未内置的函数）用其原始标识符，其余用规范名。"""
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").lower()
    return (node.sql_name() or "").lower()


def _r3_mixed_addition(expression: exp.Expression) -> str | None:
    """红线 R3 的 **SQL 层**防线：找出「用途列 + 时段列在同一个加法表达式里相加」。

    判定单位是**每一个 `Add` 节点**：收集该节点子树里的列名，同时命中两套拆分即违规
    （嵌套加法 `(peak + flat) + (kitchen + laundry)` 在最外层节点即被捕获）。
    并列选择（`SELECT peak_kwh, kitchen_kwh`）没有 Add 节点，天然放行；
    只对一套拆分内部求和（`SUM(peak + flat + valley)`）同样放行。

    列语义映射惰性导入图表服务（单一来源：`PERIOD_SERIES` / `PURPOSE_SERIES`）；
    导入发生在首次调用时，且分析链路本就会加载 plotly，无额外冷启动成本。
    """
    from app.services.chart_service import PERIOD_SERIES, PURPOSE_SERIES

    purpose_columns = {name.lower() for name in PURPOSE_SERIES}
    period_columns = {name.lower() for name in PERIOD_SERIES}
    for add_node in expression.find_all(exp.Add):
        columns = {column.name.lower() for column in add_node.find_all(exp.Column)}
        mixed_purpose = sorted(columns & purpose_columns)
        mixed_period = sorted(columns & period_columns)
        if mixed_purpose and mixed_period:
            return (
                f"同一表达式内同时相加了「按用途」列（{'、'.join(mixed_purpose)}）与"
                f"「按时段」列（{'、'.join(mixed_period)}）——两套拆分正交，禁止混加（红线 R3）"
            )
    return None


def _null_excluded_columns(select: exp.Select) -> set[str]:
    """收集已经显式排除 NULL 的列名（`NOT (列 IS NULL)`，即 SQL 里的 `列 IS NOT NULL`）。"""
    names: set[str] = set()
    for not_node in select.find_all(exp.Not):
        inner = not_node.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            column = inner.this
            if isinstance(column, exp.Column):
                names.add(column.name.lower())
    return names


def _apply_topn_null_guard(select: exp.Select) -> list[str]:
    """给「`ORDER BY 某列 LIMIT k`（k ≤ `TOP_N_LIMIT_MAX`）」补 `AND 该列 IS NOT NULL`。

    返回被补的列名列表（没有改动时为空）。**只处理纯 `SELECT`**：UNION / 子查询外壳不动
    （结构复杂时宁可不改，避免改写语义）。
    """
    limit_node = select.args.get("limit")
    if limit_node is None:  # 没有显式 LIMIT → 不是 Top-N 语义（自动补的 LIMIT 在下一步才加）
        return []
    value = limit_node.expression
    if not isinstance(value, exp.Literal) or not value.is_int:
        return []
    if int(value.this) > TOP_N_LIMIT_MAX:
        return []

    order = select.args.get("order")
    if order is None:
        return []
    keys = [
        ordered.this
        for ordered in order.expressions
        if isinstance(ordered.this, exp.Column)  # 只处理"按某列排序"，表达式排序不动
    ]
    if not keys:
        return []

    already = _null_excluded_columns(select)
    # **必须排除"输出别名"**：`ORDER BY total DESC LIMIT 1`（`total` 是 `SUM(...) AS total`）里的
    # `total` 不是表列，若往 WHERE 里加 `NOT total IS NULL`，SQLite 会直接报 no such column
    # （写这段时实测踩到）。别名与真实列同名时保守跳过——少补一次比改坏 SQL 强。
    # **只认"真正的别名"**：纯列投影的 `alias_or_name` 返回的是列名本身，不能当成别名——
    # 否则 `SELECT date, consumption_kwh ... ORDER BY consumption_kwh LIMIT 1` 会被整体排除，
    # 修复直接失效（第一版就踩了这个坑，靠这张边界表才发现）。
    aliases = {
        projection.alias.lower()
        for projection in select.expressions
        if not isinstance(projection, exp.Column) and projection.alias
    }
    targets = [
        key for key in keys
        if key.name.lower() not in already and key.name.lower() not in aliases
    ]
    if not targets:
        return []

    for key in targets:
        select.where(
            exp.Not(this=exp.Is(this=exp.column(key.name), expression=exp.Null())),
            copy=False,
        )
    return [key.name for key in targets]


def guard_sql(sql: str | None, allowed_tables: set[str], *, limit: int = DEFAULT_LIMIT) -> GuardResult:
    """校验并规范化 SQL；通过时返回**改写后的安全 SQL**（已补 LIMIT）。"""
    text = (sql or "").strip()
    if not text:
        return _deny("SQL 为空", "sqlguard:empty")
    if len(text) > MAX_SQL_LENGTH:
        return _deny(f"SQL 过长（{len(text)} 字符，上限 {MAX_SQL_LENGTH}）", "sqlguard:too_long")

    try:
        statements = [node for node in sqlglot.parse(text, read="sqlite") if node is not None]
    except Exception as exc:  # sqlglot 的解析异常类型较杂，统一按语法错误处理
        return _deny(f"SQL 语法无法解析：{str(exc).splitlines()[0][:200]}", "sqlguard:parse_error")

    if len(statements) != 1:
        return _deny(f"只允许单条语句，检测到 {len(statements)} 条", "sqlglot:multiple_statements")

    expression = statements[0]

    # ① 语句类型：只允许 SELECT（含 WITH ... SELECT；UNION 亦为 SELECT 家族）
    if not isinstance(expression, (exp.Select, exp.Union, exp.Subquery)):
        return _deny(
            f"只允许 SELECT 查询，当前语句类型为 {type(expression).__name__.upper()}",
            "sqlglot:non_select",
        )
    for node in expression.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return _deny(
                f"SQL 中包含被禁止的操作：{type(node).__name__.upper()}",
                "sqlglot:non_select",
            )

    # ② 危险函数（`exp.Anonymous` 兜住 sqlglot 未内置的函数名，如 load_extension）
    for node in expression.find_all(exp.Func):
        name = _function_name(node)
        if name in FORBIDDEN_FUNCTIONS:
            return _deny(f"函数 {name} 被禁止", "sqlglot:forbidden_function")

    # ③ 表名白名单（含子查询 / JOIN / UNION 内的表；**CTE 别名不算真实表**）
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    tables: list[str] = []
    for table in expression.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in cte_names:
            continue
        if table.db or table.catalog:  # 跨库限定名一律拒绝
            return _deny(f"禁止跨库访问：{table.sql(dialect='sqlite')}", "sqlglot:qualified_table")
        if name not in allowed_tables:
            return _deny(
                f"表 {name} 不在当前会话可用报表范围内（可用：{', '.join(sorted(allowed_tables)) or '无'}）",
                "sqlglot:table_not_allowed",
            )
        if name not in tables:
            tables.append(name)

    if not tables:
        return _deny("查询未引用任何报表表", "sqlglot:no_table")

    # ⑤ R3 混加检测（交付物 5：用途列 + 时段列在同一加法表达式内相加 → 拒绝）
    r3_violation = _r3_mixed_addition(expression)
    if r3_violation:
        return _deny(r3_violation, "sqlguard:r3_mixed_partition")

    # ⑥ Top-N 极值自动排除 NULL（v1.19.23）：`ORDER BY 列 LIMIT k≤5` → 补 `AND 列 IS NOT NULL`。
    #    必须在"强制 LIMIT"之前判断——否则自动补的 LIMIT 会把罗列式查询误当成 Top-N。
    null_guarded: list[str] = []
    if isinstance(expression, exp.Select):
        null_guarded = _apply_topn_null_guard(expression)

    # ④ 强制 LIMIT（PRD N7：聚合查询也补，语义不变，仅作安全兜底）
    limit_applied = False
    target = expression if isinstance(expression, exp.Select) else expression
    if isinstance(target, (exp.Select, exp.Union)):
        if target.args.get("limit") is None:
            target = target.limit(limit)
            expression = target
            limit_applied = True

    safe_sql = expression.sql(dialect="sqlite")
    return GuardResult(
        allowed=True, sql=safe_sql, tables=tables,
        limit_applied=limit_applied, null_guarded=null_guarded,
    )
