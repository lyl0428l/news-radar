Set-Service -Name "NewsCrawlerService" -StartupType Automatic
$svc = Get-Service -Name "NewsCrawlerService"
Write-Host "Service: $($svc.Name)"
Write-Host "Status:  $($svc.Status)"
Write-Host "StartType: $($svc.StartType)"
Write-Host ""
Write-Host "Done!"
Read-Host "Press Enter to close"
