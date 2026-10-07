@echo off
rem Chrome для откликов на FL.ru и Kwork: отдельный профиль, порт отладки 9333 (к нему подключается бот)
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9333 --user-data-dir="C:\Projects\_docs\fl-profile\.chrome-fl" --no-first-run https://www.fl.ru/
