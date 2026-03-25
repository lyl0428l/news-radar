@echo off
cd /d D:\AI\AI\news_crawler
start "" /b D:\Python\pythonw.exe D:\AI\AI\news_crawler\scheduler.py
ping 127.0.0.1 -n 3 >nul
start "" /b D:\Python\pythonw.exe D:\AI\AI\news_crawler\run_web.py
