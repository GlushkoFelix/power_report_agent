# 实训6 基于AI共创的智电析报执行测试与效果评测

> **本章的实测数字与《智电析报_测试报告》v1.0.9 同源**——需要完整证据（测量方法、缺陷记录、遗留项）时
> 直接引该报告，本章只保留"怎么做"的操作口径。数字随迭代更新，**以测试报告为准**。

## 一、实训目标

1. 掌握单元测试与端到端回归的分工：pytest 保证后端行为，无头浏览器脚本保证界面行为
2. 能够执行数据口径校验（A1–A5）并解释每一项的业务含义
3. 掌握前端联调自检方法（**32 条断言** + **8 张页面截图**），并把用户反馈缺陷固化为断言
4. 能够运行 120 题评测集，读懂执行正确率、结论命中率、判分档分布与 bad case 归因
5. 学会如实报告结果：区分"链路自检"与"真实模型评测"，不把自检数字当成绩；
   **跨批次拼合的数字必须标注"非单轮单配置"**

## 二、实训内容

### 1. 背景知识

- **测试分层（四层，各管一段）**：
  ① **单元测试**（pytest，**365 条用例 / 25 个文件**）：后端行为与安全边界；
  ② **契约冒烟**（`Invoke-RestMethod` 或 curl）：接口包络与错误码；
  ③ **端到端回归**（无头 Edge + CDP，**32 条断言**）：界面行为与用户反馈缺陷；
  ④ **效果评测**（120 题）：模型能力与 bad case 归因。
  四层不互相替代：单元测试全绿不等于界面对，界面全对不等于模型答得准。
- **口径校验 A1–A5（数据正确性的地基）**：
  | 锚点 | 校验内容 | 交付基线 |
  | --- | --- | --- |
  | A1 | 日报 / 月报 / 结构表**三级总量一致** | 三处均 **37283.7477 kWh**，偏差 **0.00e+00** |
  | A2 | 未计量占比（结构表全期行） | **0.511977（51.1977%）** |
  | A3 | 有效分钟占比 | **2,049,280 / 2,075,259 = 98.7486%**（质量报告记 98.7482%，四舍五入口径差异） |
  | A4 | 异常日 | **81 天 / 1442 天**（越界口径：210–250V 且当日累计 ≥30 分钟） |
  | A5 | 5 张报表行数 | 分钟 **2,075,259**｜小时 34,589｜日报 1,442｜月报 48｜结构 6 |
  **口径错了后面全错**，所以 A1–A5 排在所有测试的最前面；校验**内建在数据脚本里**
  （`01_build_reports.py` 以未舍入值算、`03_init_db.py` 在库上重跑），A1/A2 不通过脚本直接返回非 0。
- **评测判分三档 + 容差口径**：
  ① 列位置一致（列数相同则按位置比对，**行序无关**）→ ② 列名归一一致 →
  ③ 单列日期/数值宽松匹配（`'2006-12'` ≈ `12` 这类年月形态等价也算对）。
  数值容差 `max(绝对下限, |值| × 1e-3, 展示级舍入 5e-3)`，其中普通列绝对下限 1e-6、
  比率列 1e-4（百分比 ±0.01 个百分点）。
  **放宽的是"展示级舍入"，不是口径错误**——比率用错分子分母仍判错（R6 红线就是这么抓出来的）。
- **自检 vs 真实**：`selfcheck` 逐题回放题库 `gold_sql`（正常应≈100%，只证明
  "题库 → 链路 → 判分 → 报告"自洽，**不代表任何模型能力**）；`real` 走真实模型，
  无 Key 时返回 400 且 hint 指向 selfcheck（**绝不静默回落**）。
- **为什么"如实报告"是本实训的重点**：一次真实评测会同时出现
  "单批成绩"、"跨批合并成绩"、"逐题累计成绩"三种口径，**混着引用就会把结论说过头**。
  本项目在测试报告里对每种口径都写了适用边界（见 7.4 的如实说明），本节把它作为操作规范传承下去。

### 2. 项目结构

```
01.code/
├── backend/tests/                     # 25 个文件（24 个 test_*.py + conftest.py）/ 365 条用例
└── tools/
    ├── ui_flow_check.mjs              # 端到端回归（32 条断言，无头 Edge + CDP）
    ├── ui_shot.mjs                    # 8 张页面截图（落 02.data/tmp/ui_shots）
    ├── sse_probe.py                   # 首字延迟（真 SSE，能识别"攒批回放"）
    ├── latency_report.py              # 按批次拆解各阶段耗时占比
    ├── eval_run.py                    # 发起评测批次并打印摘要
    ├── eval_merge.py                  # 多批次合并（同题去重取最后一批）
    └── eval_gold_audit.py             # 题库 gold 审计（NULL 陷阱与不可执行项）
02.data/
├── quality/                           # 数据质量报告（含 A1–A5 结论）
└── eval/
    ├── eval_questions.json            # 题库快照（120 题）
    └── reports/run_*.md               # 评测批次报告（含逐题明细与归因）
```

## 三、实训步骤

### 步骤1：后端单元测试

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
$env:PYTHONIOENCODING = "utf-8"
..\..\.venv\Scripts\python.exe -m pytest -q
```

**提示词（可直接复制给 AI）：**

```text
请为后端补齐单元测试（pytest + httpx AsyncClient），要求：
1. 用**临时库**（按真实 DDL 建）而不是正式库，避免污染；
2. 覆盖八条线：鉴权与隔离 / 报表读路径 / 上传两段式写路径 / 会话与消息 /
   分析链路与留痕 / 沙箱与安全 / 图表 / 评测判分；
3. 安全边界要"可观测"：越权 SQL 被拦、危险 AST 被拦、沙箱超时被真杀掉（不是软限制）；
4. 每条被修掉的缺陷都补一条回归用例（防复发），用例名写清"防的是哪个缺陷"。
要求：不依赖真实模型（用离线脚本模型 ScriptedLLM），跑得快、可反复执行。
```

**验证方法：**

```powershell
cd e:\power_report_agent\01.code\backend
..\..\.venv\Scripts\python.exe -m pytest -q 2>&1 | Select-Object -Last 3
# 预期输出: 303 passed

# 单独跑某条线（改完代码先跑它，最快定位）
..\..\.venv\Scripts\python.exe -m pytest tests/test_sandbox.py -q
```

**成功标志：**

- 全量输出 **303 passed**，零失败零警告
- 测试跑在**临时库**上（跑完正式库的记录数不变）
- 安全与沙箱用例里能看到"拦截/超时"是**被真实触发**的（不是 mock 出来的）

### 步骤2：数据口径校验（A1–A5）

**操作命令：**

```powershell
cd e:\power_report_agent
$env:PYTHONIOENCODING = "utf-8"
# ① 质量报告里的 A1–A5 结论（数据脚本产出）
Get-Content 02.data\quality\quality_report.json -Encoding UTF8 | ConvertFrom-Json | Select-Object -ExpandProperty checks
# ② 三级总量在库上复核（A1：日报 / 月报 / 结构表应完全一致）
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/reports.db');print('daily', c.execute('select round(sum(consumption_kwh),4) from report_daily').fetchone()[0]);print('monthly', c.execute('select round(sum(consumption_kwh),4) from report_monthly').fetchone()[0]);print('structure', c.execute('select round(total_kwh,4) from report_energy_structure where is_total_row=1').fetchone()[0])"
```

**提示词（可直接复制）：**

```text
请实现并解释数据口径校验 A1–A5（内建在数据脚本里，不靠人工看）：
A1 三级总量一致（日报 / 月报 / 结构表全期行）；A2 未计量占比；
A3 有效分钟占比；A4 异常日（3σ 滚动窗口 + 电压越界口径）；A5 各表行数。
要求：
1. 用**未舍入**的聚合值做校验（先舍入再比较会掩盖偏差）；
2. 校验结果落 quality_report.json，并在建库脚本里**在库上重跑一遍**（校验的是真正被分析的数据）；
3. A1 / A2 不通过时脚本返回非 0 退出码（可直接当门禁）；
4. 每一项都要写清"业务含义"与"对不上的排查方向"。
```

**验证方法：**

```powershell
# 三级总量应完全一致（三个数字相同）
# 预期输出: daily 37283.7477 / monthly 37283.7477 / structure 37283.7477

# 行数锚点（A5）
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('02.data/reports.db');[print(n, c.execute('select count(*) from ' + n).fetchone()[0]) for n in ('report_minute','report_hourly','report_daily','report_monthly','report_energy_structure')]"
# 预期输出: 2075259 / 34589 / 1442 / 48 / 6

# A1/A2 不通过时脚本应返回非 0（可当 CI 门禁）
.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force
"退出码: $LASTEXITCODE"
# 预期输出: 0（若数据被改坏则为非 0）
```

**成功标志：**

- 三级总量三处**完全一致**（偏差 0），A5 行数与质量报告一致
- A1–A5 的结论**同时**能在 `quality_report.json` 与接口 `GET /reports/{code}/quality` 里查到（同一份事实）
- 人为改坏数据后，建库脚本**返回非 0**（门禁有效）

### 步骤3：前端联调回归与截图

**操作命令：**

```powershell
cd e:\power_report_agent
# 需前后端都在跑；两个脚本会改同一账号数据，必须串行（脚本内已用锁文件强制）
node 01.code\tools\ui_flow_check.mjs    # 32 条断言
node 01.code\tools\ui_shot.mjs          # 8 张截图 → 02.data/tmp/ui_shots
```

**提示词（可直接复制）：**

```text
请把我们反馈过的每个界面缺陷固化成可重跑的断言（tools/ui_flow_check.mjs，无头 Edge + CDP）：
1. 断言要**与运行顺序无关**：脚本开头先做状态归一（走接口清理、不调模型、零 token）；
2. 覆盖：空态上传入口、连点新建会话不产生重复会话、删除会话后主区同步、
   字段字典随报表切换更新、退出登录后重新登录回首页、个人中心四个弹窗、导航与两栏对齐；
3. 失败时要打印现场（视口尺寸、命中的文本、页面尾部 HTML 片段），便于一次定位；
4. 与 ui_shot.mjs 用**锁文件互斥**：两者都改同一账号的数据，并行会互相打乱计数断言，
   第二个启动者直接退出并提示，而不是给出一堆假失败。
```

**验证方法：**

```powershell
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs
# 预期输出: 32 条 PASS，末尾"全部通过"

node 01.code\tools\ui_shot.mjs
Get-ChildItem 02.data\tmp\ui_shots\*.png | Select-Object Name
# 预期输出: 8 张（login / workbench-empty / workbench-result / workbench-dark /
#          reports / eval / eval-metrics / profile）

# 并行启动第二个脚本 → 应直接退出并提示串行
# 预期输出: "检测到 ui_flow_check.mjs 正在运行……本次直接退出"
```

**成功标志：**

- **32 条断言全部 PASS**，且重复运行结果稳定（顺序无关）
- 截图目录 **8 张**，暗色那张的图表底色为深色
- 两脚本并行时第二个**如实退出**（退出码 2），不是假失败

### 步骤4：评测自检（先验证链路，再花 token）

**操作命令：**

```powershell
cd e:\power_report_agent
$token = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
          -ContentType "application/json" -Body '{"username":"demo_ui","password":"demo12345"}').data.token

# 自检 3 题（零 token）
$run = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/eval/runs" -Method POST `
        -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
        -Body '{"impl":"selfcheck","limit":3,"name":"自检3题"}').data.run_code
$run

# 轮询到结束
do {
  Start-Sleep -Seconds 2
  $s = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/eval/runs/$run" -Headers @{ Authorization = "Bearer $token" }).data.status
  "status=$s"
} while ($s -eq "running" -or $s -eq "pending")
```

**提示词（可直接复制）：**

```text
请实现评测的自检模式（impl=selfcheck）：
1. 逐题回放题库 gold_sql（用离线脚本模型），验证"题库 → 分析链路 → 判分 → 指标 → 报告"全通；
2. 正常结果应≈100%，**任何偏差都说明链路坏了**（而不是"模型不好"）；
3. 自检批次的报告与前端指标卡**置顶标注"不代表模型能力"**；
4. 与 real 模式共用同一套判分与指标代码（避免两套口径）。
要求：自检不消耗任何模型额度。
```

**验证方法：**

```powershell
# 批次结束后看报告（首段应含执行模式）
Get-Content 02.data\eval\reports\<run_code>.md -Encoding UTF8 -TotalCount 8
# 预期输出: 执行模式：自检（不代表模型能力）；正确率≈100%

# 指标与报告同源（result_json 与报告数字一致）
.venv\Scripts\python.exe -c "import sqlite3,json,sys;sys.path.insert(0,'01.code/backend');from app.core.config import get_settings;c=sqlite3.connect('file:'+get_settings().power_agent_db+'?mode=ro',uri=True);print(c.execute('select run_code, exec_correct_rate, question_total from eval_runs order by id desc limit 3').fetchall())"
# 预期输出: 最近三个批次的正确率与题量
```

**成功标志：**

- 自检批次 `status=finished`，正确率 **≈100%**
- 报告首段标注"自检（不代表模型能力）"，指标卡同样标注
- 自检全程**零模型额度消耗**（`llm_calls` 里记的是脚本模型）

### 步骤5：真实模型评测（配置 Key 后）

**操作命令：**

```powershell
cd e:\power_report_agent
# 1) 确认已配置真实模型
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | Select-Object -ExpandProperty data |
  Select-Object -ExpandProperty llm
# 2) 小样本确认链路与耗时（20–30 题）
.venv\Scripts\python.exe 01.code\tools\eval_run.py --impl real --user demo_ui --limit 20 --name "小样本20题" --timeout 1800
```

**提示词（可直接复制）：**

```text
请用真实模型跑评测，并按"先小样本、后全量"的顺序推进：
1. 先 20–30 题确认链路、耗时与 token 量级（顺便校准"单条记录成本"）；
2. 跑全量前**先算额度**：单条记录成本 × 135（120 题 + 15 道多轮的第二轮），
   装不下就先分段或换模型，**不要跑到一半爆额度**（那会把整批变成脏数据）；
3. 批次报告要能回答四个问题：执行正确率、结论命中率、延迟、错题归因；
4. 如实记录口径：模型名、提示词版本、温度（固定 0）、批次是否合并、是否同一批跑出。
要求：自检与真实成绩在报告里必须能一眼区分。
```

**验证方法：**

```powershell
cd e:\power_report_agent
# 单条记录成本（用于额度外推；模型调用数 ÷ 记录数也应≈3）
.venv\Scripts\python.exe -c "import sqlite3,sys;sys.path.insert(0,'01.code/backend');from app.core.config import get_settings;c=sqlite3.connect('file:'+get_settings().power_agent_db+'?mode=ro',uri=True);print(c.execute('select model_name, count(*), sum(total_tokens) from llm_calls group by 1 order by 3 desc limit 5').fetchall())"
# 预期输出: 各模型累计调用次数与 token 总量

# 额度外推：单条记录成本 × 135
# 预期输出: 与实测批次报告的 token 消耗同量级（±10%）
```

**成功标志：**

- 小样本批次能跑通且耗时/token 与预期同量级（据此外推全量额度）
- 全量批次的报告含四要素：正确率、命中率、延迟、归因
- 报告头部如实写出**模型名 / 提示词版本 / 温度 / 是否合并批次**（缺一项就不算合格报告）

### 步骤6：性能实测与报告

**操作命令：**

```powershell
cd e:\power_report_agent
# 首字延迟（真 SSE 探针；评测客户端是单次 POST，采不到首字）
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
# 按批次拆解各阶段耗时占比
.venv\Scripts\python.exe 01.code\tools\latency_report.py
```

**提示词（可直接复制）：**

```text
请采集并记录性能硬指标（每项都必须写"测量方法"，否则无法复核）：
N1 首字延迟（SSE 首个事件到达）；N2 单轮平均耗时（区分"简单问数"与"含多轮"口径）；
N3 P95 耗时；N4 上传解析入库（200 万行 ≤60s）；N5 列表/切换；N6 并发；N7 图表渲染。
要求：测不准就写"未测量"，**不要用推算值冒充实测值**；未达标就如实写未达标（含影响与下一步）。
```

**验证方法：**

```powershell
cd e:\power_report_agent
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
"退出码: $LASTEXITCODE"
# 预期输出: 各事件到达时间；首字 10–16ms 量级；退出码 0（可进 CI）

.venv\Scripts\python.exe 01.code\tools\latency_report.py
# 预期输出: 按阶段的耗时占比与 G2 判定（与测试报告 N2/N3 行一致）
```

**成功标志：**

- 首字延迟有**真实 SSE 测量**（不是用单次 POST 的耗时冒充）
- 测试报告的 N1–N7 每行都写了测量方法与口径（含"未测量"的如实登记）
- 未达标项（本项目为 N3 P95）在报告里有专门条目，写清影响与下一步

### 步骤7：bad case 归因与迭代

**操作命令：**

```powershell
cd e:\power_report_agent
# 看最近批次报告里的归因表与错题复盘
Get-ChildItem 02.data\eval\reports\*.md | Sort-Object LastWriteTime -Descending | Select-Object -First 1 Name
```

**提示词（可直接复制）：**

```text
请对最近一批的错题做归因与迭代：
1. 按五类归因统计（口径理解错误 / SQL 语法与函数 / slot 继承错误 / 执行失败未重试成功 /
   结果口径偏差），并给出每类的"下一步动作"；
2. 对每题给出：题号、现象、根因（要读生成的 SQL 与 slot，不要只看结论对错）、
   修法（改提示词 / 改 Schema / 改代码守卫）、验证方式；
3. **能机检的错误优先用代码保证**：如果某类错误"规则早已明文但模型不照做"，
   就不要再加提示词文字，改成生成后的确定性校验（本项目做法：结果形态自检 + 守卫补 NULL）；
4. 修完必须回归：跑同一批或同题子集，用数字说明改好了（不要只说"应该修好了"）。
要求：区分"模型答错"与"链路失败"，两者混在一起会让迭代方向跑偏。
```

**验证方法：**

```powershell
# 归因表与错题复盘在报告里
Select-String -Path 02.data\eval\reports\run_20260919_24.md -Pattern "归因|错题复盘" -Context 0,3
# 预期输出: 命中归因分类与逐题复盘

# 修完回归（同题子集，零成本自检或小样本）
.venv\Scripts\python.exe 01.code\tools\eval_run.py --impl real --user demo_ui --codes q096 --name "q096回归" --timeout 300
# 预期输出: 1/1 = 100.0%（本项目 q096 修复后的回归结果）
```

**成功标志：**

- 每道错题都有"现象 / 根因（读 SQL 与 slot 得出）/ 修法 / 验证"四要素
- "规则已明文但不照做"的错误改由**代码保证**（而不是继续加提示词）
- 修完有回归数字（本项目示例：q096 修复后 1/1 且少一次模型调用）

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 单元测试 | `..\..\.venv\Scripts\python.exe -m pytest -q` | **303 passed** |
| 口径校验 A1 | 三级总量查询 | 三处均 37283.7477 kWh（偏差 0） |
| 口径校验 A5 | 5 张报表行数查询 | 2075259 / 34589 / 1442 / 48 / 6 |
| 门禁有效 | 改坏数据后跑 `03_init_db.py` | 退出码非 0 |
| 回归断言 | `node 01.code\tools\ui_flow_check.mjs` | **32 条全部 PASS** |
| 页面截图 | `node 01.code\tools\ui_shot.mjs` | **8 张 PNG** |
| 评测自检 | `POST /eval/runs`（selfcheck） | 正确率≈100%，报告标注"不代表模型能力" |
| 真实评测 | `.env` 配 Key 后 `impl=real` | 报告含正确率/命中率/延迟/归因 |
| 额度预估 | 单条记录成本 × 135 | 与实测批次同量级（±10%） |
| 首字延迟 | `.venv\Scripts\python.exe 01.code\tools\sse_probe.py` | 首字 10–16ms；退出码 0 |
| 性能对照 | `latency_report.py` + 测试报告 | N1/N2/N4 达标；N3 未达标**如实登记** |
| 归因与迭代 | 报告"归因分类"表 | 五类归因齐备，错题有四要素 |

## 五、故障排除

### 问题1：测试污染正式库

**现象：** 跑完测试后，演示库多了莫名其妙的会话/记录，或者上传的报表还在。

**原因：** 测试用例连了正式库；或端到端脚本（上传/删除类）直接操作正式数据。

**解决方案：** 单元测试一律用**临时库**（按真实 DDL 建）；必须动正式库的 E2E 先备份再还原，
并在还原后复核口径。

```powershell
cd e:\power_report_agent\01.code\backend
..\..\.venv\Scripts\python.exe -m pytest -q
# 跑完后正式库记录数应不变：
..\..\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('../../02.data/power_agent.db');print(c.execute('select count(*) from analyses').fetchone()[0])"
```

### 问题2：两个 UI 脚本并行跑导致断言失败

**现象：** 单独跑都过，一起跑就出现"断言找不到元素""会话数不对"。

**原因：** 两个脚本都往同一账号（`demo_ui`）里建/删会话、发提问，
计数类断言被对方的动作打乱（不是代码坏了）。

**解决方案：** 用锁文件（`02.data/tmp/_ui_tools.lock`）强制串行；第二个启动者直接退出并提示。

```powershell
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs
# 另开窗口立刻再跑一次 → 应输出"检测到 … 正在运行……本次直接退出"，退出码 2
```

### 问题3：断言偶发失败（"执行信息"没出现）

**现象：** `ui_flow_check.mjs` 偶尔报某个元素不存在，重跑又好了。

**原因：** SSE 收尾才渲染"执行信息"面板，固定 `sleep` 等不够稳。

**解决方案：** 改成**轮询等待**（条件满足即继续），而不是固定等待；脚本已如此实现。

```powershell
Select-String -Path e:\power_report_agent\01.code\tools\ui_flow_check.mjs -Pattern "waitFor|sleep\(" | Select-Object -First 6
# 预期输出: 命中轮询等待函数
```

### 问题4：真实评测返回 400

**现象：** `impl=real` 直接 400，提示未配置模型。

**原因：** `.env` 里 `LLM_API_KEY` 未配置（或还是 `sk-xxxxxx` 模板值）。这是刻意设计：
**真实评测不静默回落**，否则你拿到的"成绩"其实是回放 gold。

**解决方案：** 先用 `impl=selfcheck` 验证链路；要真实成绩就填 Key 并重启后端。

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | Select-Object -ExpandProperty data |
  Select-Object -ExpandProperty llm
# 预期输出: configured=true（含 provider / key_source）
```

### 问题5：报告里首字延迟恒为"—"

**现象：** 评测报告"首字延迟"一栏永远没有数字。

**原因：** 评测客户端是**单次 POST（非流式）**，拿不到首字时间——`first_token_avg_ms` 恒 NULL。
这是**如实记录**，不是漏采。

**解决方案：** 首字延迟用**真实 SSE 探针**单独测（`sse_probe.py`），并在报告里注明两种测量口径的区别。

```powershell
cd e:\power_report_agent
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
# 预期输出: 真正 SSE 的首字延迟（10–16ms 量级）+ 是否"攒批回放"
```

### 问题6：同一问题在不同批次数字对不上

**现象：** 同一个问题，两批跑出的耗时/正确率不同。

**原因：** ① 模型或提示词版本不同；② 温度不是 0；③ 题量/题型构成不同
（含多轮题的批次天然更慢）；④ 口径版本（`caliber_version`）不同。

**解决方案：** 比较前先对齐四件事：**模型名 / 提示词版本 / 温度 / 批次口径**；
报告头部都会写明，逐项核对再下结论。

```powershell
cd e:\power_report_agent
Get-Content 02.data\eval\reports\run_20260919_22.md -Encoding UTF8 -TotalCount 8
Get-Content 02.data\eval\reports\run_20260919_23.md -Encoding UTF8 -TotalCount 8
# 预期输出: 两份报告头部写明模型 / 提示词版本 / 题量，可比性一眼可判
```

## 六、参考代码

### 完整目录结构

```
01.code/
├── backend/tests/                   # 25 个文件（24 个 test_*.py + conftest.py）/ 365 条用例
│   ├── test_auth.py / test_reports.py / test_report_detail.py / test_report_write.py
│   ├── test_conversations.py / test_analysis.py / test_analysis_records.py / test_clarify_slot.py
│   ├── test_sandbox.py / test_security.py / test_chart.py / test_eval.py
│   ├── test_schema.py / test_llm_provider.py / test_profile.py / test_prompt_iteration.py
│   └── conftest.py                  # 临时库 + AsyncClient + 离线模型
└── tools/
    ├── ui_flow_check.mjs            # 32 条界面断言（无头 Edge + CDP，含互斥锁）
    ├── ui_shot.mjs                  # 8 张截图
    ├── sse_probe.py                 # 真 SSE 首字测量
    ├── latency_report.py            # 阶段耗时拆解
    ├── eval_run.py / eval_merge.py  # 评测发起 / 多批合并（同题去重取最后一批）
    └── eval_gold_audit.py           # 题库 gold 审计（NULL 陷阱与不可执行项）
02.data/
├── quality/quality_report.json      # A1–A5 与数据质量结论
└── eval/{eval_questions.json, reports/run_*.md}
```

### 常用命令

```powershell
cd e:\power_report_agent
$env:PYTHONIOENCODING = "utf-8"

# 后端全量测试
cd 01.code\backend; ..\..\.venv\Scripts\python.exe -m pytest -q; cd ..\..

# 前端端到端回归 + 截图（需前后端在跑；两脚本必须串行）
node 01.code\tools\ui_flow_check.mjs
node 01.code\tools\ui_shot.mjs

# 评测：自检（零 token）/ 小样本 / 指定题
.venv\Scripts\python.exe 01.code\tools\eval_run.py --impl selfcheck --limit 3
.venv\Scripts\python.exe 01.code\tools\eval_run.py --impl real --user demo_ui --limit 20 --name "小样本20题"
.venv\Scripts\python.exe 01.code\tools\eval_run.py --impl real --user demo_ui --codes q096 --name "q096回归"

# 多批合并（同题去重取最后一批）
.venv\Scripts\python.exe 01.code\tools\eval_merge.py run_A run_B --out 02.data\eval\reports\merged.md

# 题库审计（gold NULL 陷阱与不可执行项）
.venv\Scripts\python.exe 01.code\tools\eval_gold_audit.py

# 性能：首字（真 SSE）与阶段耗时
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
.venv\Scripts\python.exe 01.code\tools\latency_report.py

# 查看最近批次报告
Get-ChildItem 02.data\eval\reports | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name
```

### 关键配置说明

| 参数 | 说明 |
|--------|------|
| 测试规模 | 后端 **25 个文件 / 365 条用例**；前端 **32 条断言** + **8 张截图**；安全用例集 **53 项（恶意 40 + 合法 13）** |
| 口径锚点 | A1 三级总量 37283.7477 kWh（偏差 0）；A2 未计量占比 51.1977%；A3 有效分钟 98.7486%（质量报告 98.7482%，舍入差异）；A4 异常日 81/1442 |
| 评测规模 | 120 题 / 6 类题型 / 陷阱题 18 题 / 15 道多轮题（执行 135 条记录） |
| 判分 | 三档（列位置 → 列名归一 → 单列日期宽松）；容差 `max(1e-6, |值|×1e-3, 5e-3)`，比率列 1e-4 |
| 模式 | `real`（需 Key，无则 400 不回落）/ `selfcheck`（gold 回放，标注不代表模型能力）/ `auto` |
| 目标与实测 | 正确率 **≥85%**（实测最高：v3.9 主段 100/100）；结论命中 **≥90%**（实测 82.5–90.0% 区间）；延迟 **N2 ≤8s 达标 / N3 P95 ≤15s 未达标**（如实登记） |
| 报告四要素 | 模型名 / 提示词版本 / 温度（固定 0）/ 是否合并批次——缺一项即不合格 |
| 额度预估 | 单条记录成本 × 135 条；实测：deepseek-v4-flash 957,305｜kimi-k3 995,533｜qwen3.8-max 族 ≈113 万（单份 1M 装不下） |
