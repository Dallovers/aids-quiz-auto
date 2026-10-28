@echo off
chcp 65001 >nul
cd /d "%~dp0"
"D:\Python\python.exe" quiz_app.py
if errorlevel 1 pause
