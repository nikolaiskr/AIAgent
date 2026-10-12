@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe smoke_test.py
) else (
  python smoke_test.py
)
pause
