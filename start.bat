@echo off
chcp 65001 >nul
title News Crawler - Starting...

echo ============================================================
echo   News Aggregator - Startup Script
echo ============================================================
echo.

set DIR=%~dp0

:: 自动检测 Python 路径：优先 D:\Python，其次 PATH 中的 python
set PYTHON=
if exist "D:\Python\python.exe" (
    set PYTHON=D:\Python\python.exe
) else (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 (
        for /f "delims=" %%i in ('where python') do (
            if not defined PYTHON set PYTHON=%%i
        )
    )
)
if not defined PYTHON (
    echo [ERROR] Python not found! Please install Python or set path manually.
    pause
    exit /b 1
)
echo Using Python: %PYTHON%
echo.

:: Start scheduler in background
echo [1/2] Starting crawler scheduler (every 1 hour)...
start "News Crawler Scheduler" /min "%PYTHON%" "%DIR%scheduler.py"

:: Wait a moment for first crawl
timeout /t 5 /nobreak >nul

:: Start web server
echo [2/2] Starting web server on http://127.0.0.1:5000 ...
start "News Web Server" /min "%PYTHON%" "%DIR%web\app.py"

:: Wait and open browser
timeout /t 3 /nobreak >nul
echo.
echo ============================================================
echo   All services started!
echo   Web UI:    http://127.0.0.1:5000
echo   Scheduler: Running in background (every 1 hour)
echo   Logs:      %DIR%logs\crawler.log
echo ============================================================
echo.
echo Opening browser...
start http://127.0.0.1:5000

echo Press any key to stop all services...
pause >nul

:: Kill background processes
taskkill /fi "WINDOWTITLE eq News Crawler Scheduler" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq News Web Server" /f >nul 2>&1
echo Services stopped.
