@echo off
rem Запуск бота заказов FL.ru + Kwork. Вызывает Планировщик Windows: каждый день в 8:00 и при входе в Windows.
rem Бот сам решает: вне рабочего времени (8-22 по Новосибирску) или если уже запущен — сразу выходит.
cd /d "%~dp0"

rem Chrome с портом отладки нужен для откликов и чтения ТЗ — запускаем, если ещё не открыт
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 9333 -State Listen -ErrorAction SilentlyContinue)) { exit 1 }"
if errorlevel 1 call "%~dp0start_chrome_fl.bat"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem pythonw — без чёрного окна; лог дописывается в bot.log
start "" /min "%~dp0venv\Scripts\pythonw.exe" -c "import runpy, sys; sys.stdout = sys.stderr = open('bot.log', 'a', encoding='utf-8', buffering=1); runpy.run_path('bot.py', run_name='__main__')"
