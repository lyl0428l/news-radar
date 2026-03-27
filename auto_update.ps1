$PROJECT = "C:\news_crawler"
$BASE    = "https://raw.githubusercontent.com/lyl0428l/news-radar/master"
$LOG     = "$PROJECT\logs\auto_update.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Output $line
    Add-Content -Path $LOG -Value $line -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path "$PROJECT\logs" | Out-Null
Set-Location $PROJECT
Write-Log "===== auto update start ====="

$files = @{
    "config.py"                      = "$PROJECT\config.py"
    "main.py"                        = "$PROJECT\main.py"
    "storage.py"                     = "$PROJECT\storage.py"
    "scheduler.py"                   = "$PROJECT\scheduler.py"
    "models.py"                      = "$PROJECT\models.py"
    "media_storage.py"               = "$PROJECT\media_storage.py"
    "logging_config.py"              = "$PROJECT\logging_config.py"
    "run_web.py"                     = "$PROJECT\run_web.py"
    "server_receiver.py"             = "$PROJECT\server_receiver.py"
    "crawlers/base.py"               = "$PROJECT\crawlers\base.py"
    "crawlers/tencent.py"            = "$PROJECT\crawlers\tencent.py"
    "crawlers/sohu.py"               = "$PROJECT\crawlers\sohu.py"
    "crawlers/xinhua.py"             = "$PROJECT\crawlers\xinhua.py"
    "crawlers/thepaper.py"           = "$PROJECT\crawlers\thepaper.py"
    "crawlers/sina.py"               = "$PROJECT\crawlers\sina.py"
    "crawlers/netease.py"            = "$PROJECT\crawlers\netease.py"
    "crawlers/people.py"             = "$PROJECT\crawlers\people.py"
    "crawlers/cctv.py"               = "$PROJECT\crawlers\cctv.py"
    "crawlers/jiemian.py"            = "$PROJECT\crawlers\jiemian.py"
    "crawlers/ifeng.py"              = "$PROJECT\crawlers\ifeng.py"
    "utils/notify.py"                = "$PROJECT\utils\notify.py"
    "utils/content_extractor.py"     = "$PROJECT\utils\content_extractor.py"
    "utils/browser.py"               = "$PROJECT\utils\browser.py"
    "utils/sync_remote.py"           = "$PROJECT\utils\sync_remote.py"
    "web/app.py"                     = "$PROJECT\web\app.py"
    "web/templates/index.html"       = "$PROJECT\web\templates\index.html"
    "web/templates/archive.html"     = "$PROJECT\web\templates\archive.html"
    "web/templates/detail.html"      = "$PROJECT\web\templates\detail.html"
    "web/templates/health.html"      = "$PROJECT\web\templates\health.html"
    "web/templates/404.html"         = "$PROJECT\web\templates\404.html"
    "web/templates/500.html"         = "$PROJECT\web\templates\500.html"
    "start_services.ps1"             = "$PROJECT\start_services.ps1"
    "start_background.bat"           = "$PROJECT\start_background.bat"
}

$updated = 0
$failed  = 0

foreach ($rel in $files.Keys) {
    $local = $files[$rel]
    $url   = "$BASE/$rel"
    try {
        $tmp = "$local.tmp"
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
        $newHash = (Get-FileHash $tmp -Algorithm MD5).Hash
        $oldHash = if (Test-Path $local) { (Get-FileHash $local -Algorithm MD5).Hash } else { "" }
        if ($newHash -ne $oldHash) {
            Copy-Item $tmp $local -Force
            Write-Log "  updated: $rel"
            $updated++
        }
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Log "  failed: $rel | $_"
        $failed++
        Remove-Item "$local.tmp" -Force -ErrorAction SilentlyContinue
    }
}

Write-Log "download done: updated $updated, failed $failed"

if ($updated -gt 0) {
    Write-Log "changes detected, restarting services..."
    Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3
    # 直接启动（不依赖 start_services.ps1，避免该文件损坏导致启动失败）
    Start-Process python -ArgumentList @("$PROJECT\run_web.py") -WorkingDirectory $PROJECT -WindowStyle Hidden
    Start-Sleep -Seconds 2
    Start-Process python -ArgumentList @("$PROJECT\scheduler.py") -WorkingDirectory $PROJECT -WindowStyle Hidden
    Start-Sleep -Seconds 5
    $procs = Get-Process python,pythonw -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Log "restart ok, process count: $($procs.Count)"
    } else {
        Write-Log "warning: no python process found after restart"
    }
} else {
    Write-Log "no changes, skip restart"
}

Write-Log "===== auto update done ====="
