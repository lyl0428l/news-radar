# ============================================================
# 新闻爬虫 - 自动更新脚本
# 每天凌晨12点由 Windows 任务计划程序自动执行
# 从 GitHub 拉取最新代码，有变化则重启服务
# ============================================================

$PROJECT = "C:\news_crawler"
$BASE    = "https://raw.githubusercontent.com/lyl0428l/news-radar/master"
$LOG     = "$PROJECT\logs\auto_update.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Output $line
    Add-Content -Path $LOG -Value $line -Encoding UTF8
}

Set-Location $PROJECT
Write-Log "===== 自动更新开始 ====="

# 需要更新的文件列表（相对路径 -> 本地路径）
$files = @{
    "config.py"                      = "$PROJECT\config.py"
    "main.py"                        = "$PROJECT\main.py"
    "storage.py"                     = "$PROJECT\storage.py"
    "scheduler.py"                   = "$PROJECT\scheduler.py"
    "models.py"                      = "$PROJECT\models.py"
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
    "web/app.py"                     = "$PROJECT\web\app.py"
    "web/templates/detail.html"      = "$PROJECT\web\templates\detail.html"
    "web/templates/index.html"       = "$PROJECT\web\templates\index.html"
}

$updated = 0
$failed  = 0

foreach ($rel in $files.Keys) {
    $local = $files[$rel]
    $url   = "$BASE/$rel"
    try {
        # 下载到临时文件，比较哈希，只有内容变化才覆盖
        $tmp = "$local.tmp"
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop

        $newHash = (Get-FileHash $tmp -Algorithm MD5).Hash
        $oldHash = if (Test-Path $local) { (Get-FileHash $local -Algorithm MD5).Hash } else { "" }

        if ($newHash -ne $oldHash) {
            Copy-Item $tmp $local -Force
            Write-Log "  更新: $rel"
            $updated++
        }
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Log "  失败: $rel | $_"
        $failed++
        Remove-Item "$local.tmp" -Force -ErrorAction SilentlyContinue
    }
}

Write-Log "下载完成: 更新 $updated 个文件, 失败 $failed 个"

# 有文件更新才重启服务
if ($updated -gt 0) {
    Write-Log "检测到更新，重启服务..."

    # 停止旧进程
    Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3

    # 启动新服务
    PowerShell -ExecutionPolicy Bypass -File "$PROJECT\start_services.ps1"
    Start-Sleep -Seconds 5

    # 确认进程启动
    $procs = Get-Process python,pythonw -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Log "服务重启成功，进程数: $($procs.Count)"
    } else {
        Write-Log "警告: 服务重启后未检测到进程，请手动检查"
    }
} else {
    Write-Log "无文件更新，服务无需重启"
}

Write-Log "===== 自动更新完成 ====="
