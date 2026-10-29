# 百炼API密钥申请与环境配置语料库

**主题：** 阿里云百炼（DashScope）接入、`.env` 配置与本地环境要求
**适用范围：** 环境搭建者、部署人员
**版本：** v1.0

---

## Q: 申请百炼 API 密钥的完整步骤是什么？

**A:** 六步（详细操作见《实训附录A-申请百炼API密钥》）：
① 注册/登录阿里云并完成**实名认证**；
② 在百炼控制台**开通服务**；
③ **创建 API-KEY**（`sk-` 开头）；
④ 写入项目 `01.code/backend/.env`（见下一条的变量清单）；
⑤ **重启后端**并验证"确实读到了 Key"（看 `/system/health` 的 `llm.configured`）；
⑥ 跑一次**真实问答**，确认端到端可用。

**标签：** 百炼申请, 实名认证, API-KEY, 接入步骤

---

## Q: 项目的 `.env` 需要配置哪些变量？

**A:** 分组如下：
- **LLM**：`LLM_API_KEY`（兼容旧名 `DASHSCOPE_API_KEY`）、`LLM_BASE_URL`（OpenAI 兼容端点）、
  `LLM_MODEL_MAIN`、`LLM_MODEL_LIGHT`、`LLM_PROVIDER_LABEL`（仅展示用）、
  `LLM_TEMPERATURE`（评测固定 0）、`LLM_MAX_TOKENS`、`LLM_ENABLE_THINKING`；
- **鉴权**：`JWT_SECRET_KEY`（**缺失则启动报错**，刻意设计）、`JWT_ALGORITHM`、`JWT_EXPIRE_HOURS`；
- **应用**：`APP_HOST` / `APP_PORT` / `DEBUG`；
- **数据库**：`REPORTS_DB_PATH` / `POWER_AGENT_DB_PATH`（相对**项目根**解析，不依赖启动目录）。

**标签：** .env 配置, 变量清单, JWT_SECRET_KEY, 库路径

---

## Q: 为什么 `JWT_SECRET_KEY` 缺失要直接启动报错？

**A:** 因为如果每次重启都临时生成一个密钥，**之前签发的所有 token 会立刻失效**，
表现为"用户莫名其妙被踢下线"，而且很难排查。
所以设计成**缺它就跑不起来**，并在错误信息里打印生成命令：
`python -c "import secrets;print(secrets.token_urlsafe(48))"`——
把"隐性坑"变成"启动时的显式报错"。

**标签：** JWT 密钥, 启动报错, 刻意设计, 避免隐性坑

---

## Q: 怎么确认后端"确实读到了 Key"？

**A:** 调 `GET /api/v1/system/health` 看 `data.llm`：
- `configured`：是否已配置可用 Key；
- `provider`：厂商标识（如 `aliyun-maas`）；
- `protocol`：协议（本项目恒为 `openai-compatible`）；
- `key_source`：Key 来自哪个环境变量（换厂商排错时很有用）。
等价地，也可以看批次的 `model_name` 是否为真实模型名。
**注意**：模板占位值（如 `sk-xxxxxx`）会被视为"**未配置**"，
这时链路回落离线脚本模型（`ScriptedLLM`），可跑通但不代表模型能力。

**标签：** 健康检查, configured, key_source, 占位值

---

## Q: 本地运行需要什么环境？

**A:** 本项目面向 **Windows 11** 开发与部署：
- **Python 3.13.12**（要求 3.12+）；虚拟环境是**项目根的统一 `.venv`**（不另建 `backend/.venv`，
  因为 pandas 路径在 API 进程内运行）；
- **Node.js v26.7.0 / npm 11.19.0**（前端 Vite 6）；
- 两个 SQLite 库由数据脚本生成（`02.data/reports.db` 约 408MB、
  `02.data/power_agent.db` 约 200KB）。
启动前建议设置 `$env:PYTHONIOENCODING = "utf-8"`，避免中文日志在控制台乱码。

**标签：** 环境要求, Python 3.13, Node, 统一 venv, 编码

---

## Q: 后端和前端分别是哪条命令启动的？

**A:** 两个**独立进程**，各占一个终端窗口：
- **后端**（端口 8000，工作目录必须是 `01.code\backend`）：
  `..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  （局域网访问用 `--host 0.0.0.0`）；
- **前端**（端口 5173，工作目录 `01.code\frontend`）：`npm run dev`。
前端通过 Vite 的 `/api` 代理访问后端，所以**后端没起时表现为"页面能开，一提问就失败"**。
注意：前端 `strictPort: false`（5173 被占会自动换 5174），
而后端 8000 被占会**直接启动失败**（`WinError 10048`）。

**标签：** 启动命令, 两进程, 代理, 端口占用

---

## Q: 换模型/换厂商要改什么？

**A:** **只改 `.env`，代码零改动**：改 `LLM_BASE_URL`、`LLM_API_KEY`、
`LLM_MODEL_MAIN` / `LLM_MODEL_LIGHT`，然后**重启后端**。
支持任何 OpenAI 兼容端点（百炼 / DeepSeek / Moonshot / 智谱 / 本地 vLLM·Ollama）。
换厂商后：
① 先跑一次健康检查确认 `configured=true`、`provider` 已变；
② **必须重新量化"单条记录成本"**再跑评测——不同模型的 token 成本可能差 10% 以上，
本项目就因此提前发现"某族模型单份 1M 额度装不下完整 120 题"。

**标签：** 换厂商, 只改 .env, 重启后端, 重新量化成本

---

## Q: 密钥安全要注意什么？

**A:** 四条：
① `.env` 必须写进 `.gitignore`，**严禁提交到版本库**；
② 页面上与日志里**不展示 token 与密钥**（日志脱敏由后端保证），
错误信息也不输出文件绝对路径；
③ 用工作空间级密钥时注意它**只对对应端点有效**（换端点必须换 Key）；
④ 若怀疑泄露，立即在控制台重置 Key 并更新 `.env`。
另外：仓库里只保留 `.env.example`（占位值），新人照它复制成 `.env` 再填真值。

**标签：** 密钥安全, .gitignore, 日志脱敏, .env.example

---

## Q: 调用被限流了怎么办？

**A:** 典型表现是批量评测时部分题报 `E_LLM`，重试后恢复。
处理办法：① 降低评测并发度（本项目是逐题串行，不需要额外并发）；
② 增大重试退避间隔；③ 在控制台确认该模型的 QPS/并发限制并按它设置评测节奏；
④ 把限流信息写进运维记录，避免下次批量评测再踩。
另可先跑 `selfcheck` 批（零 token）验证链路，把真实额度留给关键批次。

**标签：** 限流, QPS, 评测并发, 退避重试

---

## Q: 没额度/没 Key 的时候怎么演示？

**A:** 用**自检模式**：评测中心选 `selfcheck`（逐题回放题库 gold），
链路、判分、指标、报告全都能走通，且**零 token 消耗**。
两个硬约束：
① 报告与指标卡会**置顶标注"不代表模型能力"**，引用时必须一并说明；
② 真实评测（`impl=real`）在未配置 Key 时**返回 400 并给 hint**，
**不会静默回落**成自检——避免"以为是模型成绩、其实是回放 gold"。
若是额度耗尽，可在控制台充值为目标模型留出足够额度后再跑真实批次。

**标签：** 零额度演示, selfcheck, 不代表模型能力, 不静默回落
