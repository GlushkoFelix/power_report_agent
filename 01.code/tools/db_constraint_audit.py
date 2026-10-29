"""DB CHECK 约束 ↔ 代码取值 一致性审计（2026-10-14 新增，起因：问题 `P-036`）。

**背景**：`analyses.route` 上有 `CHECK (route IS NULL OR route IN ('sql','pandas'))`，
而失败路径曾把**空串**写进该列 → `IntegrityError` → **失败留痕丢失 + 异常上抛**
（评测侧表现为"这题没有调用记录、却是空结果"，看起来像模型答错）。
这类"**约束与代码默认值/字面量不对齐**"的写法极易复发，且触发时的现象很难反推根因。

**四段（全部只读，不改任何文件）**：

1. **DDL ↔ 实库 diff**：本项目纪律是"DDL 是表结构唯一事实源"；一旦漂移，靠 DDL 读不出真实约束；
2. **约束清单**（以实库为准）；
3. **默认值比对 → HIGH**：`col: T = "x"` / `col = "x"` / ORM `default="x"` 若 ∉ 允许集合；
   列名**跨表重名**时（`status` / `period` 等）按"所在类名是否与该表同源"判断：
   同源 → HIGH，不同源 → 归入「需确认」（人工一眼可判）；
4. **字面量比对 → WARN**：跨表重名列一律跳过（必然误报），其余列报出。

**两条实现要点（都是踩过才知道的）**：
- 约束有 `CHECK (col IN (...))` 与 `CHECK (col IS NULL OR col IN (...))` **两种写法，都要认** ——
  否则会漏掉最关键的那条（`analyses.route` 正是第二种）；
- 列名跨表重名是**误报之源**，必须带"表 / 类"上下文判断。

**自证**：`--selftest` 会在临时目录写一个 `route: str = ""` 的坏样例并喂给本脚本自身，
确认 HIGH 规则确实会命中（**规则改坏了会立刻暴露**，不会沦为"永远报 0"的摆设）。

用法：
    python 01.code\\tools\\db_constraint_audit.py            # 常规审计（退出码 0=无 HIGH，1=有 HIGH）
    python 01.code\\tools\\db_constraint_audit.py --selftest  # 自证规则有效
"""

from __future__ import annotations

import collections
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "01.code" / "scripts" / "sql"
#: 常规扫描目录；`--selftest` 时用环境变量指向"故意写坏"的临时目录
APP_DIR = pathlib.Path(os.environ.get("AUDIT_APP_DIR") or (ROOT / "01.code" / "backend" / "app"))
DBS = {"控制面": ROOT / "02.data" / "power_agent.db", "数据面": ROOT / "02.data" / "reports.db"}

#: 同时覆盖 `CHECK (col IN (...))` 与 `CHECK (col IS NULL OR col IN (...))`
CHECK_RE = re.compile(
    r"CHECK\s*\(\s*(?:[A-Za-z_][\w]*\s+IS\s+NULL\s+OR\s+)?([A-Za-z_][\w]*)\s+IN\s*\((.*?)\)\s*\)",
    re.I | re.S,
)


def parse_tables(text: str) -> list[tuple[str, str]]:
    """切出 `CREATE TABLE x ( ... )` 的表名与表体（括号配平）。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][\w]*)\s*\(", text, re.I):
        i, depth, start = m.end(), 1, m.end()
        while i < len(text) and depth:
            depth += (text[i] == "(") - (text[i] == ")")
            i += 1
        out.append((m.group(1), text[start : i - 1]))
    return out


def _allowed(raw: str) -> set[str]:
    values = {v.strip().strip("'\"").strip() for v in raw.split(",") if v.strip()}
    return {v for v in values if v.upper() != "NULL"}


def constraints_from_ddl() -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        raw = re.sub(r"--[^\n]*", "", sql_file.read_text(encoding="utf-8", errors="ignore"))
        for table, body in parse_tables(raw):
            for cm in CHECK_RE.finditer(body):
                if allowed := _allowed(cm.group(2)):
                    found[(table, cm.group(1))] = allowed
    return found


def constraints_from_db() -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for path in DBS.values():
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            con.close()
        for name, ddl in rows:
            if not ddl or "CHECK" not in ddl.upper():
                continue
            for cm in CHECK_RE.finditer(ddl[ddl.find("(") + 1 :]):
                if allowed := _allowed(cm.group(2)):
                    found[(name, cm.group(1))] = allowed
    return found


def enclosing_class(text: str, pos: int) -> str:
    """命中位置之前最近的类名（判断"这个默认值属于哪张表的类"）。"""
    names = re.findall(r"^class\s+([A-Za-z_]\w*)", text[:pos], re.M)
    return names[-1] if names else ""


def same_table(table: str, class_name: str) -> bool:
    """表名与类名是否同源（取前 4 字母比对，够用且误判代价低）。"""
    if not class_name:
        return False
    t, c = table.lower(), class_name.lower()
    return t[:4] == c[:4] or t in c or c in t


def scan_defaults(table: str, col: str, allowed: set[str]) -> list[tuple[str, str]]:
    """默认值 ∉ 允许集合 → [(级别, 描述)]。HIGH＝同源类（必然是它）；需确认＝跨表重名。"""
    hits: list[tuple[str, str]] = []
    typed = re.compile(rf"^\s*{re.escape(col)}\s*:\s*[^=\n]+=\s*(['\"])(.*?)\1", re.M)
    plain = re.compile(rf"^\s*{re.escape(col)}\s*=\s*(['\"])(.*?)\1", re.M)
    orm = re.compile(
        rf"{re.escape(col)}\s*=\s*(?:mapped_column|Column)\((?:[^()]|\([^()]*\))*?default\s*=\s*(['\"])(.*?)\1",
        re.S,
    )
    for py in sorted(APP_DIR.rglob("*.py")):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for tag, rx in (("属性默认", typed), ("属性默认", plain), ("ORM default", orm)):
            for m in rx.finditer(text):
                value = m.group(2)
                if value != "" and value in allowed:
                    continue  # 合法默认值（含空串→按"可疑"处理，见下）
                cls = enclosing_class(text, m.start())
                level = "HIGH" if same_table(table, cls) else "需确认"
                line = text[: m.start()].count("\n") + 1
                hits.append((
                    level,
                    f"{py.relative_to(APP_DIR)}:{line}　【{tag}】class={cls or '—'}　"
                    f"{col} = {value!r}　∉ {sorted(allowed)}",
                ))
    return hits


def scan_literals(col: str, allowed: set[str]) -> list[str]:
    """代码里的字面量赋值 ∉ 允许集合（跨表重名列由调用方跳过）。"""
    hits: list[str] = []
    rx = re.compile(rf"\b{re.escape(col)}\s*=\s*(['\"])(.*?)\1")
    rx2 = re.compile(rf"['\"]{re.escape(col)}['\"]\s*:\s*(['\"])(.*?)\1")
    for py in sorted(APP_DIR.rglob("*.py")):
        # 提示词文件里的示例 JSON/SQL 不是真实写库值（实测会误报 `route = 'sql'` 这类）
        if py.name == "prompts.py":
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        for m in list(rx.finditer(text)) + list(rx2.finditer(text)):
            value = m.group(2)
            # 只认"像标识符"的字面量：`'...'` / `{x}` 之类是文档或模板文本
            if value and re.fullmatch(r"[a-z_][a-z0-9_]*", value) and value not in allowed:
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{py.relative_to(APP_DIR)}:{line}　{col} = {value!r}　∉ {sorted(allowed)}")
    return hits


def main() -> int:
    ddl, db = constraints_from_ddl(), constraints_from_db()
    print("① DDL ↔ 实库 diff（DDL %d 条 / 实库 %d 条）" % (len(ddl), len(db)))
    only_db = {k: v for k, v in db.items() if k not in ddl}
    only_ddl = {k: v for k, v in ddl.items() if k not in db}
    diff_value = {k: (ddl[k], db[k]) for k in ddl.keys() & db.keys() if ddl[k] != db[k]}
    for label, items in (("只在实库里有（DDL 未沉淀）", only_db), ("只在 DDL 里有（库未生效）", only_ddl)):
        for (table, col), allowed in sorted(items.items()):
            print("   ⚠ %s：%-18s %-16s %s" % (label, table, col, sorted(allowed)))
    for (table, col), (a, b) in sorted(diff_value.items()):
        print("   ⚠ 取值集合不一致：%-18s %-16s DDL=%s 实库=%s" % (table, col, sorted(a), sorted(b)))
    if not (only_db or only_ddl or diff_value):
        print("   （一致）✓")
    print()

    print("② 默认值比对（HIGH＝同源类；重名列不同源归「需确认」）：")
    highs: list[str] = []
    unsure: list[str] = []
    for (table, col), allowed in sorted(db.items()):
        for level, desc in scan_defaults(table, col, allowed):
            if level == "HIGH":
                highs.append(f"{table}.{col} → {desc}")
                print("   ❌ [%s.%s] %s" % (table, col, desc))
            else:
                unsure.append(f"{table}.{col} → {desc}")
    if not highs:
        print("    （无）✓")
    if unsure:
        print("    ── 需人工确认（列名跨表重名，多为别的表的字段）%d 条：" % len(unsure))
        for u in unsure[:12]:
            print("       · " + u)
        if len(unsure) > 12:
            print("       · …（余 %d 条）" % (len(unsure) - 12))
    print()

    ambiguous = {col for col, n in collections.Counter(c for _t, c in db).items() if n > 1}
    print("③ 字面量比对（已跳过跨表重名列：%s）" % ("、".join(sorted(ambiguous)) or "无"))
    warns: list[str] = []
    for (table, col), allowed in sorted(db.items()):
        if col in ambiguous:
            continue
        for w in scan_literals(col, allowed):
            warns.append(f"{table}.{col} → {w}")
            print("   ⚠ [%s.%s] %s" % (table, col, w))
    if not warns:
        print("   （无）✓")
    print()
    print("结论：约束 diff %d 条｜HIGH %d 条｜需确认 %d 条｜WARN %d 条"
          % (len(only_db) + len(only_ddl) + len(diff_value), len(highs), len(unsure), len(warns)))
    print()
    print("【P-036 案例的口径】`analyses.route` 的**数据类默认值是 `sql`（合法）**；")
    print("当时写进库的空串来自**图状态的失败路径**，已由落库归一化修复 → 当前 0 HIGH 属正确结果。")
    print("规则是否真的会报，用 `--selftest` 自证（临时目录放一个 route: str = \"\" 的坏样例）。")
    return 1 if highs else 0


def selftest() -> int:
    """自证：把"坏样例"喂给本脚本自身（子进程），确认 HIGH 规则确实会命中。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="audit_selftest_"))
    (tmp / "bad_case.py").write_text('class AnalysisResult:\n    route: str = ""\n', encoding="utf-8")
    env = dict(os.environ, AUDIT_APP_DIR=str(tmp), PYTHONIOENCODING="utf-8")
    out = subprocess.run(
        [sys.executable, __file__],
        capture_output=True,
        # ⚠️ 必须显式指定 utf-8：Windows 上 text=True 会按 GBK 解码 → UnicodeDecodeError（实测踩过）
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    text = (out.stdout or "") + (out.stderr or "")
    hit = "analyses.route" in text and "HIGH 1 条" in text
    print("selftest 目录：%s" % tmp)
    print('坏样例：class AnalysisResult:  route: str = ""')
    print("结果：%s" % ("✅ 规则命中（HIGH 1 条）" if hit else "❌ 规则未命中，需修规则"))
    if not hit:
        print("子进程输出尾部：%s" % text[-600:])
    return 0 if hit else 1


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
