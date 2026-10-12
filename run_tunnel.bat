@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Туннель для Mini App ===
echo.
echo  1. В ДРУГОМ окне должен быть запущен run_bot.bat
echo  2. Ниже, после баннера, появится ссылка вида https://xxxx.lhr.life
echo  3. Скопируй её в config.env в строку MINIAPP_URL= и перезапусти бота
echo.
echo  Ссылка живёт, пока открыто это окно.
echo  При каждом новом запуске туннеля ссылка меняется - не забудь обновить config.env.
echo.
"%SystemRoot%\System32\OpenSSH\ssh.exe" -o StrictHostKeyChecking=accept-new -R 80:localhost:8080 nokey@localhost.run
echo.
echo Туннель закрыт.
pause
