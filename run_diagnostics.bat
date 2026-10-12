@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe diagnostics_cli.py
) else (
  python diagnostics_cli.py
)
pause
