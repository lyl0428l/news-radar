@echo off
chcp 65001 >nul
title News Web Server

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

echo Starting web server...
echo Using Python: %PYTHON%
echo Browser: http://127.0.0.1:5000
echo.
start http://127.0.0.1:5000
"%PYTHON%" "%~dp0web\app.py"
pause
