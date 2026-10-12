@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === AI Agent V3: установка ===
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)
if not exist .venv (
  %PY% -m venv .venv
  if errorlevel 1 goto :error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
if not exist config.env (
  copy /Y config.example.env config.env >nul
  echo Создан пустой config.env из config.example.env
)
echo.
echo Готово.
echo 1. Вставь TELEGRAM_BOT_TOKEN в config.env.
echo 2. Для локального теста AI-ключи можно пока оставить пустыми.
echo 3. Запусти run_smoke_test.bat.
echo 4. Затем run_bot.bat.
pause
exit /b 0
:error
echo.
echo Ошибка установки. Скопируй текст из окна и пришли его в чат.
pause
exit /b 1
