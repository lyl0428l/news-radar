$PROJECT = "C:\news_crawler"

Set-Location $PROJECT

Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Start-Process python -ArgumentList "scheduler.py" -WorkingDirectory $PROJECT -WindowStyle Hidden
Start-Sleep -Seconds 3

Start-Process python -ArgumentList "run_web.py" -WorkingDirectory $PROJECT -WindowStyle Hidden
Start-Sleep -Seconds 5

$procs = Get-Process python,pythonw -ErrorAction SilentlyContinue
if ($procs) {
    Write-Output "services started, process count: $($procs.Count)"
} else {
    Write-Output "warning: no python process found"
}
