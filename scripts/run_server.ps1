# knowledge_manager 一键启动开发服务
#
# 用法（在项目根目录的 PowerShell 里）：
#     .\scripts\run_server.ps1
# 或在文件资源管理器里右键本文件 →「使用 PowerShell 运行」
#
# 它会先清掉可能残留的 uvicorn 子进程——Ctrl+C 或直接关窗口有时只杀掉了外层，
# 留下子进程占着 8102 端口，下次启动就会报 [Errno 10048]。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # scripts/ 的上一级 = 项目根
Set-Location $root

Write-Host ""
Write-Host "=== knowledge_manager 开发服务 ===" -ForegroundColor Cyan

$stale = Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*knowledge_manager\.venv*" }
if ($stale) {
    foreach ($p in $stale) {
        Write-Host "  停止残留进程 PID $($p.Id)" -ForegroundColor Yellow
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

if (-not (Test-Path "$root\.env")) {
    Write-Host "  ✘ 找不到 .env，请先确认在项目根目录" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  前端登录页   http://127.0.0.1:8102/ui/"        -ForegroundColor Green
Write-Host "  接口文档     http://127.0.0.1:8102/docs"       -ForegroundColor Green
Write-Host "  健康检查     http://127.0.0.1:8102/health"     -ForegroundColor Green
Write-Host ""
Write-Host "  演示账号：lina / zhangwei / wangqiang　密码 Demo@12345"
Write-Host "  按 Ctrl+C 停止服务" -ForegroundColor DarkGray
Write-Host ""

& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8102
