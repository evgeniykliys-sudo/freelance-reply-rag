"""Офлайн-проверки inbox_watch без сети и нейросети: чистка текста сообщений, проверка черновика, карточка.

    python tests/test_inbox_watch.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import inbox_watch as iw  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


check("BB-ссылки, сущности и <br> Kwork → обычный текст",
      iw.clean('Тема: проект &quot;[URL=https://kwork.ru/projects/1]Правки[/URL]&quot;<br>ок\r\n')
      == 'Тема: проект "Правки (https://kwork.ru/projects/1)"\nок')
check("markdown в ответе подсвечивается", any("markdown" in p for p in iw.problems("**Этап 1** — 4 500 ₽")))
check("контакты в ответе подсвечиваются", iw.problems("Напишите в t.me/me"))
check("нормальный ответ без замечаний", iw.problems("Посмотрел отчёты. Этап 1 — 4 500 ₽, 2 дня.") == [])
item = {"site": "kwork", "peer": "x", "subject": "", "new": [{"text": "спасибо", "files": [], "mine": False, "time": 1}]}
check("«спасибо» → карточка без черновика", "не нужен" in iw.card(item, "НЕ НУЖЕН: благодарность"))
check("без черновика нет кнопки «Отправить»",
      iw.keyboard(1, "https://kwork.ru/inbox/x", sendable=False).inline_keyboard[0][0].text == "✍️ Свой текст")

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
