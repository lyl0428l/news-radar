@echo off
chcp 65001 >nul
title News Crawler Service - Install

:: 自动检测 Python 路径
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
    echo [ERROR] Python not found!
    pause
    exit /b 1
)

echo ============================================================
echo   News Crawler - Windows Service Manager
echo ============================================================
echo.
echo   Python: %PYTHON%
echo   Script: %~dp0news_service.py
echo.
echo   [1] Install service   (安装服务)
echo   [2] Start service     (启动服务)
echo   [3] Stop service      (停止服务)
echo   [4] Restart service   (重启服务)
echo   [5] Remove service    (卸载服务)
echo   [6] Check status      (查看状态)
echo   [0] Exit
echo.
echo ============================================================
echo.

set /p choice="Enter choice (0-6): "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto start
if "%choice%"=="3" goto stop
if "%choice%"=="4" goto restart
if "%choice%"=="5" goto remove
if "%choice%"=="6" goto status
if "%choice%"=="0" goto end
echo Invalid choice.
pause
goto end

:install
echo.
echo Installing service...
"%PYTHON%" "%~dp0news_service.py" --startup=auto install
if %ERRORLEVEL%==0 (
    echo.
    echo Service installed successfully!
    echo.
    echo Configuring auto-restart on failure...
    sc failure NewsCrawlerService reset=86400 actions=restart/60000/restart/120000/restart/300000 >nul 2>&1
    echo Done. Service will auto-restart on crash (60s / 120s / 300s intervals).
) else (
    echo.
    echo Install failed. Make sure you run this as Administrator!
)
echo.
pause
goto end

:start
echo.
echo Starting service...
"%PYTHON%" "%~dp0news_service.py" start
echo.
pause
goto end

:stop
echo.
echo Stopping service...
"%PYTHON%" "%~dp0news_service.py" stop
echo.
pause
goto end

:restart
echo.
echo Restarting service...
"%PYTHON%" "%~dp0news_service.py" stop
timeout /t 3 /nobreak >nul
"%PYTHON%" "%~dp0news_service.py" start
echo.
pause
goto end

:remove
echo.
echo Stopping and removing service...
"%PYTHON%" "%~dp0news_service.py" stop >nul 2>&1
timeout /t 2 /nobreak >nul
"%PYTHON%" "%~dp0news_service.py" remove
echo.
pause
goto end

:status
echo.
sc query NewsCrawlerService 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Service is not installed.
)
echo.
pause
goto end

:end
