"""Офлайн-проверки fl_watch без нейросети и сети: разбор RSS FL.ru, проверка черновиков, карточка для Telegram.

    python tests/test_fl_watch.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import fl_watch as fw  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✅" if cond else "❌"), name)


RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>Разработка сайта (Бюджет: 600 000  &#8381;)</title>
<link>https://www.fl.ru/projects/5524579/razrabotka.html</link><description>Нужен &lt;b&gt;сайт&lt;/b&gt;  по ТЗ</description>
<category>Сайты / Laravel</category><pubDate>Tue, 06 Oct 2026 11:05:18 GMT</pubDate></item>
<item><title>Тестирование приложения (Бюджет: 10 000  &#8381;, для всех)</title>
<link>https://www.fl.ru/projects/5524600/test.html</link><description>текст</description>
<category>Программирование / Google Android</category><pubDate>Wed, 07 Oct 2026 04:57:00 GMT</pubDate></item>
<item><title>Бот для записи (для всех)</title>
<link>https://www.fl.ru/projects/5524601/bot.html</link><description>бот</description>
<category>Мессенджеры / Telegram</category><pubDate>Wed, 07 Oct 2026 05:00:00 GMT</pubDate></item>
<item><title>Сайт (по ТЗ) для компании</title>
<link>https://www.fl.ru/projects/5524602/site.html</link><description>x</description>
<category>Сайты / Тильда</category><pubDate>Wed, 07 Oct 2026 05:01:00 GMT</pubDate></item>
<item><title>Не заказ</title><link>https://www.fl.ru/blogs/1/</link></item>
</channel></rss>"""

orders = fw.parse_feed(RSS)
by = {o.id: o for o in orders}
check(f"разобрано 4 заказа, ссылка не на проект пропущена ({len(orders)})", len(orders) == 4)
check("бюджет 600 000 из «Бюджет: 600 000 ₽»", by["5524579"].budget == 600000)
check("заголовок без хвоста про бюджет", by["5524579"].title == "Разработка сайта")
check("«Бюджет …, для всех» → бюджет 10 000 и для всех", by["5524600"].budget == 10000 and by["5524600"].for_all)
check("«(для всех)» без бюджета", by["5524601"].budget is None and by["5524601"].for_all and by["5524601"].title == "Бот для записи")
check("скобки в середине заголовка не трогаются", by["5524602"].title == "Сайт (по ТЗ) для компании" and not by["5524602"].for_all)
check("HTML в описании раскодирован", "<b>сайт</b>" in by["5524579"].desc)

good = {"reply": "Здравствуйте! Сделаю сайт на Tilda: соберу структуру, адаптив и формы, покажу макет на согласование. "
                 "Беру по сниженной цене — набираю первые отзывы на FL.ru. Срок 5 дней. Есть ли у вас тексты и фото?",
        "price": 5000, "days": 5}
check("нормальный черновик проходит проверку", fw.draft_problems(good) == [])
for text, why in [("Делал похожие сайты много раз.", "выдуманный опыт"), ("Евгений, задача понятна.", "обращение к себе"),
                  ("Пишите в телеграм @jg_dev_bot.", "контакт"), ("Звоните +7 913 123-45-67.", "телефон"),
                  ("Нужна предоплата 50% на карту.", "предоплата")]:
    check(f"ловит: {why}", fw.draft_problems({**good, "reply": good["reply"] + " " + text}))
check("«сделаю» не считается «делал»", not fw.draft_problems({**good, "reply": good["reply"] + " Сделаю аккуратно."}))
check("нет цены → проблема", "нет цены" in fw.draft_problems({**good, "price": None}))

c = fw.card(by["5524579"], {"learn": "Laravel", "big": True}, good)
check("карточка: заголовок, бюджет, освоить, крупный, черновик", all(x in c for x in
      ["Разработка сайта", "600 000 ₽", "освоить: Laravel", "крупный проект", "5 000 ₽", "<code>"]))
evil = fw.Order(id="1", title="<script>x</script>", link="https://www.fl.ru/projects/1/", desc="a & b", category="Сайты",
                budget=None, for_all=False, published=by["5524579"].published)
check("HTML в заказе экранирован (Telegram не сломается)", "&lt;script&gt;" in fw.card(evil, {}, good))
check("карточка не длиннее лимита Telegram", len(fw.card(by["5524579"], {}, {**good, "reply": "x" * 9000})) <= 4096)

# хранение черновиков: по номеру сообщения находим заказ, правка увеличивает версию
import tempfile  # noqa: E402

fw.DB = Path(tempfile.mkdtemp()) / "t.db"
c = fw.db()
o = by["5524601"]
fw.save_draft(c, o, {"learn": ""}, good, 111)
check("заказ находится по номеру сообщения", fw.order_by_msg(c, 111) == o.id)
o2, tri2, d2, st, ver = fw.load_draft(c, o.id)
check("заказ восстановлен из базы целиком", o2 == o and d2 == good and st == "new" and ver == 1)
fw.save_draft(c, o, {"learn": ""}, {**good, "price": 3000}, 222)
_, _, d3, _, ver3 = fw.load_draft(c, o.id)
check("правка: новая версия, старое сообщение тоже ведёт к заказу",
      ver3 == 2 and d3["price"] == 3000 and fw.order_by_msg(c, 111) == o.id and fw.order_by_msg(c, 222) == o.id)
k = fw.keyboard(o.id, o.link)
check("кнопки: Править, Утвердить, Открыть заказ",
      [b["text"] for row in k["inline_keyboard"] for b in row] == ["✏️ Править", "✅ Утвердить", "Открыть заказ на FL.ru"])
check("после утверждения — только «Открыть заказ»", len(fw.keyboard(o.id, o.link, "approved")["inline_keyboard"]) == 1)
check("callback_data укладывается в лимит Telegram (64 байта)",
      all(len(b.get("callback_data", "").encode()) <= 64 for row in k["inline_keyboard"] for b in row))
check("карточка версии 2 помечена", "версия 2" in fw.card(o, {}, good, 2))
check("parse_draft разбирает ответ модели",
      fw.parse_draft("ОТКЛИК:\nТекст отклика\nЦЕНА: 3 500 ₽\nСРОК: 4 дн.") == {"reply": "Текст отклика", "price": 3500, "days": 4, "works": []})

# Kwork: заказы из JSON страницы биржи
import kwork_watch as kw  # noqa: E402

KW = ('<script>var x = {"wants":[{"id":3264640,"name":"Сайт &laquo;под ключ&raquo;","description":"Нужен сайт.\\r\\nWordPress",'
      '"priceLimit":"80000.00","possiblePriceLimit":200000,"kwork_count":9,"category_id":"37",'
      '"date_create":"2026-10-07 12:36:34","date_active":"2026-10-07 12:37:46"}],'
      '"categories":{"11":{"name":"Разработка и IT","cats":[{"CATID":"37","name":"Создание сайта"}]}}};</script>')
kwo = kw.parse_page(KW, kw.category_names(KW))
check("Kwork: заказ разобран", len(kwo) == 1)
k0 = kwo[0]
check("Kwork: id с префиксом kw, ссылка на /view", k0.id == "kw3264640" and k0.link.endswith("/projects/3264640/view"))
check("Kwork: бюджет, «готов до», конкуренты", (k0.budget, k0.max_budget, k0.competitors) == (80000, 200000, 9))
check("Kwork: время МСК → UTC", k0.published.hour == 9 and k0.published.minute == 37)
check("Kwork: HTML-сущности и переносы", k0.title == "Сайт «под ключ»" and k0.desc == "Нужен сайт.\nWordPress")
check("Kwork: раздел с названием", k0.category == "Kwork / Разработка и IT / Создание сайта")
cardk = fw.card(k0, {}, good)
check("Kwork: карточка с площадкой, «готов до», откликами", all(x in cardk for x in ["[Kwork]", "готов до 200 000 ₽", "откликов: 9"]))
check("Kwork: кнопка «Открыть заказ на Kwork»", fw.keyboard(k0.id, k0.link)["inline_keyboard"][1][0]["text"] == "Открыть заказ на Kwork")
check("Kwork: в промпте площадка Kwork, не FL.ru", "Kwork" in fw.draft_system(k0) and "отзывы на FL.ru" not in fw.draft_system(k0))
LOGO = KW.replace('"wants":[', '"wants":[{"id":1,"name":"Нарисовать логотип","description":"x","priceLimit":"1000","category_id":"28",'
                  '"date_create":"2026-10-07 12:36:34"},{"id":2,"name":"Баннер","description":"x","priceLimit":"1000",'
                  '"category_id":"25","date_create":"2026-10-07 12:36:34"},{"id":3,"name":"Перевести картинку в вектор SVG",'
                  '"description":"x","priceLimit":"1000","category_id":"68","date_create":"2026-10-07 12:36:34"},')
check("Kwork: логотипы и вектор отсеяны, сайт с «логотип» в описании остался",
      [o.id for o in kw.parse_page(LOGO.replace("Нужен сайт.", "Нужен сайт, логотип есть."), {})] == ["kw3264640"])
check("старый заказ без новых полей читается из базы", fw.order_from_json(fw.order_to_json(by["5524601"])).source == "fl")

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
