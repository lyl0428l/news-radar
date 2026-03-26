# ============================================================
# 新闻爬虫 - 服务启动脚本
# 供 auto_update.ps1 和手动操作调用
# ============================================================

$PROJECT = "C:\news_crawler"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$ts] $msg"
}

Set-Location $PROJECT

# 停止所有旧进程（容错：没有进程时不报错）
Get-Process python, pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# 启动爬虫调度器（后台无窗口）
Start-Process pythonw -ArgumentList "scheduler.py" -WorkingDirectory $PROJECT
Write-Log "调度器已启动"
Start-Sleep -Seconds 3

# 启动 Web 服务（后台无窗口）
Start-Process pythonw -ArgumentList "run_web.py" -WorkingDirectory $PROJECT
Write-Log "Web 服务已启动"
Start-Sleep -Seconds 5

# 验证进程
$procs = Get-Process python, pythonw -ErrorAction SilentlyContinue
if ($procs) {
    Write-Log "服务启动成功，进程数: $($procs.Count)"
    $procs | Select-Object Id, ProcessName | ForEach-Object { Write-Log "  PID $($_.Id) - $($_.ProcessName)" }
} else {
    Write-Log "警告: 未检测到 Python 进程，请手动检查"
}
