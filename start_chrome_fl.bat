@echo off
rem Chrome для откликов на FL.ru: отдельный профиль, порт отладки 9333 (к нему подключается бот)
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9333 --user-data-dir="C:\Projects\_docsl-profile\.chrome-fl" --no-first-run https://www.fl.ru/
