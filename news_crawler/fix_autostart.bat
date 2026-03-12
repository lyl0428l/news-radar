@echo off
chcp 65001 >nul
echo Setting service to auto start (requires admin)...
powershell -Command "Start-Process powershell.exe -ArgumentList '-ExecutionPolicy Bypass -File \"%~dp0fix_autostart.ps1\"' -Verb RunAs -Wait"
echo.
echo Checking result...
sc qc NewsCrawlerService 2>nul | findstr START_TYPE
echo.
pause
