# 实训2 基于AI共创的智电析报UI/UX设计文档编写

## 一、实训目标

1. 掌握用「页面清单 + 信息架构 + 交互约束」驱动 AI 产出 UI/UX 设计文档的方法
2. 学会定义对话式数据分析界面的信息架构（三栏布局、会话列表、消息流、结果面板）
3. 理解「设计令牌 → 语义色 → 明暗双主题」的前后端分工：后端只写语义色，主题交前端
4. 能够设计「条件芯片」「阶段时间线」「SSE 状态映射」「五种呈现层次」等对话式界面关键组件
5. 掌握响应式四断点规则（1440 / 1366 / 1024 / 窄屏）与关键交互流程（上传、澄清、导出）

## 二、实训内容

### 1. 背景知识

- **UI/UX 设计文档的定位**：介于 PRD 与前端代码之间的"可直接实现"的界面规范，
  含**页面清单、全局规范、关键页面原型、组件规格、状态映射、响应式规则**六类内容。
  判定标准只有一条：前端拿到它就能照着写，不需要再问"这里到底怎么显示"。
- **对话式界面的特殊性**：一次提问不是"请求—响应"两步，而是**流式中间态**。
  界面上必须有"解析中 / 生成中 / 执行中"的可见反馈，因此**每个 SSE 事件都要定义界面表现**，
  否则会出现"点了发送却像卡死"的体验。本项目共 10 个事件：
  `status` / `slot` / `clarify` / `code` / `table` / `chart` / `narrative` / `meta` / `error` / `done`。
- **设计令牌（Design Token）**：把颜色、间距、圆角、字号收敛成一份命名清单，
  再由它同时生成 CSS 变量与 Ant Design 的主题令牌，避免"每个组件各写一套色值"。
- **语义色的两条铁律**：
  ① **峰 / 平 / 谷 / 未计量 / 异常 / 缺采**六种业务语义与颜色**一一绑定**，全站（含图表、表格标签）
  必须一致；
  ② **"按用途"与"按时段"两套拆分色板互不共用**——这是 PRD 红线 R3（两套拆分不可混加）
  在视觉层的落地：用户一眼就能看出这几段数据不是同一套口径。
- **为什么图表颜色不写进后端**：Plotly 的 figure 里颜色必须是字面量（**不认 CSS 变量**），
  若后端写死颜色，暗色主题下就会出现"白底黑字看不清"。因此约定：
  **后端只写语义色与文案，纸面/字体/网格等主题色一律由前端注入**（亮暗切换、导出固定亮色底）。
- **低保真线框图**：先用 SVG 画布局（不纠结配色），对齐后再进入视觉与代码，
  避免"边写边改布局"；线框图与最终实现不一致时，**要如实记录差异**（本项目 UIUX 第 14 章做法）。

### 2. 项目结构

本实训涉及的目录：

```
04.实训指导书/
├── AI共创过程文档/智电析报_UIUX设计文档.md     # 本实训产出（15 章）
└── images2/                                    # 线框图：7 张 SVG 源码 + 同名 PNG
01.code/frontend/src/
├── theme/tokens.ts                             # 设计令牌（→ CSS 变量 / AntD token / Plotly 主题）
├── styles/index.css                            # 令牌落地与 AntD 层叠处理（关闭 Tailwind preflight）
└── pages/{Login,Workbench,Reports,Eval,Profile}/  # 五个页面的组件实现
```

产物与章节对应关系（写文档时按这个顺序推进，每步都能独立验证）：

| 步骤 | 产出章节 | 关键产物 |
| --- | --- | --- |
| 步骤1 | 3 页面清单 / 5 关键页面原型 | 信息架构 + 5 页原型（含三栏宽度） |
| 步骤2 | images2/ 线框图 | 7 张 SVG + PNG |
| 步骤3 | 4 全局设计规范 | 令牌清单 + 语义色 + 明暗双主题 |
| 步骤4 | 6 条件芯片 / 7 图表与表格联动 | 芯片规格 + `customdata` 联动 |
| 步骤5 | 8 流式状态展示 / 9 错误提示样式 | 事件映射表 + 五种呈现层次 |
| 步骤6 | 10 响应式规则 / 12 关键交互流程 | 四断点规则 + 3 条流程图 |
| 收尾 | 13 组件清单与目录映射 / 14 落地记录 | 组件 ↔ 文件映射 + 差异记录 |

## 三、实训步骤

### 步骤1：交代页面清单与信息架构

**操作命令：**

```powershell
cd e:\power_report_agent
New-Item -ItemType Directory -Force 04.实训指导书\AI共创过程文档
```

**提示词（可直接复制给 AI）：**

```text
请为"智电析报"（对话式能源报表分析 Web 应用）编写 UI/UX 设计文档，先只做两件事：
1. 页面清单：共 5 个页面 —— 登录/注册、对话工作台、报表管理、评测中心、个人中心；
   分别说明每页的职责、进入方式、主要区域。
2. 对话工作台的信息架构：三栏布局（左：会话列表 240px；中：消息流，自适应；
   右：结果面板 408px，可折叠），逐栏列出区域编号与内容。
约束：中文界面；明暗双主题；Ant Design 5 + Tailwind 3（关闭 preflight，避免与 AntD 冲突）；
以 1440 宽为设计基准；右栏必须能收起（窄屏进抽屉）。先给骨架与区域编号，不要写样式细节。
```

**验证方法：**

```powershell
# 文档已生成
Test-Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md"
# 预期输出: True

# 页面清单齐备（应输出 5 行）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "^### 5\.\d" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 5

# 三栏宽度已写进文档
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "240|408"
# 预期输出: 命中"左会话列表 240 / 右结果面板 408"
```

**成功标志：**

- 文档存在，`## 3. 页面清单` 下**恰好 5 个 `### 5.x`** 小节
- 工作台原型含**区域编号**（左/中/右三栏各有编号，后续步骤引用这些编号）
- 三栏宽度明确写死为 `240 / flex / 408`

### 步骤2：生成低保真线框图（SVG）并离线转 PNG

**操作命令：**

```powershell
cd e:\power_report_agent
New-Item -ItemType Directory -Force 04.实训指导书\images2
# 线框图由 AI 以 SVG 源码输出后写入 images2/，再用本地 Edge 无头渲染转 PNG（离线、不依赖前端工程）
.venv\Scripts\python.exe 01.code\tools\svg2png.py "04.实训指导书\images2"
```

**提示词（可直接复制）：**

```text
用 SVG 源码输出 7 张低保真线框图（每张 1200x760，纯灰阶，不要配色）：
01_workbench（对话工作台三栏）
02_login（登录/注册）
03_reports（报表管理与上传向导三步）
04_eval（评测中心：发起面板 + 指标卡 + 逐题明细）
05_profile（个人中心：账号信息 + 统计 + 记录 + 危险区）
06_chip_states（条件芯片的四种状态：空 / 已选 / 多值折叠 / 编辑中）
07_error_states（五种呈现层次：内联校验 / 卡片错误块 / Toast / 拒答卡片 / 澄清卡片）
只输出 SVG 源码，不要解释；每张图里用文字标注区域编号。
```

**验证方法：**

```powershell
# 7 张 SVG 与 7 张 PNG 都在（各应输出 7）
(Get-ChildItem "e:\power_report_agent\04.实训指导书\images2\*.svg").Count
(Get-ChildItem "e:\power_report_agent\04.实训指导书\images2\*.png").Count
# 预期输出: 7 和 7

# 逐张核对工具链是否可用
Get-ChildItem "e:\power_report_agent\04.实训指导书\images2\" | Select-Object Name, Length
```

**成功标志：**

- `images2/` 下 **7 张 SVG + 7 张同名 PNG**（`01_workbench` … `07_error_states`）
- 工作台线框三栏宽度与文档一致（240 / flex / 408）
- PNG 可直接在文档里用相对路径引用：`![...](../images2/07_error_states.png)`

### 步骤3：定义设计令牌与语义色（前后端分工）

**操作命令：**

```powershell
cd e:\power_report_agent
New-Item -ItemType File -Force 01.code\frontend\src\theme\tokens.ts
```

**提示词（可直接复制）：**

```text
请定义前端设计令牌，输出 TypeScript 常量（文件 src/theme/tokens.ts）：
1. SEMANTIC_COLORS：light/dark 两套，键为 peak/flat/valley/unmetered/anomaly/missing
   （峰=橙红、平=蓝、谷=青绿、未计量=紫、异常=红、缺采=灰；暗色系整体提亮一档）；
2. PURPOSE_COLORS：按用途拆分专用（kitchen/laundry/hvac/unmetered），
   与 SEMANTIC_COLORS 的色值**不共用**（对应口径红线 R3）；
3. PLOTLY_THEME：paper_bgcolor / plot_bgcolor / fontColor / gridcolor / zerolinecolor，light/dark 两套；
4. LEGEND_ITEMS：右栏固定图例（中文标签 + 对应 CSS 变量名 --c-peak 等）；
5. themedFigure(figure, mode)：把主题**浅拷贝注入**后端返回的 figure
   （只改纸面/字体/网格/图例/hover，保留后端写的语义色与文案）。
约束：注释说明"为什么 JS 里还要存一份十六进制色值"（Plotly 不认 CSS 变量）。
```

**验证方法：**

```powershell
# 令牌文件存在且含两套色板
Test-Path "e:\power_report_agent\01.code\frontend\src\theme\tokens.ts"
Select-String -Path "e:\power_report_agent\01.code\frontend\src\theme\tokens.ts" -Pattern "SEMANTIC_COLORS|PURPOSE_COLORS|PLOTLY_THEME|themedFigure" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: True 和 4

# CSS 变量与令牌一一对应（应命中 --c-peak 等）
Select-String -Path "e:\power_report_agent\01.code\frontend\src\styles\index.css" -Pattern "--c-peak|--c-valley|--c-anomaly"
# 预期输出: 命中若干行
```

**成功标志：**

- `tokens.ts` 含 `SEMANTIC_COLORS` / `PURPOSE_COLORS` / `PLOTLY_THEME` / `LEGEND_ITEMS` / `themedFigure`
- `styles/index.css` 里的 `--c-*` 变量与令牌**逐条对应**
- 页面右上角切换主题后，**图表颜色与纸面底色随之变化**（后端 figure 不变）

### 步骤4：定义条件芯片与图表/表格联动规范

**操作命令：**

```powershell
# 先看后端 slot 的真实字段域（芯片必须与它同域，否则"编辑芯片 ≠ 改查询"）
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'01.code/backend'); from app.services.slot_service import SLOT_KEYS; print(SLOT_KEYS)"
```

**提示词（可直接复制）：**

```text
请编写「条件芯片（SlotChip）」组件规范与「图表/表格联动」规范：
1. 芯片字段域必须与后端 slot 的 8 个字段完全同域：time_range / granularity / metric /
   dimension / filters / compare / intent / chart_hint；filters 是对象数组 {field, op, value}；
2. 芯片四种状态：空（不显示）/ 已选（显示中文值 + 点 × 清除）/ 多值折叠（>3 条时显示 "+N"）/
   编辑中（点击弹出编辑气泡，确认后自动重算并追加一条消息）；
3. 联动：点图表数据点 → 表格定位到对应行；点表格行 → 图表高亮对应点；
   实现方式用 Plotly 的 customdata 携带行号，禁止用"猜行序"；
4. 说明为什么"芯片与 slot 同域"是硬要求。
```

**验证方法：**

```powershell
# 核对芯片字段域与后端一致（两边都应是这 8 个键）
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'01.code/backend'); from app.services.slot_service import SLOT_KEYS; print(sorted(SLOT_KEYS))"
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "time_range.*granularity.*metric.*dimension"
# 预期输出: 两处字段域一致
```

**成功标志：**

- UIUX 文档 6 章写明芯片**四种状态**与编辑气泡交互
- 7 章写明联动实现依赖 `customdata` 携带行号（不是行序猜测）
- 芯片选项值与后端 slot 取值域一致（时间范围 / 粒度 / 指标清单）

### 步骤5：定义 SSE 事件 → 界面映射与五种呈现层次

**操作命令：**

```powershell
# 事件清单以后端契约为准（见《智电析报_API接口文档》7.3 节 SSE 事件契约）
Select-String -Path "e:\power_report_agent\01.code\backend\app\services\analysis_service.py" -Pattern '"event": "' | Select-Object -First 12
```

**提示词（可直接复制）：**

```text
请编写「流式状态展示」与「错误提示样式」两章：
1. SSE 事件 → 界面映射表（10 个事件：status / slot / clarify / code / table / chart /
   narrative / meta / error / done），每行给出：界面反应、目标时延、对应验收指标；
2. 阶段时间线固定 5 阶段：解析问题 → 生成 SQL/代码 → 沙箱执行 → 生成图表 → 生成解读，
   并说明为什么"Schema 注入"不单独占一个阶段（它是会话级前置，单次提问内不重复）；
3. 五种呈现层次：内联校验 / 卡片错误块 / Toast / 拒答卡片 / 澄清卡片 —— 逐条给出使用场景、
   样式、出现位置；并明确：**只有执行失败与安全拦截用红色**，澄清用主色、拒答用中性灰；
4. 安全拦截样式：红描边 2px + 展示被拦截内容摘要与命中规则 + "已写入安全审计日志"，
   **不提供「重试」按钮**，改为「换个问法」。
```

**验证方法：**

```powershell
# 10 个事件都在文档里（应输出 10 行）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern '`(status|slot|clarify|code|table|chart|narrative|meta|error|done)`' | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: 10（或更多，含正文引用）

# 五种呈现层次齐备
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "内联校验|卡片错误块|Toast|拒答卡片|澄清卡片"
```

**成功标志：**

- 8.1 事件映射表覆盖 **10 个事件**（含 `error`，不能漏）
- 9.1 呈现层次**恰好五种**，且红色只用于执行失败与安全拦截
- 阶段时间线为 **5 阶段**，并写明与 PRD 6 步算法流程的差异原因

### 步骤6：响应式四断点与关键交互流程

**操作命令：**

```powershell
# 先确认前端断点常量（与文档必须一致）
Select-String -Path "e:\power_report_agent\01.code\frontend\src" -Include *.css,*.ts -Pattern "1440|1366|1024" -Recurse | Select-Object -First 10
```

**提示词（可直接复制）：**

```text
请编写「响应式规则」与「关键交互流程」两章：
1. 四断点：>=1440 三栏全展开；1366–1439 隐藏副标题、压缩间距；1024–1365 右栏收进右侧抽屉
   （输入区上方给「查看图表与表格」按钮）；<1024 单栏 + 会话列表进左侧抽屉。
   每条给出"优先保住什么"（主链路是：提问 → 看结论 → 看图表）；
2. 三条关键流程（用 Mermaid 流程图）：上传入库（两步 + 重复文件复用分支）、
   澄清回路（澄清卡 → 点选回填 → 原地继续执行）、结果导出（图片 / 表格）；
3. 说明窄屏为什么优先保主链路，而不是把三栏等比缩小。
```

**验证方法：**

```powershell
# 断点规则齐备
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "1440|1366|1024"
# 预期输出: 命中四断点规则

# 交互流程图齐备（Mermaid）
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "^```mermaid" | Measure-Object | Select-Object -ExpandProperty Count
# 预期输出: >= 3

# 实操：把浏览器窗口缩到 1100 宽，右栏应变为抽屉
# 预期输出: 输入区上方出现「查看图表与表格」
```

**成功标志：**

- 10 章写明四断点及各自行为（含"右栏收抽屉"与"会话列表进抽屉"）
- 12 章含**至少 3 条** Mermaid 交互流程图（上传 / 澄清 / 导出）
- 缩放到 1024–1365 宽时，右栏确实收进抽屉且能找到入口按钮

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| UIUX 文档存在 | `Test-Path "...\智电析报_UIUX设计文档.md"` | True |
| 页面清单齐备（5 页） | `Select-String "^### 5\.\d"` 计数 | 5 |
| 线框图齐备（7 张） | `Get-ChildItem images2\*.png` 计数 | 7 |
| 设计令牌落地 | `Select-String tokens.ts -Pattern "SEMANTIC_COLORS\|themedFigure"` | 命中 |
| 两套色板互斥（R3） | 对比 `SEMANTIC_COLORS` 与 `PURPOSE_COLORS` 色值 | 无重复色值 |
| 芯片与 slot 同域 | 两边打印 `SLOT_KEYS` | 8 个键一致 |
| 事件映射齐备 | 文档内检索 10 个事件名 | 10 个都在 |
| 五种呈现层次 | 检索"内联校验/卡片错误块/Toast/拒答卡片/澄清卡片" | 5 种齐备 |
| 明暗双主题 | 页面右上角切换后 `data-theme` 变化 | 切换成功且图表跟随 |
| 响应式生效 | 视口 1440 / 1280 / 1024 分别观察 | 三栏 / 右栏收 / 单栏正确 |
| 端到端回归 | `node 01.code\tools\ui_flow_check.mjs` | 全部断言 PASS |

## 五、故障排除

### 问题1：暗色主题下图表文字看不清

**现象：** 切到暗色后，图表标题、坐标轴刻度、网格线仍是深色，与深色纸面糊在一起。

**原因：** 后端 figure 里写死了 `paper_bgcolor` / `font.color` / 网格色（后端不该管主题）。

**解决方案：** 把这些键从后端**分析链路**的布局里移除（只保留语义色与文案），
改由前端 `themedFigure(figure, mode)` 注入；切换主题时重新 `Plotly.react`。

注意有一个**刻意写死的例外**：导出图片走 `EXPORT_LIGHT_LAYOUT`（固定亮色底、2 倍图，
见 UIUX 7.1），那里的白色底是**故意**的——导出件不应受当前主题影响。核对方法：

```powershell
Select-String -Path "e:\power_report_agent\01.code\backend\app\services\chart_service.py" -Pattern "paper_bgcolor|plot_bgcolor"
# 预期输出: 只有两处 —— ① `_base_layout` 的 docstring（声明"不写主题色"）；
#           ② `EXPORT_LIGHT_LAYOUT`（导出固定亮色底）。
#           **分析链路生成的 figure 里不含这两个键**（由前端 themedFigure 注入）。
```

### 问题2：主题切换后首帧仍是旧主题

**现象：** 点主题开关后，页面已经变暗，但图表第一帧还是亮色（或截图里是亮色）。

**原因：** 渲染路径上拿到了**上一帧**的图；无头截图时更常见（DOM 更新与绘制不同步）。

**解决方案：** 主题变化后**丢弃一帧再渲染**（`requestAnimationFrame` 或等待一次重绘）；
截图脚本 `ui_shot.mjs` 已按此处理：

```powershell
node 01.code\tools\ui_shot.mjs
# 预期输出: 8 张截图，暗色那张图表底色为深色
```

### 问题3：条件芯片显示与查询条件不一致

**现象：** 芯片显示"2020 年"，但实际查询用的是"全期"；或清除芯片后仍带旧条件。

**原因：** 芯片字段域与后端 slot 不同域（例如芯片自己发明了 `period` 字段），
或 `filters` 用了字符串数组而不是 `{field, op, value}` 对象数组。

**解决方案：** 芯片只渲染后端返回的 slot 原值（不要在前端二次加工）；
`filters` 统一对象数组。核对：

```powershell
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'01.code/backend'); from app.services.slot_service import SLOT_KEYS; print(sorted(SLOT_KEYS))"
# 预期输出: ['chart_hint','compare','dimension','filters','granularity','intent','metric','time_range']
```

### 问题4：窄屏下右栏"消失"，以为没出数据

**现象：** 窗口缩小后右栏不见了，用户以为查询没结果。

**原因：** 右栏按响应式规则收进了抽屉，但**入口不明显**。

**解决方案：** 输入区上方固定「查看图表与表格」按钮，并在结果生成时给一次高亮提示；
文档 10 章写明该入口。核对：

```powershell
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "查看图表与表格"
# 预期输出: 命中（响应式规则里写明入口）
```

### 问题5：线框图与最终实现不一致

**现象：** 文档里的线框图是三栏，实现时右栏改成了可折叠抽屉但线框没改。

**原因：** 设计变更后只改了代码，没回写线框图与文档。

**解决方案：** 变更后**同步改线框图与文档**；无法同步时，在 UIUX 文档第 14 章
「前端落地记录」里**如实记录差异与原因**（本项目做法，禁止"文档比实现更漂亮"）：

```powershell
Select-String -Path "e:\power_report_agent\04.实训指导书\AI共创过程文档\智电析报_UIUX设计文档.md" -Pattern "^## 14\.|落地记录" -Context 0,3
# 预期输出: 命中第 14 章落地记录与差异说明
```

### 问题6：图片转换脚本报 `UnicodeDecodeError: 'gbk' codec`

**现象：** 运行 `svg2png.py` 时控制台抛 `UnicodeDecodeError`（但图其实已经生成）。

**原因：** 脚本输出 UTF-8 中文，经 Windows 控制台管道转发时按 GBK 解码失败——**不是出图失败**。

**解决方案：** 直接把输出落文件再查看，或先设置编码：

```powershell
$env:PYTHONIOENCODING = "utf-8"
chcp 65001
.venv\Scripts\python.exe 01.code\tools\svg2png.py "04.实训指导书\images2"
```

## 六、参考代码

### 完整目录结构

```
04.实训指导书/
├── AI共创过程文档/智电析报_UIUX设计文档.md      # 本实训产出（15 章）
├── images2/                                     # 线框图 7 张（SVG 源码 + PNG）
│   ├── 01_workbench.svg / .png                  # 对话工作台三栏
│   ├── 02_login.svg / .png
│   ├── 03_reports.svg / .png
│   ├── 04_eval.svg / .png
│   ├── 05_profile.svg / .png
│   ├── 06_chip_states.svg / .png                # 条件芯片四种状态
│   └── 07_error_states.svg / .png               # 五种呈现层次
└── images_arch/                                 # 架构与流程图（实训3 使用）
01.code/frontend/src/
├── theme/tokens.ts                              # 设计令牌与 Plotly 主题注入
├── styles/index.css                             # CSS 变量落地（AntD 与 Tailwind 层叠）
└── pages/Workbench/                             # 组件实现（见 13 章组件清单）
```

### 常用命令

```powershell
# 线框图 SVG → PNG（离线，Edge 无头；PNG 与源 SVG 同目录同名）
.venv\Scripts\python.exe 01.code\tools\svg2png.py "04.实训指导书\images2"

# 查看设计令牌
Get-Content 01.code\frontend\src\theme\tokens.ts | Select-Object -First 60

# 打印后端 slot 字段域（芯片必须与它同域）
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'01.code/backend'); from app.services.slot_service import SLOT_KEYS; print(sorted(SLOT_KEYS))"

# 页面截图（7 张，需前后端都在跑）
node 01.code\tools\ui_shot.mjs

# 端到端回归断言（含主题、芯片、联动、错误块）
node 01.code\tools\ui_flow_check.mjs
```

### 关键配置说明

| 参数 | 取值 | 说明 |
|------|------|------|
| 三栏宽度 | 240 / flex / 408 | 左会话列表 / 中消息流 / 右结果面板 |
| 断点 | 1440 / 1366 / 1024 | 三栏全展开 / 隐藏副标题 / 右栏收抽屉；<1024 单栏 |
| 语义色（亮） | `peak #E8590C`｜`flat #1D4ED8`｜`valley #0CA678`｜`unmetered #7048E8`｜`anomaly #E03131`｜`missing #ADB5BD` | 与暗色两套，键名不变 |
| 用途色板 | `kitchen / laundry / hvac / unmetered` | 与语义色**互不共用**（红线 R3 的视觉落地） |
| 图表主题 | `themedFigure(figure, mode)` | 后端只写语义色，纸面/字体/网格由前端注入 |
| SSE 事件 | `status` `slot` `clarify` `code` `table` `chart` `narrative` `meta` `error` `done` | 10 个，前端状态机依据 |
| 阶段时间线 | 解析问题 → 生成 SQL/代码 → 沙箱执行 → 生成图表 → 生成解读 | 5 阶段（Schema 注入是会话级前置，不占阶段） |
| 呈现层次 | 内联校验 / 卡片错误块 / Toast / 拒答卡片 / 澄清卡片 | 只有执行失败与安全拦截用红色 |
