# 实训1 基于AI共创的智电析报产品需求规格说明书编写

## 一、实训目标

1. 掌握用「角色设定 + 输入材料 + 功能模块 + 输出要求」的结构化方式向 AI 描述产品需求
2. 学会为对话式数据分析（ChatBI）系统定义功能需求（F1–F6）与非功能需求（N1–N7 / S1–S8）
3. 理解 PRD 的 12 章标准结构，掌握「接口清单」「错误码双轨制」「数据口径规范」三类硬约束的写法
4. 掌握「TBD 待回填机制」与「修订记录版本闭环」，避免文档与代码脱节
5. 能够通过多轮清单式评审，把 AI 生成的初版 PRD 迭代到可交付状态（本项目实测 v1.0.0 → v1.19.5，共 26 版）

## 二、实训内容

### 1. 背景知识

- **对话式数据分析（ChatBI）与 Text2SQL**：由大语言模型把「上个月用电量同比变化如何」这类自然语言问题转译为 SQL 或 pandas 分析代码，执行后再把结果转成图表与文字解读，使非技术岗位也能直接拿到数据结论。
- **PRD（产品需求规格说明书）**：产品开发的"地图"，明确功能边界、业务规则、接口契约与验收标准。后续 UI/UX 设计、数据库设计、接口文档、测试报告都以它为输入。
- **AI 共创**：以 AI 编程助手（本项目使用 CodeBuddy）为协作方完成文档与代码。**关键留痕**：全过程提示词按轮次记录在项目根 `提示词.txt`，本实训的每一步都能追溯到真实指令。
- **数据口径**：电力报表领域最容易出错的地方（kW 与 kWh 不可相加、未计量用电公式、峰平谷与分设备两套正交拆分、比率类字段不可 SUM）。这些必须写进 PRD 并固化成红线规则 R1–R7，否则大模型会"算得对但口径错"。
- **数据集**：UCI Individual household electric power consumption（分钟级，2,075,259 行，2006-12 至 2010-11），作为报表数据底座。

### 2. 项目结构

本实训涉及的文件与目录：

```
power_report_agent/
├── 能源电力报表智能分析Agent项目任务书V1.0.0.docx   # 需求来源（10 项交付物）
├── 我的选题.txt                                     # 选题与技术栈
├── household_power_consumption.txt                  # 原始数据集（未清洗）
├── 提示词.txt                                       # AI 共创全过程留痕（本实训的核心素材）
├── AI共创面诊分析系统/                              # 示例项目（仅参考目录结构与文档风格，代码不复用）
├── 01.code/                                         # 代码（后续实训产出）
├── 02.data/                                         # 数据（后续实训产出）
└── 04.实训指导书/
    └── AI共创过程文档/
        └── 智电析报_PRD文档.md                      # 本实训产出（PRD）
```

## 三、实训步骤

### 步骤1：给 AI 交代项目边界与协作规则

**操作：**

在 AI 对话窗口输入以下内容（摘自本项目真实第一轮对话，见 `提示词.txt`）：

```bash
帮我分析一下 @能源电力报表智能分析Agent项目任务书V1.0.0.docx 我当前的任务是什么，我要怎么做？
@我的选题.txt 里的内容是我的选题，里面有罗列了一些技术栈等信息。
@household_power_consumption.txt 是我从参考网址
（https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption）下载的数据集。
@AI共创面诊分析系统 是示例项目，可以参考示例项目来实现任务书里的任务，
但这个示例项目与我现在的项目不同，代码要从头开始。
按照示例项目的流程，我当前的身份是全栈开发，要从编写 PRD 文档开始。
记住 @AI共创面诊分析系统 只是示例，只能是参考，不能直接拿来用。
等我"确认"再开始，之后每一步都要等我"确认"，确保我能熟悉流程。
所有生成的文件都放到当前目录下，不要放到其他目录下。
```

**操作结果：**

- AI 先输出对任务书的理解（10 项交付物）与执行路线（PRD → 设计文档 → 数据清洗 → 架构 → 后端 → 前端 → 联调 → 评测 → 文档）
- 随后进入"等确认"节奏：每完成一步停下，由你确认后继续

**操作验证：**

```powershell
# 确认项目根目录下的输入材料齐备
Test-Path "e:\power_report_agent\能源电力报表智能分析Agent项目任务书V1.0.0.docx"
Test-Path "e:\power_report_agent\我的选题.txt"
Test-Path "e:\power_report_agent\household_power_consumption.txt"
Test-Path "e:\power_report_agent\AI共创面诊分析系统"
# 预期输出: 四行均为 True
```

**成功标志：**

- 四份输入材料齐备（`Test-Path` 四行均为 `True`）
- AI 复述的任务边界与你的理解一致（10 项交付物、6 大模块、参考项目只作参考）
- 已进入"等确认"节奏：AI 明确表示"等你确认再继续"，而不是一口气做完
- 输出目录约定明确（本项目为**项目根目录**，不散落到其它目录）

**操作说明：**

1. **为什么要先交代边界**：AI 不会自动知道"示例项目只能参考、不能复制代码"，也不会知道你的角色是"全栈开发"。边界不清会出现"直接抄示例代码"或"文档写到一半跑偏"。
2. **为什么要"等确认"**：文档是后续所有工作的输入，PRD 阶段跑偏，后面全部返工。逐步确认能让每一步都可控、可学习。
3. **为什么要指定输出目录**：默认输出目录可能与项目结构不一致，导致交付物散落。

### 步骤2：生成 PRD 初版（先要骨架，再要内容）

**操作：**

```bash
先写 PRD 文档的初版，我看看效果。
注意：不要调用无关的记忆，当前是一个新的项目，没有可用的记忆。
```

**操作结果：**

AI 生成 `04.实训指导书\AI共创过程文档\智电析报_PRD文档.md` 初版（v1.0.0），包含 12 章：

```
1 文档概述        2 产品背景        3 产品目标        4 用户角色与场景
5 功能需求(F1–F6) 6 非功能需求(N/S) 7 数据需求        8 数据口径与报表规范
9 接口需求        10 项目规划       11 风险评估       12 附录（修订记录/TBD）
```

**操作验证：**

```powershell
$prd = "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md"
Test-Path $prd
# 预期输出: True

# 检查 12 个一级章节是否齐备
Select-String -Path $prd -Pattern "^## \d+\. " | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 12

# 检查功能需求条目数（F1–F6）
Select-String -Path $prd -Pattern "^### F\d" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 6
```

**成功标志：**

- PRD 文件已生成，且**12 个一级章节齐备**（`^## \d+\. ` 计数 = 12）
- 功能需求 **F1–F6 六组齐备**，与任务书的 6 个模块**一一对应**
- 初版允许细节不全（结构优先），但章节骨架**已可进入评审**

**操作说明：**

- 初版**不追求完美**，先确认章节结构是否覆盖任务书的 6 个模块（数据源接入 / Text2SQL 与分析编排 / 可视化 / 多轮追问 / 评测 / 用户管理）。
- 本项目把任务书的 6 个模块映射为 F1–F6，一一对应，便于后续验收时逐条核对。

### 步骤3：清单式评审——用"逐条意见"驱动迭代

**操作：**

不要笼统地说"再完善一下"，而是**逐条**给出修改点。以下是本项目第五轮的真实评审清单（节选）：

```bash
1. 第 5.2 节 F1.3 动态建表
用户上传的报表会动态生成新表，建议明确：这些动态表也属于 reports.db，
但只由上传入库服务写入，LLM 生成代码永远只读；并在 7.4 表清单下补一句说明。

2. 第 5.3 节 F2.2 SQL 白名单
聚合查询本身返回行数很少，建议补一句：对聚合查询，LIMIT 仅作为安全兜底，不改变业务语义。

3. 第 8.4 节 峰平谷时段
小时区间写法 00:00–07:59 容易被误读为包含 07:59 整点，建议加注：
"即 00:00:00 至 07:59:59，其余时段同理。"

4. 第 8.6 节 日均用电折算
公式中 active_minutes 可能为 0（整段缺失），建议补一句：
"active_minutes = 0 时，daily_avg_kwh 置为 NULL，不参与后续计算。"

5. 第 9.2 节 SSE 事件
slot 事件的 filters 示例建议统一为对象数组，例如
"filters": [{"field":"is_weekend","op":"=","value":true}]，便于前端渲染条件芯片。

6. 第 10.3 节 资源需求
后端依赖建议补充：python-multipart、bcrypt、PyJWT、sse-starlette。

7. 第 12.4 节 待回填事项
模型名回填时建议同时记录温度参数（评测固定 temperature=0）与并发限制；
另可增加一条：安全用例集（≥20 条恶意样例）的实际通过率。

把上述小点合并进 PRD，升版。
```

**操作结果：**

- AI 按条目逐一修改，PRD 从 v1.0.0 升到 v1.1.0，`12.1 修订记录` 追加一行（版本 / 日期 / 修订内容）
- 关键收益：**口径条款被追问到"可执行"**——例如"00:00–07:59 即 00:00:00–07:59:59"这类细节，是后续 SQL 与评测判分的依据

**操作验证：**

```powershell
$prd = "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md"

# 1) 修订记录是否成表且有版本行
Select-String -Path $prd -Pattern "^\| v1\.\d+\.\d+ \|" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 大于 20（本项目 26 版，逐版留痕）

# 2) 口径红线 R1–R7 是否齐备
Select-String -Path $prd -Pattern "R[1-7] " | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 大于 0（R1–R7 均被引用）

# 3) 待回填事项是否为 TBD 前缀（避免与其他编号冲突）
Select-String -Path $prd -Pattern "TBD\d" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 大于 0
```

**成功标志：**

- `12.1 修订记录` 成表且逐版留痕（`^\| v1\.\d+\.\d+ \|` 计数 > 20）
- 口径红线 **R1–R7** 在 PRD 内被显式引用
- 「程序要照着做」的条款（时段边界、NULL 规则、字段类型）**写到无歧义**——
  即每条都能用一句 SQL 或一条断言验证（本项目把这类条款逐条落到 8 章）

**操作说明：**

1. **清单式评审优于形容词式评审**：一条意见对应一处改动，AI 不会漏改，你也能逐条验收。
2. **评审要点是"可执行性"**：凡是模型或程序要照着做的条款（时段边界、NULL 规则、字段类型），必须写到无歧义。
3. **每条都要落到章节号**：便于 AI 定位，也便于后续在修订记录里回溯。

### 步骤4：冻结接口与错误码（双轨制）

**操作：**

```bash
请补齐第 9 章接口需求：
1. 每个端点写清 method / path / 请求体 / 响应体（业务编号用 report_code、
   conversation_code、record_code、run_code，不使用自增主键）；
2. 统一响应包络 {code, message, data, request_id}；
3. 错误码表采用双轨制：HTTP 状态码与业务 code 同值，并在 data.error_code 返回 E_* 常量；
4. 每个错误码给出 hint（给用户的修复建议）。
```

**操作结果：**

- 第 9 章形成 7 组接口（auth / reports / conversations / analysis / charts / eval / system）与错误码总表
- 后续实现严格按此执行（本项目实测 **39 个端点**，前端联调时未因契约模糊返工）

**操作验证：**

```powershell
# 从运行中的后端导出 OpenAPI，核对端点数量与契约（需先启动后端，见附录 B）
$spec = Invoke-RestMethod -Uri "http://127.0.0.1:8000/openapi.json" -Method GET
$spec.paths.PSObject.Properties.Name.Count
# 预期输出: 与 PRD 第 9 章列出的端点数一致（本项目 39）

# 抽查统一包络：健康检查
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/system/health" -Method GET |
  ConvertTo-Json -Depth 3
# 预期输出: 含 code=0 / message / data（含 llm.configured）/ request_id 四个字段
```

**成功标志：**

- 第 9 章含 **7 组接口**与错误码总表，且每个错误码都有 `hint`
- 运行中后端的 OpenAPI 端点计数**与文档一致**（本项目 **39** 条）
- `/api/v1/system/health` 返回**四字段包络**（`code` / `message` / `data` / `request_id`）
- 业务编号统一用 `report_code` / `conversation_code` / `record_code` / `run_code`（不暴露自增主键）

**操作说明：**

- **双轨制的价值**：前端既能按 HTTP 状态码做通用处理，又能按 `error_code` 做精准文案（本项目 UI/UX 文档据此列出四级错误呈现）。
- **hint 字段**：把"为什么失败、怎么修"直接写进响应，避免用户拿到 `E_FILE_TYPE` 却不知道要转成 UTF-8 CSV。

### 步骤5：需求追溯——把 F1–F6 映射到交付物

**操作：**

```bash
请在第 10 章补一张「需求 → 交付物 → 验收方式」对照表：
把 F1–F6 与任务书的 10 项交付物对应起来，并写明每项的验收方式（命令、指标或页面）。
```

**操作结果：**

形成可逐条勾选的对照表，例如：F1 → 交付物 2/3（清洗脚本 + Schema 模块）→ `pytest` 与 `A1–A5` 口径校验；F5 → 交付物 8 → 120 题评测报告。

**操作验证：**

```powershell
$prd = "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md"
Select-String -Path $prd -Pattern "交付物" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 大于 10（需求与交付物的对应关系被显式引用）
```

**成功标志：**

- 第 10 章含「**需求 → 交付物 → 验收方式**」对照表
- F1–F6 每一条都能指到具体交付物与可执行的验收方式（命令 / 指标 / 页面）
- "交付物"在 PRD 内被显式引用（`Select-String "交付物"` 计数 > 10）

**操作说明：**

- 对照表是**答辩时最好用的材料**：评审问"你怎么证明需求都实现了"，直接指着表逐条给证据（测试、截图、评测报告）。

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| PRD 文件存在 | `Test-Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md"` | True |
| 12 个一级章节齐备 | `Select-String -Pattern "^## \d+\. "` 计数 | 12 |
| 功能需求 F1–F6 齐备 | `Select-String -Pattern "^### F\d"` 计数 | 6 |
| 非功能需求 N/S 齐备 | `Select-String -Pattern "^### N\d|^### S\d"` 计数 | 大于 10 |
| 口径红线 R1–R7 已固化 | `Select-String -Pattern "R[1-7] "` 计数 | 大于 0 |
| 修订记录逐版留痕 | `Select-String -Pattern "^\| v1\.\d+\.\d+ \|"` 计数 | 大于 20 |
| 待回填事项使用 TBD 前缀 | `Select-String -Pattern "TBD\d"` 计数 | 大于 0 |
| 接口清单与实现一致 | `Invoke-RestMethod "http://127.0.0.1:8000/openapi.json"` 端点计数 | 39 |
| 统一响应包络生效 | `Invoke-RestMethod ".../api/v1/system/health"` | 含 code / message / data / request_id |

## 五、故障排除

### 问题1：AI 生成的 PRD 缺章节或章节顺序混乱

**现象：** 初版只生成了 8 章，缺少"数据口径与报表规范""接口需求""风险评估"。

**解决方案：**

```bash
# 不要重开对话（会丢上下文），直接给"章节清单"要求补齐
PRD 缺少以下章节，请补齐并保持 12 章顺序：
8 数据口径与报表规范、9 接口需求、10 项目规划、11 风险评估、12 附录
其中第 8 章必须包含：量纲换算、未计量用电公式、两套正交拆分、比率类字段语义。
```

### 问题2：需求写得"不可验证"（如"响应要快""结果要准确"）

**现象：** 验收时无法判断是否达标，答辩时给不出证据。

**解决方案：**

```bash
请把所有非功能需求改写为可测量指标，并写明测量方式：
示例：单轮问答首字 ≤2s、平均 ≤8s、P95 ≤15s（SSE 事件时间戳测量）；
上传 200 万行 CSV 解析 ≤60s（脚本计时）；Text2SQL 正确率 ≥85%（120 题评测集）。
```

### 问题3：文档口径与数据集实际不符

**现象：** 文档写"电压正常范围 200–260V"，但实测该规则命中 0 次，无法产生异常项。

**解决方案：**

```bash
# 用真实数据反查后再改文档（本项目实测后收紧为 210–250V 且当日累计 ≥30 分钟）
cd e:\power_report_agent
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'02.data\reports.db'); print(c.execute('select min(voltage_v), max(voltage_v) from report_minute').fetchone())"
```

### 问题4：文档版本越改越乱，找不回上一版

**现象：** 改到 v1.5 时想对比 v1.2 的接口定义，但已覆盖。

**解决方案：**

```bash
# 每完成一个交付物批次，把 4 份文档快照归档到 历史版本/ 子目录
# 本项目归档在：04.实训指导书\AI共创过程文档\历史版本\{PRD文档,系统架构设计文档,数据库设计文档,UIUX设计文档}
Get-ChildItem "e:\power_report_agent\04.实训指导书\AI共创过程文档\历史版本\PRD文档" |
  Select-Object -ExpandProperty Name
# 预期输出: 多个版本的 .md 快照（PRD 共 14 份）
```

### 问题5：PowerShell 里中文路径/输出乱码

**现象：** 执行 `Test-Path` 返回 False，或控制台输出乱码。

**解决方案：**

```powershell
# 1) 确认路径拼写与盘符（本项目在 e: 盘，不是 d:）
# 2) 把控制台切到 UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 3) 运行 Python 脚本时显式指定编码
$env:PYTHONIOENCODING = "utf-8"
```

## 六、参考代码

### 完整目录结构

```
power_report_agent/
├── 能源电力报表智能分析Agent项目任务书V1.0.0.docx
├── 我的选题.txt
├── household_power_consumption.txt
├── 提示词.txt
├── AI共创面诊分析系统/                  # 示例项目（仅参考）
├── 01.code/                            # 后端(backend) / 前端(frontend) / 脚本(scripts) / 工具(tools)
├── 02.data/                            # reports.db / power_agent.db / reports / schema / quality / eval
└── 04.实训指导书/
    ├── AI共创过程文档/
    │   ├── 智电析报_PRD文档.md          # 本实训产出
    │   ├── 智电析报_UIUX设计文档.md
    │   ├── 智电析报_数据库设计文档.md
    │   ├── 智电析报_系统架构设计文档.md
    │   └── 历史版本/                    # 各版本快照
    ├── images2/                         # 线框图（实训2 用）
    └── images_arch/                     # 架构图（实训3 用）
```

### 常用命令

```powershell
# 查看 PRD 全文（分页）
Get-Content "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md" |
  Select-Object -First 60

# 搜索某个需求的全部落点（示例：拒答策略）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md" -Pattern "拒答"

# 统计各章标题，确认结构
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_PRD文档.md" -Pattern "^## \d+\. .+" |
  Select-Object -ExpandProperty Line

# 查看 AI 共创留痕（本实训的每一步都能追溯）
Get-Content "e:\power_report_agent\提示词.txt" | Select-Object -First 40
```

### 关键配置说明

| 项 | 说明 | 示例 |
|------|------|------|
| 功能需求编号 | F1–F6 与任务书 6 个模块一一对应 | F1 数据源接入与 Schema 理解 |
| 非功能需求编号 | N 性能 / S 安全 | N1 首字 ≤2s、S1 生成代码沙箱 |
| 口径红线 | R1–R7，模型与评测共用 | R3 两套正交拆分不可混加 |
| 接口响应包络 | 四字段统一 | `{code, message, data, request_id}` |
| 错误码双轨 | HTTP 与业务 code 同值 + `data.error_code` | 403 + `E_FORBIDDEN` |
| 待回填事项 | TBD 前缀 + 触发时机 | TBD1 前端依赖版本（实测后回填） |
| 版本闭环 | 4 份文档头部互相标注版本 | PRD v1.19.5 ↔ 架构 v1.0.11 ↔ DB v1.12.0 ↔ UIUX v1.14.3 |
