# 实训3 基于AI共创的智电析报系统架构与数据库设计文档编写

## 一、实训目标

1. 掌握用「架构约束 + 分层契约 + 部署形态」驱动 AI 产出系统架构设计文档的方法
2. 学会设计前后端分离的 B/S 四层架构，并写清层间契约（谁读写哪个库、谁负责守卫）
3. 能够把业务规则转化为数据库设计：两库分离、外键与级联矩阵、索引清单、容量估算
4. 掌握用 Mermaid 画架构/时序图并用脚本离线导出 PNG（可在无网环境交付）
5. 建立「文档与实现一致性」的核对习惯：组件表逐项对代码、附录目录树对实际文件

## 二、实训内容

### 1. 背景知识

- **四层架构（本项目的分层口径）**：
  ① **接入层**：React 前端（五页 + SSE 客户端）；
  ② **接口层**：FastAPI 路由（39 条端点）+ SSE 流式响应；
  ③ **服务与 Agent 层**：分析编排、slot 解析、双工具路由、SQL/代码守卫、沙箱执行、图表与解读；
  ④ **数据层**：两个 SQLite 库（分析对象 + 系统数据）。
- **两库分离与只读原则（本项目安全基石）**：
  - `reports.db`：只放报表数据（5 张预置表 + 用户上传的动态表），**应用侧只读**——
    连接层 `mode=ro`、语句层 `PRAGMA query_only`、表名层白名单，三重保证；
  - `power_agent.db`：用户、会话、消息、分析记录、报表元数据、评测题库与批次，读写。
- **唯一的写通道**：用户上传 CSV 入库时，由上传入库服务用**独占写连接**（原生 sqlite3 +
  进程内写锁 + 分批提交）完成；**LLM 生成的 SQL 与 pandas 代码永远只读**。
  这条边界决定了"AI 生成的东西能造成多大破坏"的上限，是架构设计的重点。
- **ADR（Architecture Decision Record）**：把"为什么这样选"写下来，格式为
  **决策 / 备选方案 / 理由 / 代价与缓解**。记录**否决方案的理由**尤其重要——
  评审最常问的就是"为什么不用 Docker / 为什么不用 LangGraph"。
- **图即文档**：Mermaid 源码留在 Markdown 内（可改、可 diff），PNG 由脚本离线导出
  （可交付、可在无网环境打开）；两者必须同源，改一处要重导一次。
- **"规划"与"落地"必须分开写**：架构文档天然会先写理想组件（如 `sandbox.py`、
  `agent/graph.py`），实现时方案可能变（本项目最终是 `services/sandbox_service.py` 子进程隔离、
  没有 graph.py）。**组件清单必须带「落地状态」列**，否则文档就成了"想象图"。

### 2. 项目结构（本实训产出与依赖）

```
04.实训指导书/
├── AI共创过程文档/
│   ├── 智电析报_系统架构设计文档.md      # 本实训产出（11 章 + 附录 A/B）
│   └── 智电析报_数据库设计文档.md        # 本实训产出（12 章）
├── images_arch/                          # 架构图与流程图 PNG（11 张：6 流程 + 5 时序）
└── images2/                              # 线框图（实训2）
01.code/
├── scripts/sql/{reports.sql,power_agent.sql,power_agent_langgraph.sql}  # DDL 事实源（5 + 12 = 17 张表）
├── scripts/{_common.py,01_build_reports.py,02_build_schema.py,03_init_db.py,
│            04_build_eval_set.py,05_migrate_audit_events.py}
├── tools/mermaid2png.py                         # Mermaid → PNG（离线）
└── backend/app/{api,services,core,agent,db,schemas}/  # 组件与文档一一对应
```

产物与章节对应关系：

| 步骤 | 产出章节 | 关键产物 |
| --- | --- | --- |
| 步骤1 | 架构文档 2 章 | 架构目标与约束（含"不引入什么"） |
| 步骤2 | 架构文档 3–4 章 | 四层架构图 + 组件清单表（带落地状态） |
| 步骤3 | 数据库文档 | 两库 17 张表（5 报表 + 12 系统，含 2 张 LangGraph 检查点表）、级联矩阵、索引、容量、初始化流程 |
| 步骤4 | 架构文档 9 章 | ADR-001 起逐条（本项目 21 条，含 ADR-021「接入状态图」） |
| 步骤5 | 架构文档 7 章 + `images_arch/` | 时序图源码 + 11 张 PNG |
| 步骤6 | 两份文档的变更记录 | 一致性核对与修正记录 |

## 三、实训步骤

### 步骤1：交代架构约束

**操作命令：**

```powershell
cd e:\power_report_agent
New-Item -ItemType File -Force "04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md"
```

**提示词（可直接复制）：**

```text
请为"智电析报"编写《系统架构设计文档》，第一步只写「架构目标与约束」：
背景：对话式能源报表分析 Web 应用，前后端分离，单机 Windows 演示（局域网可访问）。
必须写清的约束（每条给出"因为这条约束，方案上排除了什么"）：
1. 前后端分离的 B/S 架构，前端只通过 HTTP/SSE 与后端交互；
2. 两个 SQLite 库分离：reports.db 只读（分析对象）、power_agent.db 读写（系统数据）；
3. 部署形态：本机 Windows，直接跑 Python 进程 + 前端开发服务器，不引入 Docker / Nginx；
4. LLM 生成的 SQL 与 pandas 代码必须在受控环境执行（只读 + 沙箱）；
5. 流式响应：分析过程用 SSE 推送，前端要能显示阶段进度。
只输出第 2 章，不要写组件与表结构。
```

**验证方法：**

```powershell
Test-Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md"
# 预期输出: True

# 约束是否写全（"不引入 Docker/Nginx" 这类排除项必须显式写出）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "Docker|Nginx|只读|SSE"
# 预期输出: 命中若干行，含"不引入"字样
```

**成功标志：**

- 文档存在，且**约束条数 ≥5**，每条都能回答"排除了什么方案"
- 写明部署形态（单机 Windows、局域网可访问）与安全底线（只读 + 沙箱）

### 步骤2：生成四层架构与组件清单

**操作命令：**

```powershell
# 先看后端真实模块（组件表必须与它逐项对得上）
Get-ChildItem e:\power_report_agent\01.code\backend\app\services\*.py | Select-Object -ExpandProperty Name
Get-ChildItem e:\power_report_agent\01.code\backend\app\core\*.py | Select-Object -ExpandProperty Name
```

**提示词（可直接复制）：**

```text
请编写《系统架构设计文档》的「四层系统架构」与「后端组件与 Agent 流水线」两章：
1. 四层架构：接入层（React）/ 接口层（FastAPI + SSE）/ 服务与 Agent 层 / 数据层（两库）；
   用 Mermaid flowchart 画分层图，标注跨层调用方向；
2. 组件清单表（列：文件 / 职责 / 依赖 / 落地状态），落地状态取值只能是
   「已落地 / 部分落地 / 未落地（原因）」；
3. Agent 流水线：说明一次提问经过哪些阶段（slot 解析 → 工具选择 → 生成 → 守卫 → 执行 →
   图表 → 解读），并标注哪些阶段会调用大模型。
要求：组件表里的每个文件路径都必须真实存在；不确定的写"未落地"，不要写成已完成。
```

**验证方法：**

```powershell
# 组件表里的 services 文件与磁盘逐项对照（两边应一致，无虚构项）
$doc = "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md"
Select-String -Path $doc -Pattern "app/services/[a-z_]+\.py" -AllMatches |
  ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
Get-ChildItem e:\power_report_agent\01.code\backend\app\services\*.py | ForEach-Object { "app/services/" + $_.Name } | Sort-Object
# 预期输出: 两份清单应一致（services 14 个模块）

# 落地状态列存在
Select-String -Path $doc -Pattern "落地状态|未落地"
# 预期输出: 命中
```

**成功标志：**

- 组件清单含**落地状态**列，且存在"部分落地/未落地"标注（全部写"已落地"基本可判定为未核对）
- 组件表里的**每个路径都能在仓库里找到**（反之，仓库里的 services 模块也没有漏写）
- 四层架构图是 Mermaid `flowchart`（源码在文档内，可改可 diff）

### 步骤3：生成数据库设计（两库 15 张表）

**操作命令：**

```powershell
# 看 DDL 事实源（唯一权威）
Get-Content e:\power_report_agent\01.code\scripts\sql\power_agent.sql | Select-Object -First 40
# 按 DDL 重建两库（幂等；--force 会删除既有库文件后重建）
.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force
```

**提示词（可直接复制）：**

```text
请编写《数据库设计文档》，覆盖 12 章：
1. 设计目标与原则（两库分离、只读约束、口径版本字段）；
2. 总体设计（两库职责边界表：库 / 放什么 / 访问模式 / 谁来写）；
3. reports.db：5 张报表表（分钟 / 小时 / 日 / 月 / 用电结构），逐表给字段、类型、单位、
   可否聚合；特别标注"按用途"与"按时段"两套拆分不可混加（口径红线 R3）；
4. power_agent.db：10 张系统表（users / uploaded_reports / upload_files / conversations /
   messages / analyses / llm_calls / audit_logs / eval_questions / eval_runs）；
5. 参照完整性与删除行为矩阵（哪些外键 ON DELETE CASCADE、哪些 SET NULL、为什么）；
6. 索引设计汇总（每条索引对应哪个真实查询）；
7. 容量与性能估算（按真实行数与体积）；
8. 初始化、迁移与校验流程（脚本顺序与幂等性）。
要求：字段名必须与 DDL 一致；不要发明字段。
```

**验证方法：**

```powershell
# 系统库真实表数（排除 SQLite 内部表 sqlite_sequence）
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/power_agent.db');print(len(c.execute('select name from sqlite_master where type=? and name not like ?',('table','sqlite_%')).fetchall()))"
# 预期输出: 12（10 张业务表 + 2 张 LangGraph 检查点表 checkpoints / writes）

# 报表库真实表数
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/reports.db');print(len(c.execute('select name from sqlite_master where type=? and name not like ?',('table','sqlite_%')).fetchall()))"
# 预期输出: 5

# 文档里的表名与库里一致（逐个对照，无虚构字段）
.venv\Scripts\python.exe -c "import sqlite3;[print(n) for (n,) in sqlite3.connect('02.data/power_agent.db').execute('select name from sqlite_master where type=? and name not like ? order by name',('table','sqlite_%'))]"
```

**成功标志：**

- 系统库 **10 张表**、报表库 **5 张表**（合计 15），与文档清单**逐名一致**
- 删除行为矩阵覆盖全部外键（`ON DELETE` 取值逐个有理由）
- 文档中每条索引都能指出"服务于哪个查询"（不写无用索引）

### 步骤4：写 ADR（决策记录）

**操作命令：**

```powershell
# 看现有 ADR 条数（本项目 20 条）
(Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "^\| ADR-\d+").Count
```

**提示词（可直接复制）：**

```text
请为《系统架构设计文档》补「架构决策记录（ADR）」一章，从 ADR-001 开始逐条编号，
每条一张表：决策 / 备选方案 / 选择理由 / 代价与缓解措施。
至少覆盖这些已发生的选择：
1. 两库分离（而不是单库分表）；2. 不引入 Docker（单机 Windows 演示）；
3. 沙箱用子进程隔离而不是线程（Windows 无 unshare，要能真的杀掉死循环）；
4. SQL 守卫用 sqlglot 白名单（而不是正则匹配）；
5. 图表由后端产 figure、前端注入主题（而不是后端写死颜色）；
6. **引入 LangGraph 状态图**（`app/agent/graph.py`：13 节点 + 条件边；曾按 ADR-017 用显式阶段编排，2026-10-13 起按 **ADR-021** 接入并成为默认）。
要求：每条都要写"为什么否决备选方案"，不要只写选了哪个。
```

**验证方法：**

```powershell
# ADR 条数（应与实际一致）
(Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "^\| ADR-\d+").Count
# 预期输出: 20

# 抽查是否写了"否决理由"（每个备选方案都要有理由）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "否决|不选|代价" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 明显大于 0（每条 ADR 至少一处）
```

**成功标志：**

- ADR **逐条编号无跳号**，每条含四项：决策 / 备选 / 理由 / 代价与缓解
- 至少 3 条写明了**否决方案的具体理由**（如"Windows 无 unshare，线程无法真正终止死循环"）
- ADR 与后文实现一致（如写了"子进程隔离"，组件表里就应有 `sandbox_service.py`）

### 步骤5：画关键时序图并用脚本导出 PNG

**操作命令：**

```powershell
cd e:\power_report_agent
New-Item -ItemType Directory -Force 04.实训指导书\images_arch
.venv\Scripts\python.exe 01.code\tools\mermaid2png.py "04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" "04.实训指导书\images_arch"
```

**提示词（可直接复制）：**

```text
请在《系统架构设计文档》的「关键时序」一章补 5 张 Mermaid sequenceDiagram：
1. 提问全链路（含守卫拦截与重写分支）；
2. 上传入库（sha256 去重分支 + 分批提交）；
3. 批量评测（逐题循环 + 报告生成）；
4. 澄清回路（澄清卡 → 回填 → 原地继续执行）；
5. 回归自检（ui_flow_check 断言链）。
约定：每张图**上一行**写 HTML 注释 <!-- name: seq_xxx --> 指定输出 PNG 文件名；
消息文本避免 **、--、±、… 等字符（会导致渲染失败）；每张图都要有异常分支。
```

**验证方法：**

```powershell
# 逐张导出（脚本会打印每张 [ok]）
.venv\Scripts\python.exe 01.code\tools\mermaid2png.py "04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" "04.实训指导书\images_arch"
# 预期输出: 逐张 [ok]，末尾"完成：成功 N / N"

# 产出的 PNG 数量（本项目 11 张 = 6 流程图 + 5 时序图）
(Get-ChildItem "e:\power_report_agent\04.实训指导书\images_arch\*.png").Count
# 预期输出: 11

# 防"错误页"：所有 PNG 体积不应完全相同（错误页会导致尺寸一致）
Get-ChildItem "e:\power_report_agent\04.实训指导书\images_arch\*.png" | Select-Object Name, Length
```

**成功标志：**

- 脚本对每张图输出 `[ok]`，无单独失败项
- `images_arch/` 得到 **11 张 PNG**，且体积各不相同（避免渲染成错误页）
- 文档内的 Mermaid 源码与 PNG 一一对应（改图后重导一次）

### 步骤6：一致性核对（最容易漏但最关键）

**操作命令：**

```powershell
# 四个对照面：组件表 vs 目录、ADR vs 实现、附表 vs 依赖、版本引用
Get-ChildItem e:\power_report_agent\01.code\backend\app\agent\*.py
# 预期输出: 只有 prompts.py —— 若文档写了 graph.py，就是"规划未落地"，必须标注
```

**提示词（可直接复制）：**

```text
请对我给的《系统架构设计文档》与《数据库设计文档》做一次一致性核对，逐项给出
「文档写的 / 实际有的 / 结论（一致 / 需改）」三列，重点核对四类：
1. 组件清单 vs 代码目录（有没有文档写了但代码里没有的、代码里有但文档漏的）；
2. 依赖清单 vs requirements.txt 与 pip list（尤其别把"从未安装"的框架写进依赖表）；
3. 数据库附录表清单 vs DDL 与真实库（字段名、表数量）；
4. 四份文档头部的版本互相引用是否闭环。
核对结果不要直接改文档，先给我清单，我确认后再改。
```

**验证方法：**

```powershell
# 依赖双向核对：文档声明的依赖 vs 实际安装
Get-Content e:\power_report_agent\01.code\backend\requirements.txt
.venv\Scripts\python.exe -m pip list 2>$null | Select-String -Pattern "fastapi|sqlalchemy|plotly|restricted|pandas|sse-starlette"

# 文档是否如实标注了未落地项
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "未落地|不符|偏差"
# 预期输出: 命中（本项目如实标注过"无 graph.py、未引入 LangGraph"；2026-10-13 起已接入状态图）
#           架构文档 6.3 保留了"偏差 → 消解"的完整记录，可用它核对本次反转
```

**成功标志：**

- 四类核对各产出一张"文档 / 实际 / 结论"清单，**修正项写进文档变更记录**
- 文档中不再出现"写了但代码里没有"的组件（或明确标注为"未落地 + 原因"）
- `requirements.txt` 与实际安装**双向无缺漏**（既不多写未装的，也不少写已装的）

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 架构文档存在 | `Test-Path "...\智电析报_系统架构设计文档.md"` | True |
| 四层架构齐备 | 检索"接入层/接口层/服务与 Agent 层/数据层" | 4 层齐备 |
| 组件表与目录一致 | `Get-ChildItem app\services\*.py` 与组件表逐项比对 | 无缺项、无虚构 |
| 落地状态列存在 | 检索"落地状态" | 命中（含"未落地"标注） |
| ADR 条数 | `Select-String "^\| ADR-\d+"` 计数 | 20 |
| DDL 与文档一致 | 两库真实表数 vs 文档清单 | 10 + 5 = 15 |
| 架构图可复现 | `mermaid2png.py <架构文档> <输出目录>` | 全部 `[ok]` |
| 图数量 | `Get-ChildItem images_arch\*.png` 计数 | 11 |
| 依赖一致 | `requirements.txt` vs `pip list` | 双向无缺漏 |
| 版本闭环 | 四份文档头部互相标注版本 | 互相指向有效版本 |

## 五、故障排除

### 问题1：文档写了组件但代码里没有

**现象：** 架构文档写了 `app/core/sandbox.py` + RestrictedPython、`agent/graph.py` + checkpointer，
实际仓库里是 `services/sandbox_service.py`（子进程隔离）、**没有 graph.py**、也没引入 LangGraph。
> **现状注记（2026-10-13）**：`agent/graph.py` **已落地**（LangGraph 状态图 + 检查点，见 ADR-021）。
> 本条作为"**规划先于实现、文档没回改**"的教学案例保留——它也正是"组件清单加落地状态列"这个设计的由来。

**原因：** 架构文档先于实现写成，实现时方案变了（更贴近 Windows 约束），但文档没回改；
组件表也没有"落地状态"列，读者无法分辨规划与实现。

**解决方案：** 组件清单加**「落地状态」列**，逐个对目录核对；不一致的以代码为准改文档，
并在变更记录里写明。

```powershell
# 逐项对照（两边应一致）
Get-ChildItem e:\power_report_agent\01.code\backend\app\services\*.py | ForEach-Object { $_.Name }
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "app/services/[a-z_]+\.py" -AllMatches | ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
```

### 问题2：附表声明了未安装的依赖

**现象：** 依赖表曾把 LangGraph / langchain 写成"技术栈"，实际从未安装；
`requirements.txt` 又漏了确实在用的 plotly / narwhals / RestrictedPython。

**原因：** 依赖表按"计划技术栈"写，没跟实现对齐；`requirements.txt` 靠追加维护，漏项。

**解决方案：** 以 `pip list` 与 `requirements.txt` **双向核对**（既查"写了没装"，也查"装了没写"）。

```powershell
Get-Content e:\power_report_agent\01.code\backend\requirements.txt
.venv\Scripts\python.exe -m pip list 2>$null | Select-String -Pattern "plotly|narwhals|restricted|pandas|fastapi"
# 预期输出: 两侧逐项能对上；对不上的项要么补进 requirements，要么从文档删除
```

**本项目已修正（可作对照）**：`requirements.txt` 现在显式登记 `plotly==7.0.0`、`narwhals==2.26.0`、
`RestrictedPython==8.5`，并对**刻意不装**的依赖逐条写明原因：

| 刻意不装的依赖 | 原因 |
| --- | --- |
| `kaleido` | 服务端图片导出为**预留能力**（装上即生效，未装时接口返回 501） |
| `openai` SDK | 兼容端点用已装的 `httpx` 直调，装 `openai` 会额外拖 4 个包 |
| `LangGraph` / `LangChain` —— **已反转（值得单独讲）** | **已接入**：`langgraph 1.2.14` + `langgraph-checkpoint-sqlite 3.1.2`，`app/agent/graph.py` 状态图（默认编排）。**保留这段历史是为了说明"刻意不装"也可能被新收益推翻**——反转动因见 **ADR-021**（条件边 / 检查点 / HITL），而不是"顺手装上" |

**结论：连"没装"也要写清理由**——否则下一位维护者会以为是漏项，顺手装上就把架构决策改掉了。

### 问题3：Mermaid 渲染出来的图是"错误页"

**现象：** `mermaid2png.py` 报每张 `[ok]`，但 PNG 全是同一张错误提示图
（所有文件体积几乎一样）。

**原因：** 渲染用的临时目录在浏览器加载前被删除，导致 `ERR_FILE_NOT_FOUND`。
（本项目已修：整轮渲染期间保持临时目录，并轮询等待文件落盘。）

**解决方案：** 升级脚本到"整轮保持临时目录 + 轮询等落盘"的版本；验收时**看体积**：

```powershell
Get-ChildItem "e:\power_report_agent\04.实训指导书\images_arch\*.png" | Select-Object Name, Length
# 预期输出: 11 张，体积各不相同（若都一样，先怀疑错误页而不是图内容）
```

### 问题4：图里有语法错误导致该张失败

**现象：** 11 张图里总有 1 张导出失败，或产出的图内容不完整。

**原因：** 时序图消息文本里用了 Mermaid 的保留字符：`**`、`--`、`±`、`…`、`:` 等。

**解决方案：** 消息文本改成朴素中文；失败时单独复现该张（脚本按图名逐张处理，不影响其它图）。

```powershell
# 单独复现：检查该图的 HTML 注释名与代码块
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" -Pattern "name: seq_|name: arch_" -Context 1,1
# 预期输出: 每张图的 name 注释与其 mermaid 代码块一一对应
```

### 问题5：DDL 改了但文档没改

**现象：** 新增字段（如审计事件白名单扩容、`is_pinned` 置顶字段）后，
数据库设计文档的字段清单与实际情况不符。

**原因：** DDL 是事实源，但改 DDL 时只跑了建库脚本，没回改文档。

**解决方案：** 建立"**改 DDL 三件事**"惯例——
① 改 `scripts/sql/*.sql`；② 重跑 `03_init_db.py` 并核对表结构；③ 回填数据库设计文档 + 变更记录。

```powershell
# 重建并按真实库核对表数量
.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/power_agent.db');print(len(c.execute('select name from sqlite_master where type=? and name not like ?',('table','sqlite_%')).fetchall()))"
# 预期输出: 12（10 张业务表 + 2 张 LangGraph 检查点表 checkpoints / writes）
```

### 问题6：`python -c` 里查询 SQL 的引号被 PowerShell 吃掉

**现象：** 验证命令报 `SyntaxError: unterminated string literal` 或 SQL 变成
`type=table`（引号消失了）。

**原因：** PowerShell 会先解析双引号与反斜杠转义，`python -c "... \"select ...\" ..."` 里的
内层引号会被吃掉。

**解决方案：** `-c` 外层用双引号，**内层 SQL 一律用单引号**，并用 `?` 占位符传参（不在 SQL 里写字面量）：

```powershell
# 反例（会被 PowerShell 吃掉引号）：python -c "... c.execute(\"select ... where type='table'\") ..."
# 正例：SQL 内不出现双引号，值用参数占位符
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/power_agent.db');print(len(c.execute('select name from sqlite_master where type=? and name not like ?',('table','sqlite_%')).fetchall()))"
# 预期输出: 12
```

## 六、参考代码

### 完整目录结构

```
01.code/
├── scripts/
│   ├── sql/
│   │   ├── reports.sql              # 5 张报表表（DDL 事实源）
│   │   └── power_agent.sql          # 10 张系统表（DDL 事实源）
│   ├── _common.py                   # 路径常量与解析清洗（01/03 共用，口径单一来源）
│   ├── 01_build_reports.py          # 原始数据 → 5 张报表 + 质量报告 + 口径锚点
│   ├── 02_build_schema.py           # → Schema 结构化描述 + 注入文本
│   ├── 03_init_db.py                # 按 DDL 建库（--force 重建）
│   ├── 04_build_eval_set.py         # → 120 题评测集
│   └── 05_migrate_audit_events.py   # 审计事件白名单扩容（幂等迁移）
├── tools/
│   ├── mermaid2png.py               # Mermaid → PNG（离线，Edge 无头）
│   └── svg2png.py                   # SVG → PNG（线框图用）
└── backend/app/
    ├── api/v1/{router.py,endpoints/}          # 39 条端点（接口层）
    ├── services/                              # 服务层（14 个模块）
    │   ├── analysis_service.py                # 分析编排主链路
    │   ├── slot_service.py                    # slot 解析 / 合并 / 澄清判定
    │   ├── schema_service.py                  # Schema 注入文本
    │   ├── chart_service.py                   # 图表 figure 构造
    │   ├── sandbox_service.py                 # pandas 子进程沙箱
    │   ├── report_write_service.py            # 上传入库（唯一写通道）
    │   ├── eval_service.py                    # 评测批次与判分
    │   └── …（auth / conversation / audit / report / upload_parse / analysis_record / report_pdf）
    ├── core/                                  # 7 个模块
    │   ├── sql_guard.py                       # SQL 只读白名单 + 混加检测
    │   ├── code_guard.py                      # pandas 代码 AST 预检
    │   ├── llm_client.py                      # 多厂商 OpenAI 兼容适配
    │   ├── locks.py / security.py / errors.py / config.py
    ├── agent/                                 # state.py / adapters.py / graph.py（StateGraph 编排）
    │                                          #   / nodes/ / checkpointer.py / dispatch.py
    │   └── prompts.py                         # 提示词资产（编排为 LangGraph 状态图，见 ADR-021）
    ├── db/{models,session.py}                 # ORM 映射（不调 create_all）
    └── schemas/                               # 请求/响应模型
```

### 常用命令

```powershell
# 按 DDL 事实源重建两库（幂等；--force 删除既有库文件后重建）
.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force

# 数据资产重建（顺序不可颠倒）
.venv\Scripts\python.exe 01.code\scripts\01_build_reports.py
.venv\Scripts\python.exe 01.code\scripts\02_build_schema.py

# Mermaid → PNG（架构图与流程图，11 张）
.venv\Scripts\python.exe 01.code\tools\mermaid2png.py "04.实训指导书\AI共创过程文档\智电析报_系统架构设计文档.md" "04.实训指导书\images_arch"

# 线框图 SVG → PNG（实训2 线框图，7 张）
.venv\Scripts\python.exe 01.code\tools\svg2png.py "04.实训指导书\images2"

# 两库真实表数（排除 SQLite 内部表）
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/power_agent.db');print(len(c.execute('select name from sqlite_master where type=? and name not like ?',('table','sqlite_%')).fetchall()))"

# 查看 DDL 事实源
Get-Content 01.code\scripts\sql\power_agent.sql | Select-Object -First 40
```

### 关键配置说明

| 参数 | 说明 | 本项目取值 |
|--------|------|-----------|
| 两库职责 | 分析对象 / 系统数据 | `reports.db` 只读；`power_agent.db` 读写 |
| 只读三重保证 | 连接层 / 语句层 / 表名层 | `mode=ro` + `PRAGMA query_only`；sqlglot 仅 SELECT；表白名单 |
| 写通道 | 唯一写入口 | 上传入库服务（原生 sqlite3 + 进程内写锁 + 分批提交） |
| 表数量 | 报表库 + 系统库 | 5 + 12 = 17（不含 SQLite 内部表 `sqlite_sequence`；系统库含 2 张 LangGraph 检查点表） |
| ADR 数量 | 决策留痕 | ADR-001 – **ADR-021**（21 条） |
| 架构图 | 源码在文档内、PNG 落盘 | 11 张（6 流程 + 5 时序） |
| 编排方式 | Agent 流水线 | **LangGraph 状态图**（`app/agent/graph.py`：13 节点 + 条件边；`ORCHESTRATOR` 默认 `langgraph`，`legacy` 一行回退，见 **ADR-021**） |
