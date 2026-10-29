# 实训4 基于AI共创的智电析报前端开发

## 一、实训目标

1. 掌握用 Vite + React + TypeScript 从零搭建前端工程，并按需引入 Ant Design 与 Tailwind
2. 学会封装 HTTP 与 **SSE** 客户端（统一包络解析、401 拦截、草稿保留、AbortController）
3. 能够实现对话工作台：消息流、条件芯片、阶段时间线、图表与表格联动、澄清卡、错误块
4. 掌握 Plotly 的自封装用法（`Plotly.react` + 主题注入 + 图片导出）
5. 能通过无头浏览器回归脚本（`ui_flow_check.mjs`）把用户反馈的缺陷固化为可重跑的断言

## 二、实训内容

### 1. 背景知识

- **前后端分工**：后端给"数据与语义"，前端给"主题与交互"。
  具体到图表：后端只产 `figure` JSON（含 `trace` 的**语义色**：峰 / 平 / 谷 / 未计量 / 异常 / 缺采），
  纸面底色、字体色、网格线、图例位置等**主题相关**的东西一律由前端注入
  ——因为 Plotly **不认 CSS 变量**，后端写死颜色会让暗色主题"白底黑字看不清"。
- **SSE 事件驱动**：一次提问会依次推送
  `status → slot → code → status → table → chart → narrative → meta → done`（错误时插入 `error`），
  前端状态机据此推进"阶段时间线"，用户能看到搜索"卡在哪一步"，而不是一个转圈到底。
- **为什么不用原生 `EventSource`**：分析接口是 `POST` + `Authorization` 头 + SSE，
  而原生 `EventSource` **只支持 GET、不能带自定义请求头** ⇒ 必须用
  `@microsoft/fetch-event-source`（本项目选型）或自写 `fetch` 流式读取。
- **SSE 客户端不做自动重连**：提问是**有副作用的写操作**（会落分析记录、会调用模型），
  断线重连等于"再问一遍"，所以只做**中断**（`AbortController`）+ 明确报错。
- **登录态与草稿**：token 存 localStorage（`zdxb.token`），axios 拦截器自动加 `Authorization`；
  401 时清 token + 提示 + 回登录页，并且**保留用户未发送的输入**（`zdxb.draft`）——
  这条是用户实测反馈出来的体验点。
- **Tailwind 与 AntD 共存**：Tailwind 关闭 `preflight`（否则会重置 AntD 的按钮/表单样式），
  AntD 用 CSS 变量与令牌对齐主题，两者共用同一套设计令牌（见实训2）。
- **为什么不装 `react-plotly.js`**：该库年久失修、类型基于 React 17，与 React 18 的
  `StrictMode` 双调用配合不好；本项目**自封装容器**（`ResultChart.tsx`）——
  直接 `Plotly.react(div, figure, layout, {responsive:true})`，可控且体积更小。

### 2. 项目结构

本实训产出（`01.code/frontend/`，共 32 个 `.tsx`）：

```
01.code/frontend/
├── package.json / vite.config.ts / tsconfig.json / tailwind.config.js / postcss.config.js
└── src/
    ├── main.tsx / App.tsx                           # 挂载与路由 + 鉴权守卫
    ├── layouts/                                     # 全局框架（3 个）
    │   ├── MainLayout.tsx                           # 顶部栏 + 内容区
    │   ├── TopBar.tsx                               # Logo / 四页导航 / 主题切换 / 用户菜单
    │   └── PageHeader.tsx                           # 各页统一页头（排版对齐）
    ├── pages/
    │   ├── Login/index.tsx                          # 登录 / 注册（表单校验 + 跳转 next）
    │   ├── Workbench/                               # 对话工作台（14 个组件，本实训核心）
    │   │   ├── index.tsx                            # 三栏装配 + SSE 状态机接入
    │   │   ├── ConversationList.tsx                 # 会话列表（置顶/搜索/删除/重命名）
    │   │   ├── MessageStream.tsx / Composer.tsx      # 消息流与输入区
    │   │   ├── SlotBar.tsx                          # 条件芯片栏
    │   │   ├── StageTimeline.tsx / ExecInfo.tsx      # 阶段时间线与执行信息
    │   │   ├── CodeBlock.tsx / ResultTable.tsx       # SQL/代码块与结果表
    │   │   ├── ResultChart.tsx / ResultPanel.tsx     # 图表容器与右栏结果面板
    │   │   ├── StateCards.tsx                        # 澄清卡 / 拒答卡 / 错误块
    │   │   ├── SchemaDrawer.tsx                      # 字段字典抽屉
    │   │   └── Typewriter.tsx                        # 解读打字机效果
    │   ├── Reports/                                 # 报表管理（4 个）：index / UploadWizard /
    │   │                                            #   PreviewDrawer / QualityPanel
    │   ├── Eval/                                    # 评测中心（5 个）：index / StartPanel /
    │   │                                            #   MetricsPanel / RunsList / RunQuestionsTable
    │   └── Profile/                                 # 个人中心（3 个）：index / AccountDialogs /
    │                                                #   RecordDetailDrawer
    ├── stores/                                      # zustand：auth / chat / reports / theme
    ├── api/                                         # client / sse / token / auth / conversations /
    │                                                #   reports / analysis / charts / eval / system / types / errors
    ├── theme/tokens.ts                              # 设计令牌（实训2 产出）
    ├── utils/{format,toast}.ts
    └── styles/index.css                             # CSS 变量与 AntD 层叠处理
```

产物与步骤对应：步骤1 工程骨架 → 步骤2 主题与网络层 → 步骤3 登录页 →
步骤4 工作台（14 组件）→ 步骤5 其余三页 → 步骤6 回归固化。

## 三、实训步骤

### 步骤1：工程初始化与依赖安装

**操作命令：**

```powershell
cd e:\power_report_agent\01.code
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install antd @ant-design/icons plotly.js-dist-min axios zustand @microsoft/fetch-event-source react-router-dom dayjs
npm install -D tailwindcss@3 postcss autoprefixer
```

**提示词（可直接复制给 AI）：**

```text
请在 01.code/frontend 下初始化 React + TypeScript 前端工程（Vite 脚手架已建好），并配置：
1. 依赖：antd、@ant-design/icons、plotly.js-dist-min、axios、zustand、
   @microsoft/fetch-event-source、react-router-dom、dayjs；开发依赖：tailwindcss 3 + postcss + autoprefixer；
2. Vite 开发服务器：端口 5173，`/api` 代理到 http://127.0.0.1:8000（避免开发期 CORS）；
3. Tailwind 关闭 preflight（corePlugins.preflight = false），避免重置 AntD 样式；
4. 目录骨架：src/{layouts,pages,stores,api,theme,utils,styles}；
5. 先只建 5 个路由占位页（/login、/、/reports、/eval、/profile），不做业务逻辑。
约束：不要引入 react-plotly.js（年久失修、类型基于 React 17）；图表后续自封装容器。
```

**验证方法：**

```powershell
cd e:\power_report_agent\01.code\frontend
# 依赖装齐（显示版本即成功）
npm ls antd tailwindcss plotly.js-dist-min zustand @microsoft/fetch-event-source --depth=0
# 类型检查零错误
npx tsc -b --noEmit
# 开发服务能起（另开窗口，或看浏览器）
npm run dev
# 浏览器访问 http://127.0.0.1:5173 → 应看到登录页（占位）
```

**成功标志：**

- `npm ls` 显示六个依赖都有版本号（无 `missing` / `UNMET`）
- `npx tsc -b --noEmit` **无输出**（零类型错误）
- `npm run dev` 起得来，浏览器打开 5173 能看到页面（不是白屏、不是 404）

### 步骤2：主题系统与 API/SSE 封装

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\frontend
New-Item -ItemType File -Force src\theme\tokens.ts, src\api\client.ts, src\api\sse.ts, src\api\token.ts
```

**提示词（可直接复制）：**

```text
请实现前端"主题系统 + 网络层"三件事：
1. 主题：把 theme/tokens.ts 的设计令牌落成 CSS 变量（styles/index.css 的 --c-* ），
   并同步给 AntD 的 ConfigProvider token；主题切换写 data-theme="light|dark" 到 <html>；
2. axios 客户端（api/client.ts）：baseURL=/api/v1；请求拦截加 Bearer token；
   响应拦截统一解包络 { code, message, data }，code!==0 时抛业务错误（含 error_code 与 hint）；
   401 时清 token → Toast 提示 → 跳登录页，并**保留用户未发送的输入**（存 localStorage zdxb.draft）；
3. SSE 封装（api/sse.ts）：用 @microsoft/fetch-event-source；
   **不自动重连**（分析是有副作用的写操作，重连会重复执行）；
   暴露 AbortController 供用户中断；按 event: 名分发到回调（status/slot/clarify/code/
   table/chart/narrative/meta/error/done）。
要求：类型完整，不要用 any。
```

**验证方法：**

```powershell
cd e:\power_report_agent\01.code\frontend
npx tsc -b --noEmit
# 预期输出: 无输出（零错误）

# 令牌与 CSS 变量对应
Select-String -Path src\styles\index.css -Pattern "--c-peak|--c-valley|--c-anomaly"
# 预期输出: 命中若干行

# 401 行为：手动把 localStorage 里的 token 改成非法值，再点任意请求
# 预期输出: 跳回登录页 + Toast 提示 + 输入框内容仍在（草稿保留）
```

**成功标志：**

- `tokens.ts` 的色值与 `index.css` 的 `--c-*` 变量**逐条对应**，切换 `data-theme` 后整站变色
- axios 拦截器能把 `{code,message,data}` 解包，业务错误能拿到 `error_code` 与 `hint`
- SSE 封装**不含重连逻辑**，但可被 `AbortController` 中断（中断后后端停止生成）

### 步骤3：登录/注册页

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\frontend
# 后端需在跑：e:\power_report_agent\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

**提示词（可直接复制）：**

```text
请实现登录/注册页（pages/Login/index.tsx）：
1. 表单校验与后端一致：用户名 ^[A-Za-z0-9_]{4,20}$；密码 8–64 位且必须同时含字母与数字；
2. 调 POST /auth/login，失败按 error_code 给出针对性文案（E_CREDENTIALS 用户名或密码错误、
   E_AUTH_LOCKED 带锁定剩余秒数、E_ACCOUNT_DISABLED 账号已停用）；
3. 登录成功存 token 与用户信息，跳转到 ?next= 指定的页面（默认首页）；
4. 注册成功后**自动登录**（复用同一 token 落地逻辑），不再让用户输第二遍；
5. 页面风格与全站一致（用设计令牌，不写死颜色）。
```

**验证方法：**

```powershell
# 后端接口连通（应返回 code=0 与 token）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
  -ContentType "application/json" -Body '{"username":"demo_ui","password":"demo12345"}'
# 预期输出: 含 data.token
```

**成功标志：**

- 注册新账号 → **自动登录**并落到工作台（不需要再登录一次）
- 故意输错密码 5 次 → 出现锁定提示并带剩余秒数（`E_AUTH_LOCKED`）
- 用 `?next=/eval` 打开登录页，登录成功后落到**评测中心**（而不是首页）
- 表单校验在前端就能拦住非法用户名/弱密码（不发请求）

### 步骤4：对话工作台（本实训核心）

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\frontend
# 先确认后端 SSE 契约（10 个事件）
Select-String -Path ..\backend\app\services\analysis_service.py -Pattern '"event": "' | Select-Object -First 12
```

**提示词（可直接复制）：**

```text
请实现对话工作台（pages/Workbench/，三栏：会话列表 240 / 消息流 / 结果面板 408）：
1. 消息流：用户气泡 + 助手卡片（阶段时间线 → 代码块 → 结果表 → 图表 → 解读 → 关键数值 → 异常）；
   阶段时间线 5 阶段（解析问题 / 生成 SQL 或代码 / 沙箱执行 / 生成图表 / 生成解读），
   从 SSE 的 status 事件推进；
2. 条件芯片（SlotBar）：渲染后端 slot 的 8 个字段（time_range/granularity/metric/dimension/
   filters/compare/intent/chart_hint）；支持点 × 清除、点芯片弹气泡编辑后重算（调 chat 带 slot_patch）；
   多于 3 条时折叠为 "+N"；
3. 结果面板：图表（自封装 Plotly 容器，主题注入 + 支持切换图型走 POST /charts/rerender）
   与表格联动——点图表数据点定位表格行，点表格行高亮图表点；实现用 figure 的 customdata 带行号；
4. 澄清卡 / 拒答卡 / 错误块三种卡片（样式与语义见 UIUX 9.1）；
5. 刷新回放：消息带 record_code，刷新后按记录重拉结果回填右栏与图表（从 layout.meta 恢复口径标记）。
要求：SSE 事件名以接口文档为准，不要自己发明事件；不要在前端写死任何颜色（用令牌）。
```

**验证方法：**

```powershell
cd e:\power_report_agent\01.code\frontend
npx tsc -b --noEmit
# 预期输出: 无输出

# 端到端：提问 → 出图 → 联动（需前后端都在跑）
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs
# 预期输出: 逐条 PASS，末尾"全部通过"（共 32 条断言）
```

**成功标志：**

- 提问「全期峰段用电量占总用电量的比例是多少？」→ 助手卡片按 5 阶段推进，右栏出图 + 出表
- 点图表数据点 → 表格定位到对应行；点表格行 → 图表高亮点（联动依赖 `customdata`，不是猜行序）
- 提问「用电量怎么样」（缺指标）→ 出现**澄清卡**（主色描边），点选项后**原地继续执行**并出结果
- 刷新页面 → 右栏图表与表格**自动回放**（靠 `record_code` 重拉），不是空白
- 提问中含 `DROP TABLE` → 出现**安全拦截卡**（红描边、无「重试」按钮）

### 步骤5：报表管理 / 评测中心 / 个人中心三页

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\frontend
# 三页的接口都在后端（39 条端点）：
#   报表：/reports（列表/上传/确认/删除/激活/预览/质量/字典）
#   评测：/eval/questions、POST /eval/runs、/eval/runs/{code}、报告下载
#   个人：/auth/me、会话、分析记录、改密/改资料/清空记录/删账号
```

**提示词（可直接复制）：**

```text
请实现剩余三页：
1. 报表管理（pages/Reports/）：① 上传两步向导（选择文件 → 预览与质量 → 确认入库），
   解析中显示已耗时；重复文件（is_duplicate）给 Toast 并定位到已有报表；
   ② 质量面板（dataset + checks）与 Schema 数据字典抽屉（structured.columns + 完整注入文本）；
   ③ 报表列表支持切换活跃报表与删除（预置报表按钮置灰，由 is_deletable 驱动）；
2. 评测中心（pages/Eval/）：① 发起面板（选 impl：real 自检/真实模型、题量、题号）；
   ② 运行中如实显示"运行中 · 共 N 题 + 已耗时"，**不编造 x/N 与实时正确率**；
   ③ 完成后出指标卡（分类正确率 / 结论命中率 / 延迟 / 归因）+ 逐题明细表 + 报告下载（Markdown / PDF）；
3. 个人中心（pages/Profile/）：账号信息、统计（会话/报表/记录数 + 近 7 天柱状图）、
   分析记录列表与详情抽屉（含逐阶段模型调用明细、可重跑）、危险区（清空历史记录、删除账号，
   危险操作必须有二次确认弹窗）。
要求：缺接口的功能要**置灰并写明"后续版本"**，不要伪造入口。
```

**验证方法：**

```powershell
cd e:\power_report_agent\01.code\frontend
npx tsc -b --noEmit && npm run build
# 预期输出: 类型零错误 + 构建通过（plotly 独立 chunk）

# 三页截图（需前后端都在跑；脚本产出 8 张）
cd e:\power_report_agent
node 01.code\tools\ui_shot.mjs
Get-ChildItem 02.data\tmp\ui_shots\*.png 2>$null | Select-Object Name
# 预期输出: login / workbench-empty / workbench-result / workbench-dark /
#          reports / eval / eval-metrics / profile
```

**成功标志：**

- 上传一个小 CSV：三步走完，列表出现该报表且能切换为活跃报表
- 评测中心发起 `selfcheck` 批次（不花 token）→ 完成后指标卡与逐题表有数据，报告可下载
- 个人中心能看到记录列表，点开详情能看到三阶段模型调用明细，点重跑能生成新记录
- 危险区操作（清空历史 / 删账号）**弹二次确认**，确认后生效

### 步骤6：体验细节与回归固化

**操作命令：**

```powershell
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs     # 32 条端到端断言
node 01.code\tools\ui_shot.mjs           # 8 张页面截图
```

**提示词（可直接复制）：**

```text
请做一轮"体验细节 + 回归固化"：
1. 细节：顶栏全局导航（Logo 回首页 + 四页）、三栏等高对齐、窄屏右栏进抽屉
   （输入区上方给「查看图表与表格」入口）、结果面板折叠状态记忆；
2. 回归：把我们反馈过的每个交互缺陷，各写一条可重跑断言到 tools/ui_flow_check.mjs
   （无头 Edge + CDP），断言要**与运行顺序无关**（脚本开头先做状态归一）；
3. 两个 UI 脚本必须**串行**：都用同一个账号建/删会话与跑评测，并行会互相打乱计数断言
   ——用锁文件强制串行，第二个启动者直接退出并提示。
要求：断言名写清"验的是哪条反馈"，失败时能一眼看出哪一步坏了。
```

**验证方法：**

```powershell
cd e:\power_report_agent
# 断言全过（脚本会打印每条的 PASS/FAIL）
node 01.code\tools\ui_flow_check.mjs
# 预期输出: 32 条 PASS，末尾"全部通过"

# 两个脚本同时启动 → 第二个应直接退出并提示"必须串行"
# 预期输出: "检测到 ui_shot.mjs 正在运行……本次直接退出"
```

**成功标志：**

- `ui_flow_check.mjs` 输出 **32 条 PASS**（含空态入口、重复建会话、删除同步、字典跟随、退出登录回首页等）
- `ui_shot.mjs` 产出 **8 张**截图，暗色那张图表底色为深色（主题注入生效）
- 两个脚本并行启动时，第二个**如实退出**而不是给出假失败

## 四、验证清单

完成所有步骤后，请确认以下验证项：

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 依赖安装 | `npm ls antd plotly.js-dist-min --depth=0` | 显示版本 |
| 类型检查 | `npx tsc -b --noEmit` | 无输出（零错误） |
| 生产构建 | `npm run build` | 通过（plotly 独立 chunk） |
| 开发服务 | 浏览器打开 `http://127.0.0.1:5173` | 登录页正常 |
| 登录闭环 | 注册 / 登录 | 落到工作台（`?next=` 生效） |
| 问答出图 | 提问示例问题 | 右栏出图 + 表格 + 解读 |
| 图表联动 | 点图表数据点 / 点表格行 | 双向定位正确 |
| 澄清回路 | 提问「用电量怎么样」 | 出现澄清卡并可回填继续 |
| 安全拦截 | 提问含 `DROP TABLE` | 红色拦截卡且**无重试按钮** |
| 刷新回放 | 出结果后刷新页面 | 右栏结果自动恢复 |
| 上传闭环 | 上传小 CSV | 三步走完并出现在列表 |
| 评测闭环 | 发起 selfcheck 批次 | 出指标卡与逐题明细 |
| 端到端回归 | `node 01.code\tools\ui_flow_check.mjs` | 32 条全部 PASS |
| 截图产出 | `node 01.code\tools\ui_shot.mjs` | 8 张（含暗色） |

## 五、故障排除

### 问题1：页面请求 404 / CORS 报错

**现象：** 前端调接口报 `404 Not Found` 或浏览器控制台出现 CORS 拦截。

**原因：** Vite 开发服务器的 `/api` 代理没配，或后端的 CORS 未放行 `http://127.0.0.1:5173`。

**解决方案：** 二者任一即可，推荐用代理（开发期同源，最省事）。

```powershell
Select-String -Path e:\power_report_agent\01.code\frontend\vite.config.ts -Pattern "proxy|8000"
# 预期输出: 命中 server.proxy 配置（/api → http://127.0.0.1:8000）
```

### 问题2：SSE 收不到推送（一直停在第 1 阶段）

**现象：** 提问后阶段时间线卡在"解析问题"，没有 `table` / `chart` 事件。

**原因：** ① 用了原生 `EventSource`（不支持 POST 与鉴权头）；② 响应被中间层缓冲
（`text/event-stream` 被 gzip 或反向代理攒批）；③ 后端报错但前端没监听 `error` 事件。

**解决方案：** 用 `@microsoft/fetch-event-source`；**HTTP 已是 200 也要监听 `error` 事件**
（业务失败是通过 SSE 事件下发的，不是 HTTP 状态码）；开发期不要给 SSE 响应加压缩。

```powershell
# 直接看后端是否在推事件（用项目自带的 SSE 探针，退出码 0/1 可进 CI）
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
# 预期输出: 各事件到达时间与首字延迟
```

### 问题3：构建后首屏慢 / 白屏时间长

**现象：** `npm run build` 通过，但首屏加载要好几秒。

**原因：** Plotly 体积约 4.6MB（`plotly.js-dist-min`），若打进主 chunk 会拖慢首屏。

**解决方案：** 让其独立 chunk；需要更快时改成动态 `import()` 延迟加载图表组件。

```powershell
cd e:\power_report_agent\01.code\frontend
npm run build
Get-ChildItem dist\assets\*.js | Sort-Object Length -Descending | Select-Object -First 3 Name, Length
# 预期输出: 最大的 chunk 是 plotly（独立文件，不在入口 chunk 里）
```

### 问题4：AntD 样式被 Tailwind 覆盖（按钮/表单变形）

**现象：** 引入 Tailwind 后，AntD 的按钮、输入框、表格样式被重置。

**原因：** Tailwind 的 `preflight`（基础样式重置）与 AntD 冲突。

**解决方案：** 关闭 `preflight`，并让 AntD 使用 CSS 变量模式与设计令牌对齐。

```powershell
Select-String -Path e:\power_report_agent\01.code\frontend\tailwind.config.js -Pattern "preflight"
# 预期输出: corePlugins.preflight = false
```

### 问题5：刷新后右栏空白（图表与表格都没了）

**现象：** 出图后刷新页面，右栏是空的，历史消息里的结果也没了。

**原因：** 助手消息没有携带 `record_code`，刷新后前端不知道去哪取结果。

**解决方案：** 消息必须带 `record_code`，据此重拉 `GET /analysis/records/{code}` 回放结果，
并按 `layout.meta` 恢复口径标记（这套约定是前后端一起定的，见接口文档）。

```powershell
Select-String -Path e:\power_report_agent\01.code\frontend\src\pages\Workbench -Pattern "record_code" -Recurse | Select-Object -First 5
# 预期输出: 命中（消息模型里带 record_code）
```

### 问题6：窄屏看不到结果，以为"没查出来"

**现象：** 窗口缩窄后右栏消失，用户以为查询失败。

**原因：** 右栏按响应式规则收进了抽屉，但入口不明显（见实训2 问题4）。

**解决方案：** 输入区上方固定「查看图表与表格」按钮；结果生成时给一次高亮指引。

```powershell
Select-String -Path e:\power_report_agent\01.code\frontend\src\pages\Workbench -Pattern "查看图表与表格" -Recurse
# 预期输出: 命中（窄屏抽屉入口）
```

## 六、参考代码

### 完整目录结构

```
01.code/frontend/
├── package.json                    # 依赖与脚本（dev / build / preview / typecheck）
├── vite.config.ts                  # 端口 5173 + /api 代理到后端 8000
├── tailwind.config.js              # preflight=false，与 AntD 共存
└── src/
    ├── App.tsx / main.tsx
    ├── layouts/{MainLayout,TopBar,PageHeader}.tsx
    ├── pages/
    │   ├── Login/index.tsx
    │   ├── Workbench/              # 14 个组件（见 二.2）
    │   ├── Reports/{index,UploadWizard,PreviewDrawer,QualityPanel}.tsx
    │   ├── Eval/{index,StartPanel,MetricsPanel,RunsList,RunQuestionsTable}.tsx
    │   └── Profile/{index,AccountDialogs,RecordDetailDrawer}.tsx
    ├── stores/{auth,chat,reports,theme}.ts
    ├── api/{client,sse,token,auth,conversations,reports,analysis,charts,eval,system,types,errors}.ts
    ├── theme/tokens.ts
    ├── utils/{format,toast}.ts
    └── styles/index.css
```

### 常用命令

```powershell
cd e:\power_report_agent\01.code\frontend
npm install
npm run dev            # 开发（默认 5173）
npm run build          # 生产构建（tsc + vite build）
npm run typecheck      # 仅类型检查（等价 npx tsc -b --noEmit）

# 端到端回归与截图（需前后端都在跑，且两个脚本必须串行）
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs      # 32 条断言
node 01.code\tools\ui_shot.mjs            # 8 张截图

# SSE 探针（看事件到达时间与首字延迟）
.venv\Scripts\python.exe 01.code\tools\sse_probe.py
```

### 关键配置说明

| 参数 | 取值 / 说明 |
|--------|-----------|
| 技术栈 | Vite 6 / React 18.3 / TypeScript 5.7 / Ant Design 5.22 / Tailwind 3.4（关 preflight）/ zustand 5 / plotly.js-dist-min 2.35 |
| 主题 | CSS 变量 `--c-*` + `data-theme="light\|dark"`；图表颜色由前端注入（后端只写语义色） |
| SSE 客户端 | `@microsoft/fetch-event-source`，**不自动重连**，`AbortController` 可中断 |
| 登录态 | localStorage（`zdxb.token`）+ axios 401 拦截 + 草稿保留（`zdxb.draft`） |
| 图表容器 | 自封装 `ResultChart.tsx`（`Plotly.react`），**不用 react-plotly.js** |
| 联动实现 | figure `customdata` 携带行号（点图定位表格、点表格高亮图表） |
| 回归 | `ui_flow_check.mjs` **32 条断言**；`ui_shot.mjs` **8 张截图**；两者用锁文件强制串行 |
