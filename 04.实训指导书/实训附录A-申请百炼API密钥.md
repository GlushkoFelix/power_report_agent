# 实训附录A 申请阿里云百炼（DashScope）API 密钥

> 本附录对应「实训5 后端开发」步骤7（多厂商 LLM 适配）。项目代码只依赖 **OpenAI 兼容协议**，因此换成 DeepSeek / Moonshot / 智谱 / 本地 Ollama 的流程与本文类似，只需替换端点与模型名。

## 一、实训目标

1. 掌握阿里云百炼（DashScope）控制台的开通与 API-KEY 创建流程
2. 学会 API 密钥的安全管理（不硬编码、不入库、写 `.env` 并加入 `.gitignore`）
3. 能够把密钥接入本项目并**验证后端确实读到了它**（而不是以为配好了）

## 二、实训内容

### 1. 背景知识

- **为什么需要 Key**：本项目默认使用离线脚本模型（`ScriptedLLM`）保证链路可跑通，但**真实 SQL 正确率只能靠真实模型验证**；未配置 Key 时 `/system/health` 会如实回报 `llm.configured=false`。
- **OpenAI 兼容协议**：本项目只用一种协议 `POST {LLM_BASE_URL}/chat/completions` + `Authorization: Bearer <key>`，因此换厂商**只改 `.env`、代码零改动**。
- **模板占位值防护**：`.env.example` 里的 `sk-xxxxxx` / `请填入` 会被判定为"未配置"并自动回落离线模型，避免"复制模板忘改 Key"导致全链路 `E_LLM`。

### 2. 项目结构（本附录涉及的文件）

```
01.code/backend/
├── .env                 # 密钥与模型配置（**不入版本库**）
└── .env.example         # 模板（含四厂商示例）
```

## 三、实训步骤

### 步骤1：注册/登录阿里云并完成实名认证

**操作步骤：**

1. 打开阿里云官网并登录：https://www.aliyun.com/
2. 完成个人实名认证（未认证无法开通大模型服务）

**验证方法：**

```powershell
# 手动验证：浏览器已登录阿里云控制台，且实名认证状态为"已认证"
# 预期结果：控制台右上角显示账号且无"未实名"提示
```

**成功标志：**

- 成功登录阿里云控制台
- 实名认证通过

### 步骤2：开通百炼（DashScope）服务

**操作步骤：**

1. 进入百炼控制台：https://bailian.console.aliyun.com/
2. 按引导开通服务（新用户通常有免费额度，注意查看额度与到期时间）

**验证方法：**

```powershell
# 手动验证：能进入百炼控制台的"模型服务 / API-KEY 管理"页面
# 预期结果：页面可正常访问，无"未开通"提示
```

**成功标志：**

- 百炼服务开通成功
- 可访问 API-KEY 管理页面

### 步骤3：创建 API-KEY

**操作步骤：**

1. 打开「API-KEY 管理」页面
2. 点击「创建新的 API-KEY」
3. **立即复制并妥善保存**（页面关闭后不再完整显示）

**验证方法：**

```powershell
# 手动验证：已复制形如 sk-xxxxxxxx 的密钥并保存到安全位置
# 预期结果：获取到一个可用的 API-KEY
```

**成功标志：**

- API-KEY 创建成功
- 密钥已安全保存（不要贴在聊天记录、截图或代码里）

### 步骤4：写入项目 `.env`

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend

# 若还没有 .env，从模板复制
Copy-Item .env.example .env

# 打开编辑（记事本或 IDE）
notepad .env
```

在 `.env` 中填写（**只需四项，其余保持默认**）：

```ini
# 模型服务（任意 OpenAI 兼容厂商）
LLM_API_KEY=sk-你的真实密钥
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_MAIN=qwen-max
LLM_MODEL_LIGHT=qwen-turbo
LLM_PROVIDER_LABEL=dashscope
```

**验证方法：**

```powershell
# 1) 确认 .env 已存在且键非空（不打印密钥值本身）
$env_path = "e:\power_report_agent\01.code\backend\.env"
Test-Path $env_path
(Get-Content $env_path | Select-String "^LLM_API_KEY=").Line.Length -gt 15
# 预期输出: True / True（长度大于 15 说明不是空值或占位值）

# 2) 确认 .env 已被 git 忽略（不会误提交）
Set-Location e:\power_report_agent
git check-ignore 01.code\backend\.env 2>$null
# 预期输出: 命中该路径（未初始化 git 时无输出，属正常）
```

**成功标志：**

- `.env` 存在且 `LLM_API_KEY` 为真实密钥
- 密钥不会被提交到版本库

### 步骤5：重启后端并验证"确实读到了 Key"

**操作命令：**

```powershell
cd e:\power_report_agent
# 若后端已在运行，先停掉（Ctrl+C 或结束进程），再重启
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000" `
  -WorkingDirectory "e:\power_report_agent\01.code\backend"
```

**验证方法：**

```powershell
# 关键验证：llm.configured 必须为 true
$h = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" -Method GET
$h.data.llm | ConvertTo-Json -Depth 3
# 预期输出（关键字段）:
#   configured : True
#   impl       : <你的厂商标识，如 dashscope>
#   provider   : dashscope
#   protocol   : openai-compatible
#   key_source : LLM_API_KEY
#   model_main : qwen-max
#   model_light: qwen-turbo
```

**成功标志：**

- `configured=true`，`impl` 不再是 `scripted`
- `key_source` 显示密钥来自哪个变量（换厂商排错用）

### 步骤6：跑一次真实问答，确认端到端可用

**操作命令：**

```powershell
# 登录拿 token（示例账号，按你实际注册的用户名密码替换）
$login = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/auth/login" -Method POST `
  -ContentType "application/json" -Body '{"username":"demo","password":"demo12345"}'
$token = $login.data.token

# 建会话（带上要分析的报表编号，默认活跃报表为 r_daily）
$conv = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/conversations" -Method POST `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
  -Body '{"active_report_code":"r_daily"}'
$conv.data.conversation_code
```

随后在浏览器打开 `http://127.0.0.1:5173`，用同一账号提问「2020 年 7 月总用电量是多少」，观察是否正常出 SQL、表格、图表与解读。

**验证方法：**

```powershell
# 分析记录落库检查（最近一条应为成功，且 llm_model 为真实模型名）
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'02.data\power_agent.db'); print(c.execute('select record_code,status,llm_model,elapsed_ms from analyses order by id desc limit 1').fetchone())"
# 预期输出: 形如 ('a_000123', 'success', 'qwen-max', 1234)
```

**成功标志：**

- 页面正常出图与解读（不再是离线脚本模型的固定话术）
- `analyses.llm_model` 显示真实模型名

## 四、验证清单

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| 阿里云登录 | 手动验证 | 成功登录且已实名 |
| 百炼开通 | 手动验证 | 可访问 API-KEY 管理页 |
| API-KEY 创建 | 手动验证 | 获取 `sk-` 密钥 |
| `.env` 落盘 | `Test-Path 01.code\backend\.env` | True |
| 密钥不被提交 | `git check-ignore` | 命中 `.env` |
| 后端读到 Key | `Invoke-RestMethod .../system/health` | `llm.configured=true` |
| 真实模型可用 | 提一个问题 | 正常出图且 `llm_model` 为真实模型名 |
| 额度可见 | 百炼控制台用量页 | 能查看到调用量与剩余额度 |

## 五、故障排除

### 问题1：健康检查仍显示 `configured=false`

**现象：** 已经填了 Key，但 `llm.configured` 还是 false。

**解决方案：**

```powershell
# 1) 确认变量名正确（正式名 LLM_API_KEY；旧别名 DASHSCOPE_API_KEY 亦可）
(Get-Content e:\power_report_agent\01.code\backend\.env | Select-String "^LLM_")
# 2) 确认没有把占位值留在文件里（sk-xxxxxx / 请填入 会被判为未配置）
(Get-Content e:\power_report_agent\01.code\backend\.env | Select-String "xxxxxx|请填入")
# 预期输出: 无输出
# 3) 改完 .env 必须重启后端（配置在进程启动时读取）
```

### 问题2：问答报 `E_LLM`（模型调用失败）

**现象：** 页面提示模型调用失败，记录 `error_code=E_LLM`。

**解决方案：**

```powershell
# 逐一核对三项：Key 是否有效、端点是否匹配该厂商、模型名是否可用
# 常见原因：LLM_BASE_URL 与 Key 不是同一厂商；模型名写错（以厂商控制台可用列表为准）
(Get-Content e:\power_report_agent\01.code\backend\.env | Select-String "LLM_BASE_URL|LLM_MODEL_MAIN")
```

### 问题3：报错提示"模型名不匹配该厂商"或 `response_format` 相关 400

**现象：** 少数自建/网关端点不支持 JSON 模式。

**解决方案：**

```ini
# 在 .env 中打开降级开关：被拒时自动去参重试一次
LLM_JSON_MODE_FALLBACK=true
```

### 问题4：调用限流（评测时更明显）

**现象：** 评测批次出现大量失败，错误码指向限流。

**解决方案：**

- 降低并发与批次题量（先用 `limit=20` 小样本）
- 在百炼控制台确认模型的并发上限与剩余额度
- 评测固定 `temperature=0`，不要在脚本里改温度

### 问题5：担心密钥泄露

**解决方案：**

```powershell
# 1) 确认 .env 在忽略列表中
Get-Content e:\power_report_agent\01.code\backend\.gitignore | Select-String ".env"
# 2) 若密钥曾出现在截图/文档/代码中，立即到控制台删除该 KEY 并重建
# 3) 本项目页面不提供填写/展示密钥的入口（避免"密钥落库或前端存储"的泄露面）
```

## 六、参考代码

### 关键配置文件

```
01.code/backend/.env.example      # 模板（四厂商示例，可复制为 .env）
01.code/backend/.env              # 你的真实配置（不入版本库）
```

### 常用命令

```powershell
# 查看模型与密钥状态（不打印密钥值）
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | Select-Object -ExpandProperty data |
  Select-Object -ExpandProperty llm

# 查看最近一次分析用的模型
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'02.data\power_agent.db'); print(c.execute('select record_code,status,llm_model from analyses order by id desc limit 3').fetchall())"
```

### 关键配置说明

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | 生效的密钥（优先），兼容旧名 `DASHSCOPE_API_KEY` | `sk-****` |
| `LLM_BASE_URL` | OpenAI 兼容端点（换厂商只改这里） | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL_MAIN` | 主力模型：生成 SQL/代码与解读 | `qwen-max` |
| `LLM_MODEL_LIGHT` | 轻量模型：slot 抽取等轻任务 | `qwen-turbo` |
| `LLM_PROVIDER_LABEL` | 厂商标签，**仅用于展示，不参与协议分支** | `dashscope` |
| `LLM_JSON_MODE_FALLBACK` | JSON 模式被端点拒绝时是否去参重试一次 | `true` |
