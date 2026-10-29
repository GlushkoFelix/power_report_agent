# ============================================================================
# 智电析报 · 一键启动（PowerShell 实现；由 start.bat 调用，也可直接右键运行）
# ----------------------------------------------------------------------------
# 用法：
#   start.bat                  # 启动后端(8000) + 前端(自动选端口) + 打开浏览器
#   start.bat --no-open        # 不打开浏览器
#   start.bat --no-reload      # 后端不开热重载（演示更稳）
#   start.bat --no-pause       # 结束后不停留（供脚本调用）
#
# 为什么前端端口要"自动选"（2026-10-13 的实测教训）：
#   Windows 会动态保留一批 TCP 端口段（`netsh int ipv4 show excludedportrange protocol=tcp`），
#   落进保留段的端口**绑定必失败**（报 `EACCES: permission denied`）。此前惯用的 5173 恰好被
#   保留段 5164–5263 圈进去了（PPT 预览用的 5180 同样中招），而保留段会随系统状态变化。
#   所以本脚本**先查保留段、再挑端口**，并在启动后写入 `02.data\tmp\_ports.json` 供 stop 使用。
# ============================================================================
param(
    [switch]$NoOpen,
    [switch]$NoReload,
    [switch]$NoPause
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py     = Join-Path $Root '.venv\Scripts\python.exe'
$BeDir  = Join-Path $Root '01.code\backend'
$FeDir  = Join-Path $Root '01.code\frontend'
$LogDir = Join-Path $Root '02.data\logs'
$TmpDir = Join-Path $Root '02.data\tmp'
New-Item -ItemType Directory -Force -Path $LogDir, $TmpDir | Out-Null

$BackendPort = 8000
#: 前端候选端口：全部落在当前保留段之外（若系统改动，脚本会跳过不可用的）
$FrontendCandidates = @(8080, 3000, 4180, 5599, 8081, 4181)

function Test-PortListening([int]$Port) {
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
}

function Get-ReservedRanges {
    $ranges = @()
    $text = (netsh int ipv4 show excludedportrange protocol=tcp 2>$null) -join "`n"
    foreach ($m in [regex]::Matches($text, '(?m)^\s*(\d+)\s+(\d+)\s*$')) {
        $ranges += ,@([int]$m.Groups[1].Value, [int]$m.Groups[2].Value)
    }
    return $ranges
}

$Reserved = Get-ReservedRanges
function Test-PortReserved([int]$Port) {
    foreach ($r in $Reserved) { if ($Port -ge $r[0] -and $Port -le $r[1]) { return $true } }
    return $false
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 智电析报 · 启动" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ── 1 环境检查 ──────────────────────────────────────────────────────────────
if (-not (Test-Path $Py))   { Write-Host "[x] 缺少虚拟环境：$Py" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $BeDir)) { Write-Host "[x] 缺少后端目录：$BeDir" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $FeDir)) { Write-Host "[x] 缺少前端目录：$FeDir" -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $BeDir '.env'))) {
    Write-Host "[!] 未找到 01.code\backend\.env —— 请先从 .env.example 复制并填 JWT_SECRET_KEY" -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $FeDir 'node_modules'))) {
    Write-Host "[*] 前端依赖缺失，执行 npm install（首次约 1–2 分钟）..." -ForegroundColor Yellow
    Push-Location $FeDir; npm install | Out-Null; Pop-Location
}
Write-Host "[v] 环境检查通过"

# ── 2 选端口（尊重系统保留段）──────────────────────────────────────────────
if (Test-PortReserved $BackendPort) {
    Write-Host "[x] 后端端口 $BackendPort 落在系统保留段内，无法绑定；请改用其它端口" -ForegroundColor Red
    exit 1
}
$FrontendPort = $null
foreach ($cand in $FrontendCandidates) {
    if (-not (Test-PortReserved $cand) -and -not (Test-PortListening $cand)) { $FrontendPort = $cand; break }
}
if (-not $FrontendPort) {
    Write-Host "[x] 前端候选端口（$($FrontendCandidates -join ', ')）均不可用" -ForegroundColor Red
    Write-Host "    可先运行 stop.bat 释放端口，或手动改脚本里的候选列表" -ForegroundColor Yellow
    exit 1
}

# ── 3 后端 ─────────────────────────────────────────────────────────────────
if (Test-PortListening $BackendPort) {
    Write-Host "[=] 后端已在运行（$BackendPort），跳过启动"
} else {
    $argList = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort")
    if (-not $NoReload) { $argList += '--reload' }
    Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $BeDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'backend.log') `
        -RedirectStandardError  (Join-Path $LogDir 'backend.err')
    Write-Host ("[*] 后端启动中（热重载：{0}）..." -f $(if ($NoReload) { '关' } else { '开' }))
}
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try { $null = Invoke-RestMethod "http://127.0.0.1:$BackendPort/api/v1/system/health" -TimeoutSec 3; $ready = $true; break } catch {}
}
if ($ready) { Write-Host "[v] 后端已就绪：http://127.0.0.1:$BackendPort" -ForegroundColor Green }
else { Write-Host "[!] 后端 30s 内未就绪 —— 看 02.data\logs\backend.err" -ForegroundColor Yellow }

# ── 4 前端 ─────────────────────────────────────────────────────────────────
if (Test-PortListening $FrontendPort) {
    Write-Host "[=] 前端已在运行（$FrontendPort），跳过启动"
} else {
    Start-Process -FilePath 'cmd.exe' -WindowStyle Hidden -ArgumentList `
        "/c `"cd /d $FeDir && npm run dev -- --port $FrontendPort --strictPort > `"$LogDir\frontend.log`" 2>&1`""
    Write-Host "[*] 前端启动中（Vite HMR）..."
}
$feReady = $false
for ($i = 0; $i -lt 80; $i++) {
    Start-Sleep -Milliseconds 500
    try { $r = Invoke-WebRequest "http://127.0.0.1:$FrontendPort" -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -eq 200) { $feReady = $true; break } } catch {}
}
if ($feReady) { Write-Host "[v] 前端已就绪：http://127.0.0.1:$FrontendPort" -ForegroundColor Green }
else { Write-Host "[!] 前端 40s 内未就绪 —— 看 02.data\logs\frontend.log" -ForegroundColor Yellow }

# ── 5 记录端口（供 stop.bat 精确停止）＋ 打开浏览器 ────────────────────────
@{ backend = $BackendPort; frontend = $FrontendPort } |
    ConvertTo-Json | Set-Content -Path (Join-Path $TmpDir '_ports.json') -Encoding UTF8

Write-Host ""
Write-Host "  前端      ：http://127.0.0.1:$FrontendPort"
Write-Host "  接口文档  ：http://127.0.0.1:$BackendPort/docs"
Write-Host "  健康检查  ：http://127.0.0.1:$BackendPort/api/v1/system/health"
Write-Host "  日志      ：02.data\logs\backend.log / backend.err / frontend.log"
Write-Host "  停止服务  ：双击 stop.bat"
Write-Host ""
Write-Host "  提示：模型额度有限，演示时才调用；零额度验证请把 01.code\backend\.env 的" -ForegroundColor DarkGray
Write-Host "        LLM_API_KEY 换成占位值（如 sk-xxxxxx）→ 自动回落离线脚本模型。" -ForegroundColor DarkGray

if (-not $NoOpen -and $feReady) { Start-Process "http://127.0.0.1:$FrontendPort" | Out-Null }
if (-not $NoPause) { Write-Host ""; Read-Host "按回车键关闭本窗口（服务继续在后台运行）" | Out-Null }
