"""Офлайн-проверки отправки по кнопке «Утвердить и отправить» (bot.send_now) без Telegram, бирж и Chrome:
биржа и подтверждение подменены заглушками. Главное — итоговое сообщение приходит ВСЕГДА.

    python tests/test_send_flow.py
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import bot  # noqa: E402
import fl_submit  # noqa: E402
import fl_watch as fw  # noqa: E402
import kwork_submit  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


fw.DB = Path(tempfile.mkdtemp()) / "t.db"
c = fw.db()
NOW = datetime.now(timezone.utc)
DRAFT = {"reply": "Здравствуйте! " * 20, "price": 5000, "days": 2, "works": ["8093767", "8093770"]}


def order(oid, source="fl"):
    o = fw.Order(id=oid, title="Сайт на Tilda", link=f"https://x.test/{oid}", desc="d", category="Сайты", budget=None,
                 for_all=False, published=NOW, source=source)
    fw.save_draft(c, o, {}, DRAFT, 1)
    return o


class Msg:
    def __init__(self, log):
        self.log = log

    async def answer(self, text, **kw):
        self.log.append((text, kw))
        return self

    async def edit_reply_markup(self, **kw):
        pass

    async def delete(self):
        pass


class CB:
    def __init__(self, data):
        self.data, self.log, self.alerts = data, [], []
        self.message = Msg(self.log)

    async def answer(self, text=None, **kw):
        self.alerts.append(text)


def run(oid, submit=None, confirm=None, source="fl", timeout=None):
    calls = []

    async def fake_submit(*a, **k):
        calls.append(1)
        return await submit(*a, **k)

    fl_submit.submit = fake_submit
    kwork_submit.submit = fake_submit

    async def fake_confirm(o):
        return confirm

    bot.confirm_sent = fake_confirm
    if timeout:
        bot.SEND_TIMEOUT = timeout
    cb = CB(f"ok:{oid}")
    asyncio.run(bot.send_now(cb, oid))
    status = c.execute("select status from drafts where order_id=?", (oid,)).fetchone()[0]
    last = cb.log[-1] if cb.log else ("", {})
    return cb, status, last[0], last[1], len(calls)


async def good(*a, **k):
    return {"ok": True, "left": 24, "competitors": 7}


# 1. успех на FL.ru: итог с остатком откликов, статус sent_fl
o = order("100")
cb, st, text, kw, n = run("100", submit=good)
check("FL: успех — «Отклик отправлен», остаток откликов и цена в сообщении, статус sent_fl",
      st == "sent_fl" and "Отклик отправлен на FL.ru" in text and "откликов осталось: 24" in text and "5 000 ₽" in text and n == 1)
check("до отправки — сообщение «Отправляю…», предпросмотра со скриншотом нет",
      cb.log[0][0].startswith("⏳") and not any("photo" in str(x[1]) for x in cb.log))

# 2. зависание + биржа подтверждает → успех, а не «тишина»
async def hang(*a, **k):
    await asyncio.sleep(5)

o = order("101")
cb, st, text, kw, n = run("101", submit=hang, confirm=True, timeout=0.3)
check("зависло, но отклик на бирже есть — «отправлен», статус sent_fl", st == "sent_fl" and "Отклик отправлен" in text)

# 3. зависание + подтверждения нет → честное предупреждение, повторно слать вслепую не предлагаем
o = order("102")
cb, st, text, kw, n = run("102", submit=hang, confirm=False, timeout=0.3)
check("зависло, отклика на бирже нет — предупреждение «не дождался», статус new, кнопки повторной отправки нет",
      st == "new" and "не отправлен" in text and "не дождался" in text and "Утвердить" not in str(kw.get("reply_markup")))

# 4. отказ биржи с понятной причиной → причина в сообщении и кнопка «Утвердить и отправить» для повтора
async def refuse(*a, **k):
    raise fl_submit.NotReady("FL.ru не принял отклик: слишком короткий текст")

o = order("103")
cb, st, text, kw, n = run("103", submit=refuse, confirm=False)
check("отказ биржи: причина в сообщении, статус new, можно утвердить заново",
      st == "new" and "слишком короткий текст" in text and "Утвердить и отправить" in str(kw.get("reply_markup")))

# 5. Kwork: успех — сумма заказчика, твоя сумма (−20%), срок
async def kw_good(*a, **k):
    return {"ok": True, "price": 3500, "term": "3 дня", "competitors": 2}

o = order("kw1", source="kwork")
cb, st, text, kw, n = run("kw1", submit=kw_good)
check("Kwork: успех — «Предложение отправлено», заказчик платит, тебе −20%, срок; статус sent_kw",
      st == "sent_kw" and "Предложение отправлено на Kwork" in text and "3 500 ₽" in text and "2 800 ₽" in text
      and "3 дня" in text and "поднял до минимума" in text)

# 6. Kwork подсветил контакты → не отправляем, объясняем
async def kw_stop(*a, **k):
    raise fl_submit.NotReady("Kwork подсветил в тексте контакты/стоп-слова и не пропустит предложение")

o = order("kw2", source="kwork")
cb, st, text, kw, n = run("kw2", submit=kw_stop, confirm=False)
check("Kwork: стоп-слова — отправки нет, причина названа", st == "new" and "стоп-слова" in text)

# 7. защита от двойной отправки: уже «отправляется» — биржу не трогаем
o = order("104")
c.execute("update drafts set status='sending' where order_id='104'"); c.commit()
cb, st, text, kw, n = run("104", submit=good)
check("двойное нажатие: уже отправляется — отправка не запускается, пользователю — подсказка", n == 0 and "Уже" in str(cb.alerts))

# 8. отклик «уже был» (форма пропала, но на бирже он есть) — не дублируем
async def already(*a, **k):
    return {"ok": True, "left": 3, "competitors": 1, "already": True}

o = order("105")
cb, st, text, kw, n = run("105", submit=already)
check("отклик уже был — об этом сказано, второй не отправляется", st == "sent_fl" and "уже был" in text)

# 9. настоящие контакты в тексте — не отправляем; «Telegram»/«почта» как слова — отправляем
o = order("106")
c.execute("update drafts set draft_json=? where order_id='106'",
          (__import__("json").dumps({**DRAFT, "reply": "Сделаю бота. Пишите @tctravel или +7 913 123-45-67. " * 4}),)); c.commit()
cb, st, text, kw, n = run("106", submit=good)
check("ник и телефон в тексте — отправки нет, контакты названы", n == 0 and "@tctravel" in text and "Не отправляю" in text)
o = order("kw3", source="kwork")
c.execute("update drafts set draft_json=? where order_id='kw3'",
          (__import__("json").dumps({**DRAFT, "reply": "Заявки придут вам в Telegram или на почту, подключу WhatsApp. " * 4}),)); c.commit()
cb, st, text, kw, n = run("kw3", submit=kw_good)
check("слова Telegram/почта/WhatsApp — не контакты, предложение уходит", n == 1 and st == "sent_kw")
check("в тексте ошибки нет двойной точки", ".." not in run("103", submit=refuse, confirm=False)[2])

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
