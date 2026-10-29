# ============================================================================
# 智电析报 · 停止服务（由 stop.bat 调用）
# ----------------------------------------------------------------------------
# 安全设计（避免"清理脚本反而把用户的终端/IDE 杀掉"）：
#   ① 只杀**白名单进程名**：python.exe / pythonw.exe / node.exe / cmd.exe；
#   ② 保护**自身祖先链**（启动本脚本的终端、IDE、shell 一律不碰）；
#   ③ 只处理"监听了目标端口"或"命令行匹配本项目特征"的进程；
#   ④ 端口范围**不含 PPT 预览端口**（03.ppt 的预览服务不受影响）。
# 端口来源：优先读 `02.data\tmp\_ports.json`（start.ps1 写入），再核对候选列表。
# ============================================================================
param([switch]$NoPause)

$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AllowNames = @('python.exe', 'pythonw.exe', 'node.exe', 'cmd.exe', 'npm.exe')
$CandidatePorts = @(8000, 8080, 3000, 4180, 5599, 8081, 4181)
$PortLabels = @{
    8000 = 'backend (FastAPI/uvicorn)'
    8080 = 'frontend (Vite)'; 3000 = 'frontend (Vite)'; 4180 = 'frontend (Vite)'
    5599 = 'frontend (Vite)'; 8081 = 'frontend (Vite)'; 4181 = 'frontend (Vite)'
}

# 读 start.ps1 记录的端口
$ports = @($CandidatePorts)
$stateFile = Join-Path $Root '02.data\tmp\_ports.json'
if (Test-Path $stateFile) {
    try {
        $state = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $picked = @()
        foreach ($p in @($state.backend, $state.frontend)) { if ($p) { $picked += [int]$p } }
        if ($picked.Count -gt 0) { $ports = @($picked + $CandidatePorts | Select-Object -Unique) }
    } catch {}
}

# 自身祖先链（保护对象）
$ancestors = New-Object System.Collections.Generic.HashSet[int]
$cur = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" | Select-Object -First 1
$guard = 0
while ($cur -and $cur.ParentProcessId -and $guard -lt 32) {
    $null = $ancestors.Add([int]$cur.ParentProcessId)
    $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)" | Select-Object -First 1
    $guard++
}

function Stop-Tree([int]$ProcessId, [string]$Why) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" | Select-Object -First 1
    if (-not $proc) { return }
    if ($ancestors.Contains($ProcessId)) {
        Write-Host ("  [skip] PID {0} 属于本脚本祖先链（保护）" -f $ProcessId) -ForegroundColor DarkGray
        return
    }
    if ($AllowNames -notcontains $proc.Name) {
        Write-Host ("  [skip] PID {0} 进程名 {1} 不在白名单" -f $ProcessId, $proc.Name) -ForegroundColor DarkGray
        return
    }
    # 先杀子进程（uvicorn --reload 会派生子进程；npm 会派生 node）
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId")) {
        Stop-Tree ([int]$child.ProcessId) 'child'
    }
    Stop-Process -Id $ProcessId -Force
    Write-Host ("  [kill] PID {0}  {1}" -f $ProcessId, $Why) -ForegroundColor Yellow
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 智电析报 · 停止服务" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

foreach ($port in $ports) {
    $label = if ($PortLabels.ContainsKey($port)) { $PortLabels[$port] } else { "port $port" }
    $owners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -eq 0) {
        Write-Host ("  [ ok ] port {0} : 无监听" -f $port) -ForegroundColor DarkGray
    } else {
        foreach ($owner in ($owners | Sort-Object -Descending)) {
            Stop-Tree ([int]$owner) ("port $port -> $label")
        }
    }
}

# ── 兜底清扫：按命令行特征收尾 ────────────────────────────────────────────
# 为什么必须有这一步（2026-10-13 实测）：
#   `uvicorn --reload` 是「**监督进程 + 工作子进程**」结构，监听套接字在子进程手里；
#   只杀按端口找到的子进程，**监督进程会立刻把子进程重新拉起**（表现为"杀完端口还在"）。
#   所以再按命令行特征扫一遍（含 cmd 包装窗口），并由祖先链保护确保不误杀终端/IDE。
$sweep = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and (
            ($_.CommandLine -like '*uvicorn*app.main*') -or
            ($_.CommandLine -like "*$Root*01.code\frontend*" -and $_.CommandLine -like '*vite*')
        )
    } | Sort-Object ProcessId)   # 父进程 PID 通常更小 → 先杀父，断掉"重新拉起"的源头
foreach ($proc in $sweep) {
    Stop-Tree ([int]$proc.ProcessId) 'leftover uvicorn / vite（监督进程或窗口包装）'
}

Start-Sleep -Milliseconds 800
$left = @($ports | Where-Object { @(Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue).Count -gt 0 })
if ($left.Count -eq 0) { Write-Host "`n  done : 端口已全部释放" -ForegroundColor Green }
else { Write-Host "`n  warn : 仍有端口在监听：$($left -join ', ')" -ForegroundColor Yellow }
Write-Host "  （PPT 预览端口不在此清理范围内）" -ForegroundColor DarkGray

if (-not $NoPause) { Write-Host ""; Read-Host "按回车键关闭本窗口" | Out-Null }
