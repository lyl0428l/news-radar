# ============================================================
# 注册自动更新任务到 Windows 任务计划程序
# 在服务器上以管理员身份运行一次即可
# ============================================================

$TaskName   = "NewsRadar-AutoUpdate"
$ScriptPath = "C:\news_crawler\auto_update.ps1"
$LogPath    = "C:\news_crawler\logs\auto_update.log"

# 删除旧任务（如果存在）
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除旧任务: $TaskName"
}

# 定义执行动作：用 PowerShell 运行更新脚本
$action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$ScriptPath`"" `
    -WorkingDirectory "C:\news_crawler"

# 定义触发器：每天凌晨 00:05 执行（00:00整点服务器可能有其他任务，错开5分钟）
$trigger = New-ScheduledTaskTrigger -Daily -At "00:05"

# 定义运行账户：SYSTEM账户，开机自动可用，不需要用户登录
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

# 定义任务设置
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# 注册任务
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "每天凌晨自动从GitHub拉取最新代码，有更新则重启新闻爬虫服务" `
    -Force

Write-Host ""
Write-Host "任务注册成功!" -ForegroundColor Green
Write-Host "  任务名称: $TaskName"
Write-Host "  执行时间: 每天凌晨 00:05"
Write-Host "  脚本路径: $ScriptPath"
Write-Host "  日志路径: $LogPath"
Write-Host ""
Write-Host "验证任务："
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Write-Host ""
Write-Host "手动立即执行一次测试："
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
