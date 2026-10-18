@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 启动会议号池 ...
"C:\Program Files\Python312\python.exe" app.py
pause
