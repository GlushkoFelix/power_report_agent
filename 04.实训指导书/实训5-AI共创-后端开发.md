# 实训5 基于AI共创的智电析报后端开发

## 一、实训目标

1. 掌握用 FastAPI 从零搭建分层后端（接口层 / 服务层 / 守卫与沙箱 / 数据层）与统一响应包络
2. 学会两库连接策略：控制面读写（WAL）与数据面只读（`mode=ro` + `query_only`），并实测只读拒绝写入
3. 能够实现鉴权（bcrypt + JWT）、报表读路径与**上传两段式写路径**（解析预览 → 确认入库）
4. 掌握分析链路：slot 增量抽取 → 生成 SQL/代码 → **只读守卫** → 沙箱执行 → 图表 → 解读 → 全量留痕
5. 能接入任意 OpenAI 兼容大模型厂商，并如实回报"当前是否配置真实模型"
6. 能实现 120 题评测：判分三级兜底、指标汇总、bad case 归因与 Markdown 报告

## 二、实训内容

### 1. 背景知识

- **统一响应包络（双轨制）**：所有接口返回 `{code, message, data, request_id}`，
  其中 `code` 与 HTTP 状态码同值（成功为 0），**业务错误码放 `data.error_code`**（`E_*`）。
  好处：HTTP 语义不被破坏（网关、日志、监控照常工作），同时前端能拿到精确的业务错误分类与
  `data.hint`（给用户的下一步建议）。
- **三连接策略（安全与性能一起考虑）**：
  ① **控制面读写**（`power_agent.db`，WAL 模式）；
  ② **数据面只读**（`reports.db`，`mode=ro` + `PRAGMA query_only`，用 NullPool 避免连接跨事件循环复用）；
  ③ **报表库独占写**（仅上传入库服务使用，原生 sqlite3 + 进程内写锁 + 分批提交）。
  LLM 生成的东西只走 ②，永远碰不到 ③。
- **两道守卫（生成之后、执行之前）**：
  - `sql_guard`（**sqlglot 语法树**判定，不用正则）：单语句、仅 `SELECT`、表名白名单、
    强制追加 `LIMIT`、**R3 混加检测**（"按用途"与"按时段"两套拆分不可同列相加）、
    求极值时的 Top-N 自动补 `IS NOT NULL`；
  - `code_guard`（pandas 路径）：AST 预检 —— 禁 `import` 非白名单、禁 dunder 属性、
    禁 `open`/`eval`/`exec`/`__import__`/`subprocess`/`socket`。
- **沙箱执行**：AST 预检不过**不进沙箱**；过了才在**独立子进程**里跑
  （超时强杀、崩溃重建、结果行数上限、内存上限、禁网禁写）。
  选子进程而不是线程的原因很实在：**Windows 上没有 `unshare`，线程无法真正终止死循环**。
- **全量留痕**：一次问答落 `messages`（用户 + 助手）、`analyses`（SQL/代码、结果快照、解读、
  重试次数、路径切换）、`llm_calls`（逐阶段 token，含 `is_retry`），并回写
  `conversations.slot_json`（多轮继承的载体）。
- **无 Key 也能跑通**：未配置模型 Key 时回落离线脚本模型（`ScriptedLLM`），
  链路、留痕与回归测试照常执行；**但评测的 `impl=real` 不会静默回落**，直接返回 400 并给出 hint。
- **模型分级（PRD 10.3）**：slot 抽取用**轻量模型**，SQL 生成与解读用**主力模型**——
  前者是短输出结构化任务，后者才需要推理能力。
- **"能机检的就别指望模型"**：本项目在分析链路上逐步加了三道**确定性兜底**——
  slot 的**规则快通道**（简单问法直接产出增量，省一次往返）、**指标词面兜底**
  （模型判"缺指标"但问题里明明写了就驳回）、**结果形态自检**
  （问占比却回明细行 / 问极值却回明细行 / 筛选型追问丢掉分组维度 ⇒ 带原因重写一次）。

### 2. 项目结构

```
01.code/backend/
├── app/
│   ├── main.py                     # 入口 / 中间件 / 异常处理器 / 启动回收 pending
│   ├── api/v1/{router,deps}.py + endpoints/   # 39 条端点，7 个模块
│   │                                            #   auth / reports / conversations /
│   │                                            #   analysis / charts / eval / system
│   ├── core/                       # 7 个模块
│   │   ├── config.py               # .env 驱动配置（路径按项目根解析）
│   │   ├── errors.py               # 错误码定义（E_*）与异常基类
│   │   ├── security.py             # bcrypt 哈希与 JWT
│   │   ├── locks.py                # 进程内写锁（上传入库用）
│   │   ├── llm_client.py           # 多厂商 OpenAI 兼容适配 + 离线脚本模型
│   │   ├── sql_guard.py            # SQL 只读白名单（sqlglot）+ R3 混加检测
│   │   └── code_guard.py           # pandas 代码 AST 预检
│   ├── agent/                      # state.py / adapters.py / graph.py（StateGraph）/ nodes/
│   │                               #   / checkpointer.py（检查点）/ dispatch.py（ORCHESTRATOR 开关）
│   ├── agent/prompts.py            # 提示词集中管理（含 prompt 版本号）
│   ├── db/session.py + db/models/  # 三连接 + 10 张业务表的 ORM 映射（不调 create_all）
│   │                               #   注：LangGraph 检查点表（checkpoints/writes）无 ORM，DDL 手工纳管
│   ├── schemas/                    # 统一包络 + 各组 Pydantic 请求/响应模型
│   └── services/                   # 14 个服务模块
│       ├── analysis_service.py     # 分析编排主链路（含守卫、重写环、形态自检）
│       ├── slot_service.py         # slot 解析/合并/澄清判定 + 规则快通道
│       ├── schema_service.py       # Schema 注入文本
│       ├── chart_service.py        # 图表 figure 构造（语义色、TopN、抽稀）
│       ├── sandbox_service.py      # pandas 子进程沙箱
│       ├── report_write_service.py # 上传入库（唯一写通道）
│       ├── upload_parse.py         # CSV 解析与规范化中间文件
│       ├── report_service.py       # 报表读路径
│       ├── report_pdf.py           # 报告 PDF 导出
│       ├── conversation_service.py / analysis_record_service.py
│       ├── eval_service.py         # 评测批次 / 判分 / 报告
│       └── auth_service.py / audit_service.py
├── requirements.txt / .env.example
└── tests/                          # 16 个测试文件（+ conftest.py），共 323 条用例
```

## 三、实训步骤

### 步骤1：工程骨架与两库连接

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
New-Item -ItemType Directory -Force app\api\v1\endpoints, app\core, app\services, app\schemas, app\db\models, app\agent
New-Item -ItemType File -Force app\main.py, app\core\config.py, app\db\session.py
```

**提示词（可直接复制给 AI）：**

```text
请搭建 FastAPI 后端骨架（分层：api / services / core / db / schemas / agent），实现：
1. 统一响应包络 {code, message, data, request_id}：成功 code=0；业务错误 code 与 HTTP 状态同值，
   业务错误码（E_*）放 data.error_code，并支持 data.hint（给用户的下一步建议）；
2. .env 驱动配置（pydantic-settings）：库路径**按项目根解析**（不依赖启动时的工作目录）；
3. 三连接工厂：① 控制面读写（power_agent.db，WAL）；② 数据面只读
   （reports.db，mode=ro + PRAGMA query_only，NullPool）；③ 报表库独占写（仅上传服务用）；
4. 请求级 request_id 中间件（写响应头，便于按 rid 串联日志）；
5. GET /api/v1/system/health：回报进程、两库可达性、以及**模型配置状态**
   （configured / provider / protocol / key_source），未配置也要如实回报而不是假装正常。
```

**验证方法：**

```powershell
Set-Location e:\power_report_agent\01.code\backend
$env:PYTHONIOENCODING = "utf-8"
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另开一个窗口：
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | ConvertTo-Json -Depth 4
# 预期输出: code=0；data.status=healthy；data.databases.reports/power_agent=ok；
#          data.llm.configured=true|false（如实回报）
```

**成功标志：**

- `/system/health` 返回 `status=healthy`，两库均 `ok`
- **只读库确实拒绝写入**（用 sqlite3 直连 `reports.db` 执行 `INSERT` 会报 `attempt to write a readonly database`）
- 未配置模型 Key 时 `llm.configured=false`（不是报错、也不是假装 true）

### 步骤2：鉴权（注册 / 登录 / 锁定 / 数据隔离）

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
# 生成 JWT 密钥（缺失则启动报错——刻意设计，避免每次重启换密钥导致已签发 token 全失效）
..\..\.venv\Scripts\python.exe -c "import secrets;print(secrets.token_urlsafe(48))"
```

**提示词（可直接复制）：**

```text
请实现鉴权模块（bcrypt + JWT）：
1. 注册：用户名 ^[A-Za-z0-9_]{4,20}$（不区分大小写，统一转小写存储）；密码 8–64 位且必须同时含
   字母与数字，UTF-8 编码后 ≤72 字节（超出直接拒绝，**不静默截断**）；注册成功即签发 token；
2. 登录：密码错误返回 401 E_CREDENTIALS（用户名不存在与密码错给同一文案，防枚举）；
   连续失败 5 次起锁定 5 分钟，返回 429 + E_AUTH_LOCKED（带剩余秒数）；账号停用返回 403；
3. JWT：单 access token 24h；JWT_SECRET_KEY 必须显式配置，缺失则**启动报错**并打印生成命令；
4. 鉴权依赖：FastAPI Depends 注入当前用户；越权访问他人资源返回 404（不泄露资源是否存在）；
5. 审计：注册/登录/登录失败/锁定/登出各落一条 audit_logs（事件类型走白名单）。
要求：密码哈希直接调 bcrypt（不要用 passlib——该库自 2020 年起停更，与 bcrypt 5.x 不兼容）。
```

**验证方法：**

```powershell
# 1) 注册并自动登录（应返回 token）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/register" -Method POST `
  -ContentType "application/json" -Body '{"username":"stu_demo","password":"abc12345"}'
# 预期输出: 含 data.token

# 2) 无 token 访问受保护接口
try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports" } catch { $_.Exception.Response.StatusCode.value__ }
# 预期输出: 401

# 3) 连续 5 次错误密码 → 锁定
1..5 | ForEach-Object {
  try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
        -ContentType "application/json" -Body '{"username":"stu_demo","password":"wrong123"}' } catch {}
}
# 预期输出: 第 6 次返回 429，data.error_code=E_AUTH_LOCKED 且带剩余秒数
```

**成功标志：**

- 注册成功即拿到 token（无需二次登录）
- 无 token / 非法 token 一律 401；越权访问他人资源 404
- 第 5 次失败后进入锁定（429 + `E_AUTH_LOCKED`），锁定期间即使密码正确也不放行
- `audit_logs` 里能看到注册、登录、失败、锁定四条留痕

### 步骤3：报表读路径（列表 / 详情 / 预览 / 质量 / 字典）

**操作命令：**

```powershell
# 报表数据在两库里：预置 5 张在 reports.db；元数据在 power_agent.db
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/reports.db');print([r[0] for r in c.execute('select name from sqlite_master where type=? and name not like ? order by name',('table','sqlite_%'))])"
# 预期输出: ['report_daily','report_energy_structure','report_hourly','report_minute','report_monthly']
```

**提示词（可直接复制）：**

```text
请实现报表读路径 5 个接口：
1. GET /reports：返回全量列表（不分页），含 table_exists 自检（查 reports.db 的 sqlite_master）、
   is_deletable（预置不可删）、is_active（系统默认活跃报表）；排序 = 预置在前 + 上传按时间倒序；
2. GET /reports/{report_code}/preview：默认 50 行，limit 取值 1–200（越界返回 400 E_VALIDATION，
   不要走框架 422），按时间列正序，**过滤自增主键等代理键**并在 excluded_columns 里回显；
3. GET /reports/{report_code}/quality：返回数据质量报告（dataset 摘要 + checks 逐项），
   口径校验锚点（三级总量、未计量占比等）也在 checks 里可查；
4. GET /reports/{report_code}/schema：返回结构化列清单（structured.columns）与**完整注入文本**
   （text），并标明 text_source（预置取 schema_text.md 原文 / 上传表由 JSON 渲染）；
5. GET /reports/{report_code}：单表详情。
要求：注入文本**上传阶段一次落库、读取时零拼装**（保证"前端字段字典"与"喂给模型的文本"逐字一致）。
```

**验证方法：**

```powershell
$t = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
      -ContentType "application/json" -Body '{"username":"demo_ui","password":"demo12345"}').data.token
$h = @{ Authorization = "Bearer $t" }

# 列表应有 5 条预置
(Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports" -Headers $h).data.Count
# 预期输出: 5

# 预览过滤了代理键 id
(Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports/r_daily/preview?limit=3" -Headers $h).data.excluded_columns
# 预期输出: 含 "id"

# 越界 limit 应返回 400（不是 422）
try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports/r_daily/preview?limit=999" -Headers $h } catch { $_.Exception.Response.StatusCode.value__ }
# 预期输出: 400
```

**成功标志：**

- `/reports` 返回 5 条预置，且每条带 `table_exists=true`
- 预览接口**不返回自增主键**（`excluded_columns` 回显被过滤列），`limit` 越界返回 **400**（包络错误码 `E_VALIDATION`）
- `/schema` 的 `text` 与前端字段字典**逐字一致**（同一个数据来源）

### 步骤4：上传两段式写路径（N4 性能目标 ≤60s / 207 万行）

**操作命令：**

```powershell
# 写入路径基准对比（本项目实测，20 万行 × 16 列）
#   SQLAlchemy + to_dict    19.5µs/行
#   原生 sqlite3 + itertuples 12.7µs/行   → 选后者（ADR-011）
```

**提示词（可直接复制）：**

```text
请实现上传两段式写路径：
1. POST /reports/upload：接收 CSV → 计算 sha256（命中已有文件则直接复用，返回 is_duplicate=true）
   → 登记 pending 记录 → 流式解析（分块 + 指定 dtype）→ 落**规范化中间文件**；
2. POST /reports/{report_code}/confirm：动态建表（表名由文件名规范化 + 白名单正则 + 长度限制）
   → **先灌数后建索引**（分批 executemany 提交）→ 生成 Schema 注入文本落库 → 状态置 ready；
3. 删除：DELETE /reports/{report_code}（预置不可删 → 403）；删除时回显受影响会话数；
4. 性能目标 N4：200 万行 ≤60s；请用**原生 sqlite3 + itertuples + 分批提交**，
   不要逐行走 ORM（基准：19.5µs/行 vs 12.7µs/行）；写连接**独占**（进程内写锁）。
要求：所有写操作只发生在这一条通道里；LLM 生成的代码永远不碰这里。
```

**验证方法：**

```powershell
# 用项目自带数据卷做端到端（真实库 207 万行 / 126.8MB）
# upload 段与 confirm 段各自计时，两段之和应 ≤60s
Measure-Command { Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/reports/upload" -Method POST `
  -Headers $h -Form @{ file = Get-Item "e:\power_report_agent\household_power_consumption.txt" } }
# 预期输出: 本项目实测 upload 32.1s + confirm 17.8s = 49.9s（测试报告 N4 行）

# 重复上传同一文件 → 命中 sha256 复用（不产生第二张物理表）
# 预期输出: is_duplicate=true，耗时约 1.8s
```

**成功标志：**

- 两段式走完（`pending` → `ready`），列表出现新报表且可切换为活跃报表
- 重复上传**命中 sha256 复用**，不产生第二张物理表（`is_duplicate=true`）
- 207 万行端到端 **≤60s**（本项目 49.9s）
- 预置报表删除被拒（403），动态表删除能回收空间并回显受影响会话数

### 步骤5：分析链路（SSE 全留痕）

**操作命令：**

```powershell
# 观察事件序列（也可以用前端工作台；这里是命令行版）
# 事件共 10 个：status / slot / clarify / code / table / chart / narrative / meta / error / done
Select-String -Path app\services\analysis_service.py -Pattern '"event": "' | Select-Object -First 12
```

**提示词（可直接复制）：**

```text
请实现分析链路 POST /analysis/chat（SSE），按阶段推送事件：
status(parsing) → slot → code → status(generating/executing) → table → chart → narrative → meta → done；
任一环节失败推送 error 事件（**HTTP 仍是 200**，业务失败靠事件下发）；
1. slot 阶段：调**轻量模型**做增量抽取（只抽本轮变化），与会话 slot_json 合并（覆盖/继承/替换三类规则）；
   简单问法走**规则快通道**（省一次往返）；模型报"缺指标"但问题里有指标词 → 驳回并词面兜底；
   metric 仍缺失 → 返回 clarify 澄清卡（**澄清轮不落 analyses**，事件为 status→slot→clarify→meta→done）；
2. 生成阶段：调**主力模型**生成 SQL 或 pandas 代码（自报 route）；
3. 守卫：sql_guard（单语句/SELECT/表白名单/强制 LIMIT/R3 混加/Top-N 补 NULL 排除）或 code_guard（AST）；
4. 执行：SQL 走只读连接；pandas 走子进程沙箱；执行报错或形态不对 → 带原因重写一次，再失败换路径；
5. 结果形态自检：问占比却回明细行、问极值却回明细行、筛选型追问丢分组 → 判失败并重写；
6. 留痕：messages ×2、analyses ×1、llm_calls ×N（含 is_retry）、回写 conversations.slot_json。
要求：守卫拦截（越权/危险）**不重试不换路**，直接失败并落 audit_logs。
```

**验证方法：**

```powershell
# 用项目自带的 SSE 探针（能看首字延迟与事件到达时间，退出码 0/1 可进 CI）
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
# 预期输出: 逐事件到达时间；首字 10–16ms 量级

# 库里核对留痕：一次问答应有 messages×2 / analyses×1 / llm_calls×N
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/power_agent.db');print(c.execute('select count(*) from analyses').fetchone()[0])"
```

**成功标志：**

- 命令行/工作台都能看到事件按序到达，`done` 事件携带 `record_id`（澄清轮为 `null`）
- 一次问答在库里留下 messages ×2、analyses ×1、llm_calls ×N（含 `is_retry` 标记）
- 危险请求（`DROP TABLE` / 读系统表）被拦为 `E_SQL_FORBIDDEN`，**不重试、不换路**，且 `audit_logs` 有记录
- 问占比却返回明细行时，重写环被触发（`retries=1`），最终结果形态正确

### 步骤6：图表服务与沙箱

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
..\..\.venv\Scripts\python.exe -m pytest tests/test_chart.py tests/test_sandbox.py -q
# 预期输出: 全部通过（含沙箱超时强杀、禁网禁写、危险 AST 拦截等用例）
```

**提示词（可直接复制）：**

```text
请实现图表服务与 pandas 沙箱：
1. chart_service：按结果集形态选图型（时序→折线、分类对比→柱状、单行多指标→柱状、占比→饼图/堆叠），
   trace 颜色用**语义色**（峰/平/谷/未计量/异常/缺采），TopN 合并、大结果集抽稀、缺采断点；
   **不要写纸面底色与字体色**（主题由前端注入），但要把口径标记写进 layout.meta（刷新回放要用）；
   提供 POST /charts/rerender（换图型/粒度，不调用模型）；
2. sandbox_service：pandas 代码先过 AST 预检（禁非白名单 import、禁 dunder、禁 open/eval/exec/
   subprocess/socket）→ 通过后在**独立子进程**执行（超时 10s 强杀、内存上限 512MB、
   结果行数上限 5000、禁网禁写）；结果约定 result / fig 两个出口。
要求：超时与越权要能被测试真实观测到（不是"软限制"）。
```

**验证方法：**

```powershell
# 图表测试与沙箱测试分开跑，失败时能定位到具体能力
cd e:\power_report_agent\01.code\backend
..\..\.venv\Scripts\python.exe -m pytest tests/test_sandbox.py -q
# 预期输出: 全通过（含"死循环 10 秒被杀""读文件被拦""危险 import 被拦"）

# 离线看图表（不依赖前端，把某条记录渲染成自包含 HTML）
.venv\Scripts\python.exe 01.code\tools\chart_preview.py --help
# 预期输出: 显示用法（--record / --type / --open 等）
```

**成功标志：**

- 图表 trace 颜色来自语义色板，**figure 里不含** `paper_bgcolor` / `font.color` 等主题键
  （唯一例外：导出用的 `EXPORT_LIGHT_LAYOUT` 固定亮色底）
- `POST /charts/rerender` 能换图型/粒度且**不产生模型调用**
- 沙箱对"死循环、读文件、危险 import"三类都真拦截（有测试用例钉住）
- 服务端图片导出为**预留能力**（未装 `kaleido`，`GET .../chart.png` 返回 501；前端 `toImage` 是本版本导出入口）

### 步骤7：多厂商 LLM 适配

**操作命令：**

```powershell
# 中性变量：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_MAIN / LLM_MODEL_LIGHT
Get-Content e:\power_report_agent\01.code\backend\.env.example
```

**提示词（可直接复制）：**

```text
请实现 LLM 客户端（只实现 OpenAI 兼容协议）：
1. 配置全走 .env 中性变量：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_MAIN / LLM_MODEL_LIGHT /
   LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_ENABLE_THINKING；
2. 两级模型：slot 用 LIGHT、生成与解读用 MAIN；
3. 记录每次调用的 model / stage / prompt_tokens / completion_tokens / elapsed / is_retry 落 llm_calls；
4. /system/health 如实回报 configured / provider / protocol（恒为 openai-compatible）/ key_source；
5. 未配置或仍是模板占位值（如 sk-xxxxxx）都视为**未配置**，回落离线脚本模型；
6. 关闭推理型模型的思考过程（LLM_ENABLE_THINKING=false）——本项目的输出都是结构化短文本，
   开思考等于白烧额度（实测 4.0s/195 tokens → 0.9s/5 tokens）。
要求：换厂商只改 .env，不改代码；不要引入 openai SDK（用 httpx 直调即可）。
```

**验证方法：**

```powershell
# 改 .env 换厂商后重启，health 的 provider / key_source 应随之变化
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | ConvertTo-Json -Depth 4
# 预期输出: data.llm.provider=aliyun-maas（或所配厂商）；configured=true；
#          protocol=openai-compatible；key_source 指出 Key 来自哪个变量

# 把 LLM_API_KEY 改回模板占位值 → configured 应变回 false（而不是启动失败）
# 预期输出: configured=false，链路回落离线脚本模型
```

**成功标志：**

- 换厂商只改 `.env`（代码零改动），`health` 显示的 `provider` / `key_source` 同步变化
- 模板占位 Key 被识别为"未配置"（`configured=false`），链路回落离线模型而不是全链路报错
- `llm_calls` 里能看到两级模型都在用（slot 记轻量模型、生成/解读记主力模型），且 token 数可统计

### 步骤8：评测模块（120 题）

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
# 题库脚本化生成（gold 对真实库反算，避免手写答案与库不一致）
..\..\.venv\Scripts\python.exe ..\scripts\04_build_eval_set.py
```

**提示词（可直接复制）：**

```text
请实现评测模块：
1. 题库：120 题，覆盖 6 类题型（stat / agg / compare / anomaly / structure / multi_turn）与
   3 档难度，含口径陷阱题（标注 trap_rule）；多轮题按换行拆轮提问、判分只看最后一轮；
2. 判分三级兜底：① 列位置对齐（行序无关、数值容差 1e-6、比率 ±0.01 个百分点）→
   ② 列名归一后比对 → ③ 单列数值集合宽松比对；全不过才判错并记 diff_type；
3. 指标：总体/分类正确率（题型/难度/陷阱）、结论关键数值命中率、平均与 P95 耗时、
   重试与错误分布、五类 bad case 归因；逐题明细存 eval_runs.result_json；
4. 两种模式：impl=selfcheck（回放 gold，验证链路自洽，**报告置顶标注"不代表模型能力"**）
   与 impl=real（未配 Key 时返回 400 且 hint 指向 selfcheck，**绝不静默回落**）；
5. 报告：Markdown 落 02.data/eval/reports/{run_code}.md，支持前端下载与 PDF 导出；
6. 两种失败要分开记：澄清轮记 clarify_no_result（它没走到执行），跑批异常记 runner_error。
要求：temperature 固定 0，保证同题可复现；报告里的数字必须与 result_json 同源。
```

**验证方法：**

```powershell
# 自检批次（零 token，正常应≈100%，任何偏差都说明链路坏了）
$t = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
      -ContentType "application/json" -Body '{"username":"demo_ui","password":"demo12345"}').data.token
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/eval/runs" -Method POST `
  -Headers @{ Authorization = "Bearer $t" } -ContentType "application/json" `
  -Body '{"impl":"selfcheck","limit":3,"name":"自检3题"}'
# 预期输出: 返回 run_code；轮询 GET /eval/runs/{run_code} 直到 status=finished

# 真实模型但未配置 Key → 必须 400 且 hint 指向 selfcheck（不静默回落）
# 预期输出: 400，data.hint 提示改用 selfcheck

# 报告文件已落盘
Get-ChildItem e:\power_report_agent\02.data\eval\reports\*.md | Select-Object -Last 3 Name, Length
```

**成功标志：**

- 自检批次（`selfcheck`）跑出的正确率**≈100%**（这是链路自洽的判据，不是模型能力）
- `impl=real` 未配 Key 时**返回 400 并给 hint**，不静默回落
- 报告 Markdown 落盘且指标与 `result_json` 一致，含逐题明细与错题复盘
- 澄清轮与跑批异常在报告里**分别归类**（`clarify_no_result` / `runner_error`），不混成"结果为空"

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 服务启动 | `Invoke-RestMethod .../api/v1/system/health` | `status=healthy` |
| 只读库拒绝写入 | 用 sqlite3 对 `reports.db` 执行 INSERT | 报 `readonly database` |
| 单元测试 | `..\..\.venv\Scripts\python.exe -m pytest -q` | **323 passed** |
| 鉴权 | 无 token 调 `GET /reports` | 401 + `E_AUTH` |
| 登录锁定 | 连续 5 次错误密码 | 429 + `E_AUTH_LOCKED` |
| 上传性能 | 207 万行 CSV 走两段式 | 总耗时 ≤60s（本项目 49.9s） |
| 重复上传 | 同一文件再传一次 | `is_duplicate=true`，约 1.8s |
| SSE 事件序列 | `sse_probe.py` 或工作台提问 | **10 个事件**顺序正确 |
| 守卫拦截 | 提问 `DROP TABLE` | 拦截 + `E_SQL_FORBIDDEN` + 审计，且不重试 |
| 形态自检 | 提问占比题且模型返回明细行 | 触发重写（`retries≥1`），最终形态正确 |
| 沙箱 | `pytest tests/test_sandbox.py` | 全通过（含超时强杀、禁网禁写） |
| 多厂商 | 改 `.env` 换厂商并重启 | `health` 显示新 provider |
| 评测 | `selfcheck` 跑 3 题 | 指标卡与报告生成，正确率≈100% |

## 五、故障排除

### 问题1：模型输出不是合法 JSON

**现象：** slot 或生成阶段报解析失败，链路中断（或返回空指标）。

**原因：** 模型偶尔会带解释文字、代码块围栏、尾随逗号，直接 `json.loads` 会失败。

**解决方案：** 用**三层解析**：`json.loads` → `json_repair`（容错修复）→ 代码块正则兜底抽取；
同时在提示词里明确"只输出 JSON、不要解释"。

```powershell
# 看解析相关实现与用例
Select-String -Path e:\power_report_agent\01.code\backend\app\services\analysis_service.py -Pattern "_safe_json"
# 预期输出: 命中三层解析的实现
```

### 问题2：SQL 守卫误拦（CTE 名被当成表名）

**现象：** 合法查询被拦，提示"表不在白名单"，但 `WITH xxx AS (...)` 里的 `xxx` 只是 CTE 别名。

**原因：** 白名单校验只看 `sqlglot` 解析出的表名，未排除 CTE 定义的名字。

**解决方案：** 先收集语句里的 **CTE 别名**，再做表白名单判定（本项目踩坑记录，已有用例钉住）。

```powershell
cd e:\power_report_agent\01.code\backend
..\..\.venv\Scripts\python.exe -m pytest tests/test_security.py -q -k cte
# 预期输出: 通过（含 CTE 不误拦用例）
```

### 问题3：数据库被锁（database is locked）

**现象：** 上传入库期间，其它请求报 `database is locked`。

**原因：** 长事务/大量小事务持有写锁过久（SQLite 单写者模型）。

**解决方案：** 写路径用**独占写连接 + 分批提交**缩短持锁时间；读路径用只读连接（`mode=ro`）
不参与写锁竞争；批量灌数**先灌数后建索引**。

```powershell
# 确认写连接是独占且分批（看服务实现）
Select-String -Path e:\power_report_agent\01.code\backend\app\services\report_write_service.py -Pattern "executemany|commit"
# 预期输出: 命中分批提交逻辑
```

### 问题4：Windows 下路径或编码出错

**现象：** 换目录启动服务后连不上库；或控制台中文乱码、日志写入报编码错。

**原因：** 库路径若按"启动时的工作目录"解析就会随目录漂移；Windows 控制台默认 GBK。

**解决方案：** 库路径**一律按项目根解析**（配置层统一处理）；启动前设置
`$env:PYTHONIOENCODING = "utf-8"`。

```powershell
$env:PYTHONIOENCODING = "utf-8"
# 从任意目录启动都能连到库（路径基准是项目根，不是 cwd）
cd e:\power_report_agent
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'01.code/backend'); from app.core.config import get_settings; print(get_settings().power_agent_db)"
# 预期输出: 指向 02.data/power_agent.db 的绝对路径
```

### 问题5：真实评测返回 400

**现象：** `POST /eval/runs` 传 `impl=real` 返回 400。

**原因：** 未配置 `LLM_API_KEY`（或仍是模板占位值）。这是**刻意设计**：真实评测不静默回落，
免得"以为是模型成绩、其实是回放 gold"。

**解决方案：** 先按 hint 改用 `impl=selfcheck` 验证链路；要真实模型就在 `.env` 填好 Key 并重启。

```powershell
# 检查 Key 是否真的配了（不回显 Key 本体）
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | Select-Object -ExpandProperty data |
  Select-Object -ExpandProperty llm
# 预期输出: configured=true；key_source 指出 Key 来自哪个变量
```

### 问题6：自检数字被当成"模型能力"

**现象：** 报告里的 100% 被引用为"系统效果"，与真实模型成绩混在一起。

**原因：** 自检模式用的是回放 gold 的脚本模型，它**不代表任何模型能力**。

**解决方案：** 引用前先看报告的**执行模式**字段；自检批次的报告首段与前端指标卡都置顶标注
"不代表模型能力"，引用时必须一并说明（真实成绩见测试报告按模型/提示词版本分列）。

```powershell
# 报告首段会写明执行模式
Get-Content e:\power_report_agent\02.data\eval\reports\run_20260920_01.md -Encoding UTF8 -TotalCount 6
# 预期输出: 含"执行模式：真实模型"或"自检（不代表模型能力）"
```

## 六、参考代码

### 完整目录结构

```
01.code/backend/
├── app/
│   ├── main.py
│   ├── api/v1/
│   │   ├── router.py / deps.py
│   │   └── endpoints/{auth,reports,conversations,analysis,charts,eval,system}.py   # 39 条端点
│   ├── core/
│   │   ├── config.py / errors.py / security.py / locks.py
│   │   ├── llm_client.py            # 多厂商 OpenAI 兼容 + ScriptedLLM（离线）
│   │   ├── sql_guard.py             # sqlglot 只读白名单 + R3 混加 + Top-N NULL 排除
│   │   └── code_guard.py            # pandas 代码 AST 预检
│   ├── agent/prompts.py             # SLOT_SYSTEM / SQL_SYSTEM / PANDAS_SYSTEM + 版本号
│   ├── db/
│   │   ├── session.py               # 三连接工厂（读写 / 只读 / 独占写）
│   │   └── models/                  # 10 个模型文件（含 base.py），只做查询映射
│   ├── schemas/                     # 统一包络 + 各组请求/响应模型
│   └── services/                    # 14 个服务（见 二.2）
├── requirements.txt / requirements-dev.txt / .env.example
└── tests/                           # 16 个测试文件 + conftest.py（共 323 条用例）
```

### 常用命令

```powershell
cd e:\power_report_agent\01.code\backend
$env:PYTHONIOENCODING = "utf-8"

# 启动后端（0.0.0.0 便于局域网访问）
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 单元测试（20 个测试文件 / 323 条用例）
..\..\.venv\Scripts\python.exe -m pytest -q

# 单独跑某条线（改代码后最快定位）
..\..\.venv\Scripts\python.exe -m pytest tests/test_sandbox.py -q
..\..\.venv\Scripts\python.exe -m pytest tests/test_security.py -q

# 重建数据库（DDL 事实源，幂等）
..\..\.venv\Scripts\python.exe ..\scripts\03_init_db.py --force

# 生成题库（gold 对真实库反算）
..\..\.venv\Scripts\python.exe ..\scripts\04_build_eval_set.py

# SSE 探针（首字延迟与事件到达时间）
..\..\.venv\Scripts\python.exe ..\tools\sse_probe.py

# 评测批次（命令行发起并打印摘要）
..\..\.venv\Scripts\python.exe ..\tools\eval_run.py --impl selfcheck --limit 3
```

### 关键配置说明

| 参数 | 说明 |
|--------|------|
| 响应包络 | `{code, message, data, request_id}`；`code` 与 HTTP 同值；业务码在 `data.error_code`（`E_*`），修复建议在 `data.hint` |
| 三连接 | 控制面读写（WAL）/ 数据面只读（`mode=ro` + `query_only` + NullPool）/ 报表库独占写 |
| 守卫 | `sql_guard`（语法树：单语句、仅 SELECT、表白名单、强制 LIMIT、R3 混加、Top-N 补 NULL 排除）；`code_guard`（AST 预检）；拦截落 `audit_logs` 且**不重试** |
| 沙箱 | 独立子进程：10s 超时强杀 / 内存 512MB / 结果 ≤5000 行 / 禁网禁写；`RestrictedPython` 已装**未接入** |
| 编排 | **LangGraph 状态图**（`app/agent/graph.py`：13 节点 + 条件边；重写环 / 形态自检环 / 澄清 HITL；会话级检查点 `checkpoints` / `writes`；`ORCHESTRATOR=langgraph` 默认、`legacy` 一行回退，见架构 **ADR-021**） |
| 模型分级 | slot → 轻量模型；生成 / 解读 → 主力模型；`LLM_ENABLE_THINKING=false` 关思考省额度 |
| 调度兜底 | 规则快通道（简单问法免 slot 往返）、指标词面兜底、结果形态自检（三类） |
| 错误码（守卫） | `E_SQL_FORBIDDEN`（SQL 未过白名单）/ `E_CODE_FORBIDDEN`（pandas 代码未过 AST 预检） |
| SSE 事件 | 10 个：`status` `slot` `clarify` `code` `table` `chart` `narrative` `meta` `error` `done` |
| 上传性能 | 207 万行 × 9 列 / 126.8MB：upload 32.1s + confirm 17.8s = **49.9s**（N4 目标 ≤60s） |
| 写入路径 | 原生 sqlite3 + itertuples + 分批提交（19.5µs/行 → **12.7µs/行**，ADR-011） |
| 评测 | 120 题 / 6 类题型；判分三级兜底；`selfcheck`（回放，标注不代表模型能力）与 `real`（无 Key 返 400，不回落） |
