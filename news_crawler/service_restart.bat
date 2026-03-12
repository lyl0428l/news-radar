@echo off
chcp 65001 >nul

:: ---- Auto-elevate to admin ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting admin privileges...
    powershell -Command "Start-Process cmd.exe -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo ========================================
echo   News Crawler Service - Reinstall
echo ========================================
echo.

echo [1/6] Stopping service...
D:\Python\python.exe news_service.py stop
timeout /t 3 /nobreak >nul

echo [2/6] Removing service...
D:\Python\python.exe news_service.py remove
timeout /t 2 /nobreak >nul

echo [3/6] Clearing cache...
if exist __pycache__ rd /s /q __pycache__
if exist web\__pycache__ rd /s /q web\__pycache__
if exist crawlers\__pycache__ rd /s /q crawlers\__pycache__
echo     Cache cleared

echo [4/6] Installing service...
D:\Python\python.exe news_service.py --startup=auto install
timeout /t 2 /nobreak >nul

echo [5/6] Configuring auto-start + failure recovery...
sc config NewsCrawlerService start=auto
sc failure NewsCrawlerService reset=86400 actions=restart/5000/restart/10000/restart/30000

echo [6/6] Starting service...
D:\Python\python.exe news_service.py start

echo.
echo ========================================
echo   Service reinstalled and started!
echo   Wait 10 seconds then visit:
echo   http://127.0.0.1:5000
echo ========================================
echo.

echo Verifying...
timeout /t 10 /nobreak >nul
sc query NewsCrawlerService | findstr STATE
echo.
pause
