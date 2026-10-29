# 实训附录B 本地部署与局域网访问

> 本附录对应「实训6 执行测试与评测」的前置条件与交付验收。本项目面向 **Windows 11 本机演示 + 局域网访问**（不写 Dockerfile、不配 HTTPS 证书，见架构文档 ADR-010）。

## 一、实训目标

1. 掌握在 Windows 上从零部署本项目：Python/Node 环境、虚拟环境、依赖安装、数据库初始化
2. 学会两库的初始化与校验（`reports.db` 报表库 / `power_agent.db` 系统库）
3. 能够分别启动后端（FastAPI）与前端（Vite dev），并完成一次端到端验证
4. 掌握局域网访问配置：监听 `0.0.0.0`、Windows 防火墙入站规则、用内网 IP 访问
5. 学会数据备份与还原（两库文件 + 上传目录 + 评测报告）

## 二、实训内容

### 1. 背景知识

- **部署拓扑**：前端开发服务（5173）→ 后端 API（8000）→ 两库 + 文件区；局域网内其他设备用 `http://<本机内网IP>:5173` 访问
- **为什么用 `--host 0.0.0.0`**：默认 `127.0.0.1` 只允许本机访问；局域网访问需监听全部网卡
- **两库职责**：`reports.db`（报表数据，只读打开）/ `power_agent.db`（用户、会话、记录、评测，读写 + WAL）
- **配置外置**：所有路径、端口、模型配置来自 `01.code\backend\.env`，库路径以**项目根**为基准解析（与启动目录无关）

### 2. 项目结构

```
power_report_agent/
├── .venv/                       # 项目根统一虚拟环境（全项目复用）
├── 01.code/
│   ├── backend/                 # FastAPI 服务（app/ + requirements.txt + .env）
│   ├── frontend/                # React 前端（src/ + package.json）
│   ├── scripts/                 # 数据清洗与建库脚本（01/02/03/04）
│   └── tools/                   # 图表/架构图/截图/回归脚本
├── 02.data/
│   ├── reports.db               # 报表库（5 张预置表 + 分钟明细 207 万行）
│   ├── power_agent.db           # 系统库（10 张表）
│   ├── reports/ schema/ quality/ logs/ eval/ uploads/
└── 04.实训指导书/               # 文档与实训指导书
```

## 三、实训步骤

### 步骤1：环境准备（Python / Node / 浏览器）

**操作命令：**

```powershell
python --version           # 需 3.12+（本项目实测 3.13.12）
node --version             # 需 18+（本项目实测 v26）
npm --version
```

**验证方法：**

```powershell
python --version; node --version; npm --version
# 预期输出: 三行版本号，Python >= 3.12，Node >= 18
```

**成功标志：** 三条命令均输出版本号且满足下限。

### 步骤2：创建虚拟环境并安装依赖

**操作命令：**

```powershell
cd e:\power_report_agent

# 项目根统一虚拟环境（若已存在可跳过创建）
python -m venv .venv

# 后端依赖（含 pandas / plotly / sqlglot / FastAPI 等）
.\.venv\Scripts\python.exe -m pip install -r 01.code\backend\requirements.txt

# 前端依赖
cd 01.code\frontend
npm install
cd ..\..
```

**验证方法：**

```powershell
# 关键包自检（缺任何一个都会导致对应功能不可用）
.\.venv\Scripts\python.exe -m pip list | Select-String "fastapi|sqlalchemy|pandas|plotly|sqlglot|RestrictedPython|httpx|bcrypt|PyJWT|sse-starlette"
# 预期输出: 列出上述包与版本（plotly 7.0.0 / RestrictedPython 8.5 等）

Test-Path 01.code\frontend\node_modules\antd
# 预期输出: True
```

**成功标志：** 关键包全部存在；前端 `node_modules` 已生成。

### 步骤3：初始化数据库（两库 + 报表数据）

**操作命令：**

```powershell
cd e:\power_report_agent

# 1) 解析原始数据集 → 4 张报表 CSV + 质量报告（首次约 1–2 分钟）
.\.venv\Scripts\python.exe 01.code\scripts\01_build_reports.py

# 2) 生成 Schema JSON 与注入文本
.\.venv\Scripts\python.exe 01.code\scripts\02_build_schema.py

# 3) 建库并灌数（含分钟明细 207 万行，约 30 秒；--force 表示按 DDL 重建）
.\.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force

# 4) 构建评测题库（120 题，gold 对真实库反算；幂等）
.\.venv\Scripts\python.exe 01.code\scripts\04_build_eval_set.py
```

**验证方法：**

```powershell
# 两库是否存在、报表数据规模是否符合预期
Test-Path 02.data\reports.db; Test-Path 02.data\power_agent.db
# 预期输出: True / True

.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'02.data\reports.db'); print(c.execute('select count(*) from report_minute').fetchone())"
# 预期输出: (2075259,)

# 口径校验（A1 三级总量）
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'02.data\reports.db'); print(c.execute('select round(sum(consumption_kwh),4) from report_daily').fetchone())"
# 预期输出: (37283.7477,)
```

**成功标志：** 两库存在；分钟明细 2,075,259 行；三级总量 37283.7477 kWh。

### 步骤4：配置 `.env`（数据库路径 / 端口 / 模型）

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
Copy-Item .env.example .env
notepad .env
```

最少需要确认的项：

```ini
# 鉴权（必须设置，缺失则启动报错）
JWT_SECRET_KEY=<用下方命令生成>
# 模型（不配 = 用离线脚本模型，链路仍可跑通）
LLM_API_KEY=sk-你的密钥
# 库路径（相对项目根，保持默认即可）
REPORTS_DB_PATH=02.data/reports.db
POWER_AGENT_DB_PATH=02.data/power_agent.db
```

生成 JWT 密钥：

```powershell
cd e:\power_report_agent
.\.venv\Scripts\python.exe -c "import secrets;print(secrets.token_urlsafe(48))"
```

**验证方法：**

```powershell
Test-Path 01.code\backend\.env
(Get-Content 01.code\backend\.env | Select-String "^JWT_SECRET_KEY=.").Line.Length -gt 20
# 预期输出: True / True
```

**成功标志：** `.env` 存在且 `JWT_SECRET_KEY` 非空（否则后端启动即报"缺少 JWT_SECRET_KEY"）。

### 步骤5：启动后端并验证

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\backend
$env:PYTHONIOENCODING = "utf-8"
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**验证方法：**

```powershell
# 新开一个 PowerShell 窗口
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | ConvertTo-Json -Depth 3
# 预期输出: status=healthy；databases.reports/power_agent=ok；llm.configured 按是否配 Key

# 交互式文档（便于逐个接口验证）
Start-Process "http://127.0.0.1:8000/docs"
```

**成功标志：** 健康检查返回 `healthy` 且两库 `ok`；`/docs` 可打开。

### 步骤6：启动前端并完成端到端验证

**操作命令：**

```powershell
cd e:\power_report_agent\01.code\frontend
npm run dev        # 默认 http://127.0.0.1:5173
```

**验证方法：**

```powershell
# 1) 前端端口可访问
Invoke-WebRequest "http://127.0.0.1:5173" -UseBasicParsing | Select-Object -ExpandProperty StatusCode
# 预期输出: 200

# 2) 端到端回归（无头浏览器，32 条断言；需前后端都在跑）
cd e:\power_report_agent
node 01.code\tools\ui_flow_check.mjs
# 预期输出: 逐条 PASS，末行"全部通过"
```

**成功标志：**

- 浏览器打开 `http://127.0.0.1:5173` 可见登录页
- 注册/登录后能提问并出图
- 回归脚本 27 条全部 PASS

### 步骤7：局域网访问（其他设备打开）

**操作命令：**

```powershell
# 1) 查本机内网 IP（取 IPv4 地址，通常是 192.168.x.x 或 10.x.x.x）
ipconfig | Select-String "IPv4"

# 2) 放行入站端口（**需管理员权限的 PowerShell**）
New-NetFirewallRule -DisplayName "zdxb-backend-8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
New-NetFirewallRule -DisplayName "zdxb-frontend-5173" -Direction Inbound -Protocol TCP -LocalPort 5173 -Action Allow
```

前端在开发模式下访问后端有两种做法，任选其一：

```powershell
# 做法 A（推荐，前端不用改）：Vite 代理已指向 127.0.0.1:8000，
#   局域网设备只需访问 http://<本机内网IP>:5173，前端所有 /api 请求由本机代理转发
# 做法 B：后端已监听 0.0.0.0，也可直接访问 http://<本机内网IP>:8000/docs 验证接口
```

**验证方法：**

```powershell
# 本机自测：用内网 IP 访问（而不是 127.0.0.1），确认监听生效
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" } | Select-Object -First 1).IPAddress
Invoke-RestMethod "http://$ip`:8000/api/v1/system/health" | Select-Object -ExpandProperty data | Select-Object status
# 预期输出: healthy
```

随后在手机上连接**同一 Wi-Fi**，浏览器访问 `http://<本机内网IP>:5173` 应能看到登录页并可完成一次提问。

**成功标志：**

- 本机用内网 IP 能访问（说明不是只监听 127.0.0.1）
- 局域网设备可打开页面并完成问答
- 页面窄屏下自动切单栏（右栏进底部抽屉）

### 步骤8：数据备份与还原

**操作命令：**

```powershell
cd e:\power_report_agent

# 备份：两库 + 上传文件 + 评测报告（.env 另行安全保管）
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
New-Item -ItemType Directory -Force -Path "backup\$stamp" | Out-Null
Copy-Item 02.data\reports.db     "backup\$stamp\reports.db"
Copy-Item 02.data\power_agent.db "backup\$stamp\power_agent.db"
Copy-Item 02.data\uploads        "backup\$stamp\uploads" -Recurse -Force
Copy-Item 02.data\eval\reports   "backup\$stamp\eval_reports" -Recurse -Force
Write-Host "备份完成：backup\$stamp"
```

**验证方法：**

```powershell
Get-ChildItem "backup\$stamp" | Select-Object Name, Length
# 预期输出: reports.db（数百 MB）、power_agent.db（小）、uploads、eval_reports

# 还原演练（先停后端，再覆盖，再启动）
# Copy-Item "backup\$stamp\power_agent.db" 02.data\power_agent.db -Force
```

**成功标志：** 备份目录包含两库与文件区；还原后健康检查与问答正常。

## 四、验证清单

| 验证项 | 验证方法 | 预期结果 |
|--------|----------|----------|
| Python 版本 | `python --version` | ≥3.12 |
| Node 版本 | `node --version` | ≥18 |
| 后端依赖 | `pip list \| Select-String "fastapi\|plotly\|sqlglot"` | 列出且版本正确 |
| 前端依赖 | `Test-Path 01.code\frontend\node_modules\antd` | True |
| 两库存在 | `Test-Path 02.data\reports.db` 等 | True |
| 数据规模 | 查询 `report_minute` 行数 | 2075259 |
| 口径校验 | 查询三级总量 | 37283.7477 |
| 后端健康 | `Invoke-RestMethod .../system/health` | `healthy` + 两库 ok |
| 前端可访问 | `Invoke-WebRequest http://127.0.0.1:5173` | 200 |
| 端到端回归 | `node tools/ui_flow_check.mjs` | 27 条全 PASS |
| 局域网监听 | 用内网 IP 调 health | `healthy` |
| 防火墙放行 | `Get-NetFirewallRule -DisplayName "zdxb-*"` | 两条规则存在 |
| 备份完整 | 备份目录内容 | 两库 + uploads + 报告 |

## 五、故障排除

### 问题1：后端启动报"缺少 JWT_SECRET_KEY"

**现象：** 启动即失败，提示缺少密钥。

**解决方案：**

```powershell
cd e:\power_report_agent
$key = .\.venv\Scripts\python.exe -c "import secrets;print(secrets.token_urlsafe(48))"
# 写入 .env（注意不要留引号）
Add-Content 01.code\backend\.env "JWT_SECRET_KEY=$key"
# 重启后端
```

### 问题2：端口被占用（8000 / 5173）

**现象：** `[Errno 10048] error while attempting to bind on address`。

**解决方案：**

```powershell
# 查看占用进程
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object OwningProcess
# 结束占用（或用别的端口：--port 8001，并同步改 vite 代理）
Stop-Process -Id <OwningProcess> -Force
```

### 问题3：局域网设备打不开（本机 127.0.0.1 正常）

**现象：** 手机访问超时。

**解决方案：**

```powershell
# 1) 后端/前端是否监听 0.0.0.0（不是 127.0.0.1）
netstat -ano | Select-String ":8000|:5173"
# 2) 防火墙是否放行（见步骤7）
Get-NetFirewallRule -DisplayName "zdxb-*" | Select-Object DisplayName, Enabled, Direction
# 3) 是否为"公用网络"配置文件（公用网络默认更严格）
Get-NetConnectionProfile | Select-Object Name, NetworkCategory
```

### 问题4：页面能开但请求全部失败（CORS / 代理）

**现象：** 浏览器控制台报 CORS 或请求 404。

**解决方案：**

```powershell
# 确认后端在跑且健康
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" | Select-Object -ExpandProperty data | Select-Object status
# 用 127.0.0.1（而非 localhost）访问前端，避免代理与 CORS 的解析差异：http://127.0.0.1:5173
```

### 问题5：上传大文件很慢或失败

**现象：** 207 万行文件上传超过 1 分钟或中途报错。

**解决方案：**

- 解析与入库本身约 50s（本项目实测 32.1s + 17.8s），属正常范围；页面有进度与耗时提示
- 单文件上限 200MB；更大文件请按月/年拆分
- 确认磁盘剩余空间充足（解析中间文件约为原文件 1–2 倍）

### 问题6：数据被"改坏"想恢复

**现象：** 删除了动态表 / 改了报表数据想还原。

**解决方案：**

```powershell
# 用备份还原（步骤8），或按 DDL 重建预置库
cd e:\power_report_agent
.\.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force
# 注意：重建只恢复预置 5 张报表与系统库；用户上传的动态表需从备份还原
```

## 六、参考代码

### 关键配置文件

```
01.code/backend/.env               # 端口 / 库路径 / 鉴权 / 模型
01.code/backend/requirements.txt   # 后端依赖（实测版本）
01.code/frontend/vite.config.ts    # 前端端口与 /api 代理
01.code/backend/.gitignore         # 忽略 .env 与缓存
```

### 常用命令

```powershell
# 启动后端（局域网可访问）
cd e:\power_report_agent\01.code\backend
$env:PYTHONIOENCODING = "utf-8"
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 启动前端
cd e:\power_report_agent\01.code\frontend
npm run dev

# 建库 / 评测题库 / 单元测试 / 回归
cd e:\power_report_agent
.\.venv\Scripts\python.exe 01.code\scripts\03_init_db.py --force
.\.venv\Scripts\python.exe 01.code\scripts\04_build_eval_set.py
cd 01.code\backend; ..\..\.venv\Scripts\python.exe -m pytest -q; cd ..\..
node 01.code\tools\ui_flow_check.mjs

# 查内网 IP 与监听状态
ipconfig | Select-String "IPv4"
netstat -ano | Select-String ":8000|:5173"
```

### 关键配置说明

| 参数 | 说明 | 默认/示例 |
|------|------|-----------|
| `APP_PORT` | 后端端口 | `8000` |
| `UPLOAD_DIR_PATH` | 上传落盘根目录（按用户隔离） | `02.data/uploads` |
| `UPLOAD_MAX_MB` | 单文件上限 | `200` |
| `UPLOAD_PENDING_TTL_HOURS` | 未确认上传的存活时长（惰性回收） | `24` |
| `REPORTS_DB_PATH` / `POWER_AGENT_DB_PATH` | 两库路径（相对项目根解析） | `02.data/reports.db` / `02.data/power_agent.db` |
| 生产化建议（本项目未做） | HTTPS、反向代理、多 worker、数据库备份策略 | 见架构文档 ADR-010 |
