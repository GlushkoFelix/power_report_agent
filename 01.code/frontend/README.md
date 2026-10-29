# 智电析报 · Web 前端

基于 LLM 的能源电力报表智能分析 Agent 的浏览器端。
已完成**两批**：第一批 = 工程骨架 + 登录/注册 + **对话工作台**（主链路）；
第二批 = **报表管理 / 评测中心 / 个人中心**三页（接口全接入，未改后端契约）。
剩余：前端收尾小批（会话列表图标条、芯片"＋N"折叠、图片+表格联合导出）与个人中心缺接口的四项功能（详见末尾）。

设计依据：[`04.实训指导书/AI共创过程文档/智电析报_UIUX设计文档.md`](../../04.实训指导书/AI共创过程文档/智电析报_UIUX设计文档.md)
（第 4 章视觉规范 / 5 章页面原型 / 6 章条件芯片 / 7 章图表联动 / 8 章流式状态 / 9 章错误呈现 / 14 章落地记录）。

---

## 一、快速开始

```bash
# ① 先启动后端（另开一个终端；默认 127.0.0.1:8000）
cd 01.code/backend
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ② 安装前端依赖（首次）
cd 01.code/frontend
npm install

# ③ 启动开发服务器（默认 http://127.0.0.1:5173）
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>，**首次使用先注册**（用户名 4–20 位字母/数字/下划线，
密码 ≥8 位且含字母与数字）；注册成功即自动登录。

> 前端把 `/api` 代理到后端（`vite.config.ts` 的 `server.proxy`），因此**同源调用**、
> 不依赖后端 CORS 配置；后端地址可用环境变量覆盖：`VITE_BACKEND=http://127.0.0.1:8000 npm run dev`。

### 没有模型 Key 也能跑

后端未配置 `LLM_API_KEY` 时自动使用**离线脚本模型**（`ScriptedLLM`，见 `/api/v1/system/health`
的 `llm.impl=scripted`）：整条链路（解析 → 生成 → 守卫 → 执行 → 图表 → 解读）照常跑通，
只是结果来自预置规则、**不代表真实模型能力**。此时建议提问与预置报表口径匹配的问题，例如：

| 提问 | 期望结果 |
| --- | --- |
| 峰段用电占比多少 | 三个指标列 → **柱状图**（峰/平/谷语义色）+ 表格 |
| 有哪些用电异常日 | 异常日明细（表格带「异常」标签） |
| 按月看用电趋势 | 折线图（在月报上为按月，在日报上自动按日） |
| 厨房用电占比 | 按用途拆分（用途色板） |

---

## 二、目录结构

```
src/
├── api/                 # 接口层（唯一事实源：后端 app/schemas/*.py，逐字段对齐）
│   ├── client.ts        # axios 实例：包络解包、Bearer 注入、401 拦截
│   ├── sse.ts           # fetchEventSource 封装（POST + 鉴权 + 不自动重连）
│   ├── types.ts         # 接口类型（后端响应为 dict，OpenAPI 无 schema → 手写）
│   └── auth/conversations/reports/analysis/charts/system.ts
├── stores/              # zustand：auth（登录态）/ reports（报表）/ chat（SSE 状态机）/ theme
├── theme/tokens.ts      # 语义色 + 用途色板 + Plotly 主题注入（与 CSS 变量一一对应）
├── styles/index.css     # 设计令牌（亮/暗两套 CSS 变量）+ 少量全局类
├── layouts/             # MainLayout（顶部栏 + 内容区）、TopBar（报表选择器/主题/用户菜单）
├── pages/
│   ├── Login/           # 登录 / 注册
│   ├── Workbench/       # 对话工作台（第一批重点）
│   │   ├── ConversationList.tsx  # 左栏：会话列表（重命名/置顶/删除）
│   │   ├── SlotBar.tsx           # 条件芯片栏（编辑气泡 + slot_patch 提交）
│   │   ├── StageTimeline.tsx     # 5 阶段时间线
│   │   ├── MessageStream.tsx     # 对话流（助手卡片装配）
│   │   ├── CodeBlock.tsx         # SQL / pandas 折叠块（报表/个人中心复用）
│   │   ├── StateCards.tsx        # 错误块 / 拒答卡 / 澄清卡
│   │   ├── ResultChart.tsx       # Plotly 容器（自封装）+ 导出 PNG
│   │   ├── ResultTable.tsx       # 结果表（标记列 + 与图表联动；预览/详情复用）
│   │   ├── ResultPanel.tsx       # 右栏（图表 + 表格 + 执行信息）
│   │   ├── ExecInfo.tsx          # 执行信息（路径/重试/耗时/模型/tokens）
│   │   ├── SchemaDrawer.tsx      # 字段字典抽屉（报表管理复用）
│   │   └── Composer.tsx          # 输入区 + 8 个示例问题
│   ├── Reports/         # 报表管理（第二批）
│   │   ├── index.tsx             # 列表 + 活跃报表高亮 + 删除（is_deletable 驱动）
│   │   ├── UploadWizard.tsx      # 上传三步向导（解析 → 预览质量 → 确认入库）
│   │   ├── QualityPanel.tsx      # 质量体检（指标卡 + A1–A5 校验 + 明细）
│   │   └── PreviewDrawer.tsx     # 数据预览抽屉（复用 ResultTable）
│   ├── Eval/            # 评测中心（第二批）
│   │   ├── index.tsx             # 发起 + 批次列表 + 详情（轮询 2.5s，如实降级）
│   │   ├── StartPanel.tsx        # 模式 real/selfcheck、题量、只读模型信息
│   │   ├── MetricsPanel.tsx      # 指标卡 / 分类正确率 / 判分档 / 五类归因
│   │   ├── RunQuestionsTable.tsx # 逐题结果 + 「生成 SQL vs 标准 SQL」对照
│   │   └── RunsList.tsx          # 历史批次 + 报告导出（blob 下载）
│   └── Profile/         # 个人中心（第二批）
│       ├── index.tsx             # 账号只读 + 统计 + 记录管理 + 危险区（如实置灰）
│       └── RecordDetailDrawer.tsx# 记录详情（SQL/结果表/图表/解读/llm_calls）
└── utils/               # 格式化（数字/时间/时长/芯片文案）与 Toast 出口
```

---

## 三、三条实现约定（改代码前请先读）

1. **颜色只走 CSS 变量**（`styles/index.css`），Tailwind 的 `colors` 已映射到同一批变量 →
   暗色模式不需要写 `dark:` 前缀；Plotly 不认变量，故 `theme/tokens.ts` 保留一份对应的十六进制色板。
2. **SSE 不自动重连**：失败一律渲染错误块（`error` 之后的 `done` 只用于解除 loading 并取 `record_id`）；
   用户点「停止」用 `AbortController` 中断，已生成内容保留。
3. **接口类型手写**：后端端点返回 `dict`，OpenAPI 里响应体是 `additionalProperties: true`，
   因此 `api/types.ts` 与 `app/schemas/*.py` 必须同步改（后端改字段 → 前端同步字段）。

---

## 四、验收命令

```bash
npx tsc -b --noEmit     # 类型检查（应零错误）
npm run build           # 生产构建（plotly / antd 各自独立 chunk）

# 页面截图验收（无头 Edge + CDP 自动化，前后端都在跑的前提下；从仓库根目录执行）
node 01.code/tools/ui_shot.mjs
# → 02.data/tmp/ui_shots/{login,workbench-empty,workbench-result,workbench-dark,
#                          reports,eval,eval-metrics,profile}.png
#   注：评测页会真跑一次 3 题自检（gold 回放，不消耗 token），截图里能看到指标卡与逐题结果

# 交互流程自检（20 条断言：第一批 5 类历史缺陷 + 第二批三页关键交互；会真的建/删会话）
node 01.code/tools/ui_flow_check.mjs
```

两个脚本都在 Windows 保留端口之外**随机取调试端口**，可并行运行互不干扰。

---

## 五、后续计划（本工程尚未做的部分）

| 项 | 说明 |
| --- | --- |
| 前端收尾小批 | 会话列表自动折叠为图标条（<1366px）、条件芯片"＋N"溢出折叠、图表"图片+表格"联合导出 |
| 个人中心缺接口的四项 | 后端暂无 `PATCH /auth/me`（改资料）、`POST /auth/password`（改密）、`POST /profile/clear-records`（清空记录）、`DELETE /auth/me`（删账号）→ 页面已置灰并注明"后续版本"，不伪造入口；补齐后端后再接线 |
| 真实评测 | 页面已支持 `impl=real`，但需配置 `LLM_API_KEY`；未配置时接口返回 400 并提示改用自检回放 |
| 评测进度 | 后端无逐题进度端点与取消端点，页面如实显示"运行中 · 共 N 题"，"中断"仅为停止客户端轮询 |
