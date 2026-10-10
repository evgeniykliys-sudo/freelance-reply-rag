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
import kwork_submit as ks  # noqa: E402

TERMS = ["1 день", "2 дня", "5 дней", "7 дней", "10 дней", "2 недели", "3 недели", "1 месяц", "2 месяца"]
check("Kwork срок: 8 дней → ближайший больший «10 дней», 12 → «2 недели», 90 → «2 месяца»",
      (ks.pick_term(TERMS, 8), ks.pick_term(TERMS, 12), ks.pick_term(TERMS, 90)) == ("10 дней", "2 недели", "2 месяца"))
check("Kwork цена подгоняется под рамки заказа",
      (ks.fit_price(1500, {"min": 2000, "max": 30000}), ks.fit_price(50000, {"min": 2000, "max": 30000}),
       ks.fit_price(8500, {"min": 2000, "max": None})) == (2000, 30000, 8500))
check("Kwork: номер проекта из ссылки", ks.project_id("https://kwork.ru/projects/3264720/view") == "3264720")
check("ловит «прямого опыта с VK API пока не было»",
      fw.draft_problems({**good, "reply": good["reply"] + " Прямого опыта с VK API пока не было."}))

# вложения: Word и Excel → текст без сторонних библиотек
import zipfile  # noqa: E402

import attachments as at  # noqa: E402

tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(tmp / "ТЗ.docx", "w") as z:
    z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Бот для записи</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Услуг</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>15</w:t></w:r></w:p>'
               '</w:tc></w:tr></w:tbl><w:p><w:r><w:t xml:space="preserve">Срок &amp; цена</w:t></w:r></w:p></w:body></w:document>')
with zipfile.ZipFile(tmp / "смета.xlsx", "w") as z:
    z.writestr("xl/sharedStrings.xml", "<sst><si><t>Позиция</t></si><si><t>Кол-во</t></si></sst>")
    z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
               '<row><c t="inlineStr"><is><t>Кнопка</t></is></c><c><v>3</v></c></row></sheetData></worksheet>')
(tmp / "архив.rar").write_bytes(b"Rar!")
dt = at.docx_text(tmp / "ТЗ.docx")
check("Word: абзацы, таблица и сущности", all(x in dt for x in ["Бот для записи", "Услуг", "15", "Срок & цена"]))
check("Excel: общие строки и числа", "Позиция | Кол-во" in at.xlsx_text(tmp / "смета.xlsx") and "Кнопка | 3" in at.xlsx_text(tmp / "смета.xlsx"))
bl, rd, sk = at.to_blocks(sorted(tmp.iterdir()))
check("в блоки попали Word и Excel, rar пропущен с причиной",
      sorted(rd) == ["ТЗ.docx", "смета.xlsx"] and sk == ["архив.rar (формат .rar не читаю)"] and all(b["type"] == "text" for b in bl))
check("Kwork: заказ дешевле 2 500 ₽ отсеян, ровно 2 500 — остаётся",
      [kw.skipped({"name": "Бот", "category_id": "41", "priceLimit": p}) for p in ("2000.00", "2500.00")] == [True, False])
(tmp / "фото.png").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 50)       # JPEG под видом .png — как прислал заказчик
(tmp / "не-картинка.jpg").write_bytes(b"<html>")
bl2, rd2, sk2 = at.to_blocks([tmp / "фото.png", tmp / "не-картинка.jpg"])
check("картинка: тип по содержимому, а не по расширению; не-картинка пропущена",
      [b["source"]["media_type"] for b in bl2 if b["type"] == "image"] == ["image/jpeg"]
      and sk2 == ["не-картинка.jpg (не картинка, хотя так назван)"])
check("FL: ссылки на вложения со страницы заказа",
      at.fl_links('<div class="base-attach-class"><a class="x"href=\'https://www.fl.ru/download/files/a/projects/f_1.docx\' '
                  'target="_blank">ТЗ &amp; макет.docx</a><a href="https://www.fl.ru/download/files/b/f_2.jpg">фото.jpg</a>'
                  '</div><a href="https://www.fl.ru/about/x.pdf">Правила</a>')
      == [{"name": "ТЗ & макет.docx", "url": "https://www.fl.ru/download/files/a/projects/f_1.docx"},
          {"name": "фото.jpg", "url": "https://www.fl.ru/download/files/b/f_2.jpg"}])
import dataclasses  # noqa: E402

lay = dataclasses.replace(k0, category="Kwork / Разработка и IT / Верстка")
check("Kwork: в рубрике без портфолио — предупреждение, в «Создание сайта» и у ботов — нет",
      "нет портфолио в рубрике «Верстка»" in fw.card(lay, {}, good)
      and "нет портфолио" not in fw.card(k0, {}, good)
      and "нет портфолио" not in fw.card(dataclasses.replace(k0, category="Kwork / Разработка и IT / Скрипты, боты и mini apps"), {}, good))
from datetime import datetime as _dt, timezone  # noqa: E402

nsk = lambda h, m=0: _dt(2026, 10, 7, h, m, tzinfo=fw.NSK)  # noqa: E731
check("рабочие часы 8–22 по Новосибирску", [fw.in_work_hours(nsk(h)) for h in (7, 8, 21, 22, 23)] == [False, True, True, False, False])
check("до конца дня из 21:30 — полчаса", fw.seconds_until_end(nsk(21, 30)) == 1800)
check("время с другим поясом переводится в Новосибирск", fw.in_work_hours(_dt(2026, 10, 7, 1, 0, tzinfo=timezone.utc)))  # 08:00 НСК
lk = fw.single_instance(47299)
check("второй экземпляр бота не запускается", lk is not None and fw.single_instance(47299) is None)
lk.close()
t15 = fw.fit_days({"reply": "Сделаю за 7 дней, цена 15 000 ₽.", "price": 15000, "days": 7})
check("15 000 ₽ · 7 дн. → 2 дня, и в тексте тоже", t15["days"] == 2 and "за 2 дня" in t15["reply"] and "7 дней" not in t15["reply"])
check("40 000 ₽ · 14 дн. → 7; 30 000 ₽ · 5 дн. не трогаем",
      fw.fit_days({"reply": "x", "price": 40000, "days": 14})["days"] == 7 and fw.fit_days({"reply": "x", "price": 30000, "days": 5})["days"] == 5)
check("дороже 40 000 ₽ — срок по объёму, без потолка", fw.fit_days({"reply": "x", "price": 60000, "days": 12})["days"] == 12)
check("имя файла чистится от запрещённых символов", at.safe_name("ТЗ%20v1/2:итог?.docx") == "ТЗ v1_2_итог_.docx")
check("Kwork: вложения из JSON попадают в заказ",
      kw.parse_page(KW.replace('"date_active"', '"files":[{"fname":"ТЗ.docx","url":"https://kwork.ru/files/x/ТЗ.docx"}],"date_active"'),
                    {})[0].files == [{"name": "ТЗ.docx", "url": "https://kwork.ru/files/x/ТЗ.docx"}])
check("старый заказ без новых полей читается из базы", fw.order_from_json(fw.order_to_json(by["5524601"])).source == "fl")

split = "За 6 дней соберу базовую версию — остальное (блог, почта) добьём следующим этапом отдельным бюджетом. "
check("часть ТЗ «следующим этапом отдельным бюджетом» — подсвечивается",
      any("не просил" in p for p in fw.draft_problems({"reply": split * 3, "price": 30000, "days": 6})))
check("этапы сдачи внутри одной цены — без замечаний",
      not any("не просил" in p for p in fw.draft_problems(
          {"reply": "Всё ТЗ — 75 000 ₽ за 12 дней: к 5-му дню каталог и калькулятор, к 12-му блог и перенос. " * 3,
           "price": 75000, "days": 12})))
mvp = by["5524601"]
mvp.desc = "Сначала нужен MVP за небольшой бюджет, дальше посмотрим и доработаем."
check("заказчик сам просит MVP — отдельный этап без замечания",
      not any("этап" in p for p in fw.draft_problems({"reply": split * 3, "price": 30000, "days": 6}, mvp)))

import asyncio  # noqa: E402

cheap = dataclasses.replace(by["5524601"], id="cheap1", budget=500, published=_dt.now(timezone.utc))
c.execute("insert or replace into seen values ('x', 'x', 'old')"); c.commit()       # не «первый запуск»


async def _fetch(_):
    return [cheap]

res = asyncio.run(fw.run_once(None, None, dry=True, fetch=_fetch))      # client=None: до нейросети дойти не должно
check("FL.ru: бюджет 500 ₽ < минималки 2500 — пропуск без нейросети",
      res and res[0][2] is None and fw.is_seen(c, "cheap1"))
check("минимальная цена в отклике не ниже минималки", fw.MIN_PRICE >= fw.MIN_BUDGET == 2500)

import time  # noqa: E402

_tri, _draft = fw.triage, fw.draft
fw.triage = lambda client, o: {"take": True}
fw.draft = lambda o: (time.sleep(1), {"reply": "x" * 200, "price": 5000, "days": 1, "works": []})[1]
pair = [dataclasses.replace(k0, id=f"kwpar{i}", budget=5000, files=[], published=_dt.now(timezone.utc)) for i in (1, 2)]


async def _fetch2(_):
    return pair

t0 = time.time()
res = asyncio.run(fw.run_once(None, None, dry=True, fetch=_fetch2))
check("два заказа разбираются параллельно, а не друг за другом", len(res) == 2 and time.time() - t0 < 1.8)
fw.triage, fw.draft = _tri, _draft

class _Fake:
    """Клиент-заглушка: отвечает заданным JSON, как сортировщик."""
    def __init__(self, text):
        self.messages = self
        self.text = text

    def create(self, **kw):
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": self.text})()]})()

sus = fw.triage(_Fake('{"take": false, "reason": "задания за скриншоты", "risk": "похоже на накрутку"}'), by["5524601"])
hard = fw.triage(_Fake('{"take": false, "reason": "вакансия в штат", "risk": ""}'), by["5524601"])
check("подозрение — карточка всё равно придёт (take=true с пометкой), явный отказ — нет",
      sus["take"] and sus["risk"] and not hard["take"])
check("пометка о сомнительной схеме — в карточке",
      "похоже на сомнительную схему" in fw.card(by["5524601"], {"risk": "задания за скриншоты"}, good))

nb = by["5524601"]
check("проектирование и чертежи — не берём (рубрика или слова), сайт для архитекторов и «пришлите ПДФ» — берём",
      fw.narrow(dataclasses.replace(nb, title="Разработка раздела АР", desc="раздел АР ПСД", category="Дизайн / Архитектура"))
      and fw.narrow(dataclasses.replace(nb, title="Чертежи", desc="Перечертить план в AutoCAD", category="Дизайн / Чертежи"))
      and not fw.narrow(dataclasses.replace(nb, title="Сайт для АРХИТЕКТУРНОГО бюро", desc="МАРКЕТИНГ", category="Сайты / Лендинги"))
      and not fw.narrow(dataclasses.replace(nb, title="Логотип", desc="пришлите ПДФ", category="Дизайн / Логотипы")))

ok_reply = "Примеры работ: прикрепил сайт на Tilda — похож структурой и заявками. " * 3
check("пункт заказчика без ответа — в замечаниях",
      any("Примеры" in p for p in fw.draft_problems({"reply": ok_reply, "price": 9000, "days": 2,
                                                       "unanswered": ["Примеры аналогичных сайтов"]})))
check("«прямого опыта … нет, честно скажу» — ловится",
      len(fw.draft_problems({"reply": "Прямого опыта на WordPress для инженерной компании в портфолио нет, честно скажу. " * 3,
                             "price": 9000, "days": 2})) >= 2)
check("длинный отклик на FL.ru без «Kwork обрежет»",
      not any("Kwork" in p for p in fw.draft_problems({"reply": ok_reply * 12, "price": 9000, "days": 2}, by["5524601"])))

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
