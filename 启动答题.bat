@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "D:\Python\python.exe" (
  "D:\Python\python.exe" quiz_app.py
) else (
  python quiz_app.py
)
if errorlevel 1 pause
