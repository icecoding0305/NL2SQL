# NL2SQL 开发环境管理(一键启停/重启/状态)
#
# 用法:
#   powershell -File scripts/dev.ps1 start     # 启动后端 + 前端
#   powershell -File scripts/dev.ps1 stop      # 停止两者
#   powershell -File scripts/dev.ps1 restart   # 重启两者(可靠版)
#   powershell -File scripts/dev.ps1 status    # 查看状态
#
# 特性:
#   - 停止后等待端口真正释放,避免 10048(Address already in use)
#   - 启动后等待端口监听并验证,失败会提示查看 logs/
#   - 双击入口:项目根目录 restart.bat

param(
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action = "start"
)

$BackendPort = 8000
$FrontendPort = 5173
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
$BackendLog = Join-Path $LogDir "backend.log"
$BackendErr = Join-Path $LogDir "backend.err.log"
$FrontendLog = Join-Path $LogDir "frontend.log"
$FrontendErr = Join-Path $LogDir "frontend.err.log"
$WaitTimeout = 20   # 端口等待超时(秒)

function Get-PidByPort($port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Write-LogFile {
    $dir = Split-Path -Parent $args[0]
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

function Wait-PortFree($port, $timeoutSec = $WaitTimeout) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-PidByPort $port) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    return -not (Get-PidByPort $port)
}

function Wait-PortUp($port, $timeoutSec = $WaitTimeout) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while (-not (Get-PidByPort $port) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    return [bool](Get-PidByPort $port)
}

function Start-Backend {
    if (Get-PidByPort $BackendPort) { Write-Host "[backend] already running, skip"; return }
    Write-LogFile $BackendLog
    Write-Host "[backend] starting uvicorn on :$BackendPort"
    Start-Process -FilePath "uv" `
        -ArgumentList @("run", "uvicorn", "nl2sql_agent.main:app", "--port", "$BackendPort") `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $BackendLog `
        -RedirectStandardError $BackendErr `
        -WindowStyle Hidden
    if (Wait-PortUp $BackendPort) {
        Write-Host "[backend] up (PID $(Get-PidByPort $BackendPort))"
    } else {
        Write-Host "[backend] WARNING: ${WaitTimeout}s 内未监听 :$BackendPort, 查看 logs/backend.log"
    }
}

function Start-Frontend {
    if (Get-PidByPort $FrontendPort) { Write-Host "[frontend] already running, skip"; return }
    Write-LogFile $FrontendLog
    Write-Host "[frontend] starting vite on :$FrontendPort"
    Start-Process -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev") `
        -WorkingDirectory (Join-Path $ProjectRoot "web") `
        -RedirectStandardOutput $FrontendLog `
        -RedirectStandardError $FrontendErr `
        -WindowStyle Hidden
    if (Wait-PortUp $FrontendPort) {
        Write-Host "[frontend] up (PID $(Get-PidByPort $FrontendPort))"
    } else {
        Write-Host "[frontend] WARNING: ${WaitTimeout}s 内未监听 :$FrontendPort, 查看 logs/frontend.log"
    }
}

function Stop-Service($name, $port) {
    $procId = Get-PidByPort $port
    if (-not $procId) { Write-Host "[$name] not running"; return }
    Write-Host "[$name] stopping PID $procId"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    if (-not (Wait-PortFree $port)) {
        Write-Host "[$name] WARNING: 端口 :$port 在 ${WaitTimeout}s 内未释放"
    }
}

function Show-Status {
    $b = Get-PidByPort $BackendPort
    $f = Get-PidByPort $FrontendPort
    Write-Host ""
    Write-Host "backend  :$BackendPort  -> " $(if ($b) { "running (PID $b)" } else { "stopped" })
    Write-Host "frontend :$FrontendPort  -> " $(if ($f) { "running (PID $f)" } else { "stopped" })
    Write-Host ""
    Write-Host "frontend URL: http://localhost:$FrontendPort"
    Write-Host "API docs    : http://localhost:$BackendPort/docs"
}

switch ($Action) {
    "start" {
        Start-Backend
        Start-Frontend
        Show-Status
    }
    "stop" {
        Stop-Service "backend" $BackendPort
        Stop-Service "frontend" $FrontendPort
    }
    "status" {
        Show-Status
    }
    "restart" {
        Write-Host "== 停止 =="
        Stop-Service "backend" $BackendPort
        Stop-Service "frontend" $FrontendPort
        Write-Host "== 启动 =="
        Start-Backend
        Start-Frontend
        Show-Status
    }
}
