"""Офлайн-проверки очереди ответов Kwork («Переписка временно недоступна — проверяем собеседника»):
состояние диалога, карточка, часы очереди, обработка очереди и кнопка «Отправить». Биржа и Telegram — заглушки.

    python tests/test_kwork_queue.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import fl_watch as fw  # noqa: E402
import inbox_watch as iw  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


# --- состояние диалога по данным window.chatList (как у Alphalive 12.10) ---
moder = {"USERID": 1, "moderationStatus": "new", "allowedDialog": True, "blocked": False,
         "lastMessage": {"inboxModeration": {"status": "new", "is_processing": True, "spammed_at": None}}}
check("на проверке: moderationStatus=new / is_processing → moderated", iw.dialog_state(moder) == "moderated")
check("обычный диалог → open", iw.dialog_state({"USERID": 2, "moderationStatus": None, "lastMessage": {}}) == "open")
check("Kwork признал спамером или старый закрытый диалог → closed",
      iw.dialog_state({"USERID": 3, "lastMessage": {"inboxModeration": {"spammed_at": "2026-10-12 19:00:00"}}}) == "closed")

# --- карточка и кнопки ---
item = {"site": "kwork", "peer": "Alphalive", "peer_id": 1, "subject": "", "url": "https://kwork.ru/inbox/Alphalive",
        "key": "kw:1", "new": [{"text": "Добрый день", "files": [], "mine": False, "time": 1}], "history": [],
        "moderated": True}
check("карточка: пометка «Kwork проверяет заказчика»", "Kwork проверяет заказчика" in iw.card(item, "Здравствуйте!"))
check("кнопка «⏳ Отправить, когда откроют» вместо «🚀 Отправить»",
      iw.keyboard(5, item["url"], True, True).inline_keyboard[0][0].text.startswith("⏳"))
check("в очереди — «В очереди» в карточке", "В очереди" in iw.card(item, "Здравствуйте!", "queued"))

# --- часы очереди: с 22:00 до 01:00 по Новосибирску ---
nsk = lambda h, m=0: datetime(2026, 10, 16, h, m, tzinfo=fw.NSK)  # noqa: E731
check("часы очереди: 21:59 нет, 22:30 и 00:30 да, 01:00 и 13:00 нет",
      [iw.in_queue_hours(nsk(*x)) for x in [(21, 59), (22, 30), (0, 30), (1, 0), (13, 0)]] == [False, True, True, False, False])

# --- обработка очереди ---
iw.DB = Path(tempfile.mkdtemp()) / "q.db"
c = iw.db()


def add(peer_id, status, moderated=True, text="Здравствуйте! Сделаю за 2 дня."):
    it = {**item, "peer": f"p{peer_id}", "peer_id": peer_id, "key": f"kw:{peer_id}", "moderated": moderated}
    cur = c.execute("insert into chat_items (item_json, draft, status, created) values (?,?,?,?)",
                    (json.dumps(it, ensure_ascii=False), text, status, "2026-10-16"))
    c.commit()
    return cur.lastrowid


class Bot:
    def __init__(self):
        self.msgs = []

    async def send_message(self, chat, text, **kw):
        self.msgs.append(text)


sent_calls = []


async def fake_send(it, text):
    sent_calls.append(it["peer_id"])
    return it["peer_id"] != 15                               # заказчику 15 отправка «не проходит»

iw.send_reply = fake_send
a = add(11, "queued")                       # открыли → отправить
b = add(12, "queued")                       # всё ещё на проверке → ждать
d = add(13, "new")                          # в очередь не ставили, открыли → «можно отвечать»
e = add(14, "queued")                       # Kwork закрыл диалог → снять
f = add(15, "queued")                       # открыли, но отправка не удалась → вернуть «new»
g = add(16, "new", moderated=False)         # обычная карточка — не трогать


async def states(ids):
    return {11: "open", 12: "moderated", 13: "open", 14: "closed", 15: "open"}

iw.kwork_states = states
bot = Bot()
n = asyncio.run(iw.process_queue(bot, 1))
st = lambda i: c.execute("select status from chat_items where id=?", (i,)).fetchone()[0]  # noqa: E731
check("открыли — ответ из очереди отправлен, статус sent, сообщение «✅»",
      st(a) == "sent" and 11 in sent_calls and any("✅" in m and "p11" in m for m in bot.msgs) and n == 1)
check("ещё на проверке — ждёт в очереди, не отправляется", st(b) == "queued" and 12 not in sent_calls)
check("в очередь не ставили — «🔓 можно отвечать», без отправки", st(d) == "new" and 13 not in sent_calls
      and any("🔓" in m and "p13" in m for m in bot.msgs))
check("Kwork закрыл диалог — снят из очереди, «🚫»", st(e) == "skipped" and any("🚫" in m for m in bot.msgs))
check("отправка не прошла — вернул в «new» и предупредил", st(f) == "new" and any("⚠️" in m and "p15" in m for m in bot.msgs))
check("обычные карточки очередь не трогает", st(g) == "new" and 16 not in sent_calls)
bot2 = Bot()
asyncio.run(iw.process_queue(bot2, 1))
check("повторный проход: «🔓» не дублируется", not any("p13" in m for m in bot2.msgs))


# --- кнопка «Отправить» при проверке ставит в очередь ---
class Msg:
    async def edit_text(self, *a, **k): pass
    async def edit_reply_markup(self, *a, **k): pass
    async def answer(self, *a, **k): pass


class CB:
    def __init__(self, data):
        self.data, self.message, self.alerts = data, Msg(), []

    async def answer(self, text=None, **kw):
        self.alerts.append(text)

h = add(17, "new", moderated=False)


async def states_mod(ids):
    return {17: "moderated", 18: "open"}

iw.kwork_states = states_mod
sent_calls.clear()
asyncio.run(iw.on_send(CB(f"cs:{h}")))
check("«🚀 Отправить», а Kwork проверяет заказчика → в очередь, без отправки", st(h) == "queued" and not sent_calls)
i2 = add(18, "new", moderated=False)
asyncio.run(iw.on_send(CB(f"cq:{i2}")))
check("«⏳ Отправить, когда откроют», а диалог уже открыт → отправляет сразу", st(i2) == "sent" and 18 in sent_calls)
cb = CB(f"cs:{h}")
asyncio.run(iw.on_send(cb))
check("повторное нажатие на ответ в очереди — подсказка, без отправки", "очереди" in str(cb.alerts) and 17 not in sent_calls)
check("счётчик очереди", iw.queued_count() == 2)                      # b (12) и h (17)

# --- ночной режим бота: после полуночи не ждать «до 22:00» следующего дня ---
import bot as B  # noqa: E402

stopped = []


async def fake_stop():
    stopped.append(1)

B.dp.stop_polling = fake_stop
real_in_work, real_queued, real_hours = fw.in_work_hours, iw.queued_count, iw.in_queue_hours
fw.in_work_hours = lambda now=None: False                   # 00:30 — нерабочее время
iw.queued_count = lambda: 0                                  # очередь уже пуста
iw.in_queue_hours = lambda now=None: True


async def run_stop():
    await asyncio.wait_for(B.stop_at_night(), 3)

try:
    asyncio.run(run_stop())
    check("после полуночи с пустой очередью бот останавливается сразу, а не ждёт до завтрашних 22:00", stopped == [1])
except asyncio.TimeoutError:
    check("после полуночи с пустой очередью бот останавливается сразу, а не ждёт до завтрашних 22:00", False)
fw.in_work_hours, iw.queued_count, iw.in_queue_hours = real_in_work, real_queued, real_hours

check("старый диалог, куда писать нельзя (allowedDialog=false) → closed",
      iw.dialog_state({"USERID": 4, "allowedDialog": False}) == "closed")

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
