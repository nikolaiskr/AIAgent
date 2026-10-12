@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe bot.py
) else (
  python bot.py
)
echo.
echo Бот остановлен. Если выше есть ошибка - скопируй её целиком.
pause
