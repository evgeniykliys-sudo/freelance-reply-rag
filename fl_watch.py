"""Слежение за новыми заказами FL.ru → Telegram с готовым черновиком отклика.

Источник — официальные RSS-ленты FL.ru по разделам (без входа в аккаунт и без парсинга страниц).
Каждый новый заказ:
  1) Haiku быстро решает: это IT/диджитал-заказ, за который можно взяться? (дёшево, отсекает мусор);
  2) для подходящих Sonnet пишет черновик отклика по базе услуг (rag.py) в режиме набора первых отзывов:
     цена ниже рынка, честно «беру дешевле — набираю отзывы»;
  3) в Telegram приходит карточка: раздел, бюджет, доступ, черновик, цена, «что освоить», кнопка «Открыть заказ».

    python fl_watch.py --once     # один проход (проверка без Telegram: --dry)
"""
import argparse
import asyncio
import email.utils
import html
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import anthropic
from anthropic import Anthropic

import attachments
import fl_submit
import rag

log = logging.getLogger("fl_watch")
DB = Path(__file__).parent / "fl_seen.db"
UA = "Mozilla/5.0 (fl-watch; personal order notifier)"
# Разделы FL.ru (номер → для справки). Всё, что делается на компьютере и связано с IT/диджиталом.
CATEGORIES = {2: "Сайты", 3: "Дизайн", 5: "Программирование", 30: "Маркетплейс менеджмент", 31: "AI",
              32: "Социальные сети", 34: "Мессенджеры", 35: "Рисунки и иллюстрации", 36: "Mobile",
              37: "Браузеры", 40: "Интернет-магазины", 41: "Автоматизация бизнеса", 42: "Фирменный стиль"}
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL") or "claude-haiku-5-5"
# проектирование и узкие специальности — не берём (решение Евгения): по рубрике и по словам в заказе, без нейросети
NARROW_RUBRICS = {"Архитектура", "Интерьеры", "Ландшафтный дизайн", "Промышленный дизайн", "Инжиниринг"}
NARROW_WORDS = (r"\bПСД\b|\bПД\b|\bРД\b|проектн\w* документац|рабоч\w* документац|\bраздел\w* (АР|КР|ОВ|ВК|ЭОМ|ГП|ПОС)\b|"
                r"\b(АР|КР|ОВиК|ЭОМ)\b|чертеж|чертёж|AutoCAD|Автокад|Revit|ArchiCAD|Renga|nanoCAD|SolidWorks|Компас-?3D|"
                r"конструкторск\w* документац|архитектурн\w* проект|проектирован\w* (здани|дом|сетей|инженерн|вентиляц|отоплен|электросна)")


def narrow(o: "Order") -> bool:
    return o.category.rsplit(" / ", 1)[-1] in NARROW_RUBRICS or bool(re.search(NARROW_WORDS, f"{o.title} {o.desc}"))
# Haiku 5.5 по умолчанию «думает» перед ответом — для коротких JSON-ответов это лишние токены и обрезанный ответ
NO_THINKING = {"type": "disabled"}
DISCOUNT = int(os.getenv("DISCOUNT_PCT") or 40)        # насколько ниже рынка/прайса предлагать на старте
# минималка Евгения для обеих бирж: заказы с бюджетом ниже — не берём, и сами дешевле не предлагаем
MIN_BUDGET = int(os.getenv("MIN_BUDGET") or os.getenv("KWORK_MIN_BUDGET") or 2500)
MIN_PRICE = int(os.getenv("MIN_PRICE") or MIN_BUDGET)
# заказы старше этого возраста не берём: на них уже набралось откликов (решение Евгения — 2 часа)
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS") or 2)
# рабочие часы бота по Новосибирску (решение Евгения): ночью не следим, не тратим API и не будим уведомлениями
WORK_HOURS = os.getenv("WORK_HOURS") or "8-22"
NSK = timezone(timedelta(hours=7))


def _work_bounds() -> tuple[int, int]:
    start, end = (int(x) for x in WORK_HOURS.split("-"))
    return start, end


def in_work_hours(now: datetime | None = None) -> bool:
    start, end = _work_bounds()
    return start <= (now or datetime.now(NSK)).astimezone(NSK).hour < end


def seconds_until_end(now: datetime | None = None) -> float:
    """Сколько секунд до конца рабочего дня (22:00 по Новосибирску)."""
    now = (now or datetime.now(NSK)).astimezone(NSK)
    end = now.replace(hour=_work_bounds()[1], minute=0, second=0, microsecond=0)
    return max(0.0, (end - now).total_seconds())


def single_instance(port: int = int(os.getenv("BOT_LOCK_PORT") or 47231)):
    """Один бот на ПК: Планировщик может запустить его и в 8:00, и при входе в Windows — второй сразу выходит.
    Держим занятым локальный порт: он освобождается сам, даже если процесс упал."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s
# Kwork требует портфолио только в этих рубриках (для ботов и скриптов хватает кворков);
# где работы уже есть — перечислить через запятую в KWORK_PORTFOLIO
KWORK_NEED_PORTFOLIO = {"Создание сайта", "Верстка", "Мобильные приложения", "Игры"}
def kwork_have_portfolio() -> set[str]:
    """Читаем при каждой карточке: .env загружается в bot.py уже после импорта этого модуля."""
    return {x.strip() for x in (os.getenv("KWORK_PORTFOLIO") or "Создание сайта").split(",") if x.strip()}


@dataclass
class Order:
    id: str
    title: str
    link: str
    desc: str
    category: str
    budget: int | None
    for_all: bool
    published: datetime
    source: str = "fl"                  # fl | kwork
    competitors: int | None = None      # Kwork: сколько предложений уже подано
    max_budget: int | None = None       # Kwork: до какой суммы заказчик готов поднять цену
    files: list | None = None           # вложения [{name, url}] (у Kwork — из списка, у FL.ru — со страницы)
    att: dict | None = None             # что вышло со скачиванием: {"read": [...], "skipped": [...], "error": ...}

    @property
    def platform(self) -> str:
        return "Kwork" if self.source == "kwork" else "FL.ru"


def parse_feed(xml_text: str) -> list[Order]:
    out = []
    for it in ET.fromstring(xml_text).findall(".//item"):
        link = it.findtext("link") or ""
        m = re.search(r"/projects/(\d+)/", link)
        if not m:
            continue
        raw = html.unescape(it.findtext("title") or "").replace("\xa0", " ")
        budget = re.search(r"Бюджет:\s*([\d\s]+)\s*₽", raw)
        tail = re.search(r"\s*\(([^()]*(?:Бюджет|для всех)[^()]*)\)\s*$", raw)
        title = raw[: tail.start()].strip() if tail else raw.strip()
        desc = re.sub(r"\s+", " ", html.unescape(it.findtext("description") or "")).strip()
        out.append(Order(id=m.group(1), title=title, link=link, desc=desc, category=it.findtext("category") or "",
                         budget=int(budget.group(1).replace(" ", "")) if budget else None,
                         for_all="для всех" in (tail.group(1) if tail else ""),
                         published=email.utils.parsedate_to_datetime(it.findtext("pubDate"))))
    return out


async def fetch_all(session: aiohttp.ClientSession) -> list[Order]:
    orders: dict[str, Order] = {}
    for cat in CATEGORIES:
        try:
            async with session.get(f"https://www.fl.ru/rss/all.xml?category={cat}") as r:
                if r.status != 200:
                    log.warning("RSS %s: HTTP %s", cat, r.status)
                    continue
                for o in parse_feed(await r.text()):
                    orders.setdefault(o.id, o)
        except (aiohttp.ClientError, TimeoutError, ET.ParseError) as e:
            log.warning("RSS %s: %s", cat, e)
    return sorted(orders.values(), key=lambda o: o.published)


async def fetch_full_desc(session: aiohttp.ClientSession, o: Order) -> str:
    """RSS отдаёт только начало описания — полное берём со страницы заказа (JSON внутри HTML, вход не нужен).
    Без этого черновик отвечал на половину задачи: вторая часть ТЗ (объёмы, сопровождение) терялась."""
    try:
        async with session.get(o.link) as r:
            if r.status != 200:
                return o.desc
            page = await r.text()
    except (aiohttp.ClientError, TimeoutError):
        return o.desc
    best = o.desc
    for m in re.finditer(r'"description":"((?:[^"\\]|\\.)*)"', page):
        try:
            text = json.loads('"' + m.group(1) + '"').strip()
        except json.JSONDecodeError:
            continue
        if len(text) > len(best):
            best = text
    return best


# ---------- хранилище: что уже видели ----------
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("create table if not exists seen (id text primary key, at text, verdict text)")
    # черновики, отправленные в Telegram: по номеру сообщения находим заказ, чтобы править/утверждать
    c.execute("""create table if not exists drafts (order_id text primary key, order_json text, tri_json text,
                 draft_json text, status text default 'new', versions integer default 1)""")
    c.execute("create table if not exists msgs (msg_id integer primary key, order_id text)")
    return c


def order_to_json(o: Order) -> str:
    return json.dumps({**o.__dict__, "published": o.published.isoformat()}, ensure_ascii=False)


def order_from_json(t: str) -> Order:
    d = json.loads(t)
    return Order(**{**d, "published": datetime.fromisoformat(d["published"])})


def save_draft(c, o: Order, tri: dict, d: dict, msg_id: int | None):
    c.execute("""insert into drafts(order_id, order_json, tri_json, draft_json) values (?,?,?,?)
                 on conflict(order_id) do update set draft_json=excluded.draft_json, order_json=excluded.order_json,
                 tri_json=excluded.tri_json, versions=versions+1, status='new'""",
              (o.id, order_to_json(o), json.dumps(tri, ensure_ascii=False), json.dumps(d, ensure_ascii=False)))
    if msg_id:
        c.execute("insert or replace into msgs values (?,?)", (msg_id, o.id))
    c.commit()


def load_draft(c, order_id: str):
    r = c.execute("select order_json, tri_json, draft_json, status, versions from drafts where order_id=?", (order_id,)).fetchone()
    if not r:
        return None
    return order_from_json(r[0]), json.loads(r[1]), json.loads(r[2]), r[3], r[4]


def order_by_msg(c, msg_id: int) -> str | None:
    r = c.execute("select order_id from msgs where msg_id=?", (msg_id,)).fetchone()
    return r[0] if r else None


def is_seen(c, oid) -> bool:
    return c.execute("select 1 from seen where id=?", (oid,)).fetchone() is not None


def mark(c, oid, verdict):
    c.execute("insert or replace into seen values (?,?,?)", (oid, datetime.now().isoformat(timespec="seconds"), verdict))
    c.commit()


# ---------- 1. сортировка (Haiku) ----------
TRIAGE = """Ты отбираешь заказы с FL.ru для фрилансера-новичка. Он берётся за ЛЮБУЮ IT/диджитал-работу, которую можно сделать
удалённо на компьютере (сайты, Tilda/WordPress, боты, скрипты, парсеры, AI, автоматизация, интеграции, таблицы,
дизайн и графика с помощью нейросетей и Figma/Canva, презентации, простые приложения), и учится по ходу.
Ответь ТОЛЬКО JSON без пояснений вокруг:
{"take": true|false, "reason": "до 12 слов", "learn": "что освоить, до 12 слов, или пусто", "big": true|false,
 "risk": "чем подозрителен, до 15 слов, или пусто"}
take=false ТОЛЬКО если: не IT (офлайн, выезд, звонки, физический труд); нужны десятки живых людей/аккаунтов;
ПРЯМО сказано про накрутки, фейковые отзывы, подписки, спам, обход защит; учебные работы за студента; вакансия в
штат/на полный день; проектирование (здания, инженерные сети, конструкторская документация, чертежи) и работа для
узкого специалиста с профильной квалификацией или допуском (юрист, бухгалтер, врач, инженер-конструктор, сметчик ПСД).
Если заказ только ПОХОЖ на сомнительную схему (задания за скриншоты, «верификация» исполнителей, раздача заданий
без пояснения, зовут срочно связаться вне биржи), но прямо этого не сказано — take=true и заполни risk: решит человек.
risk — ТОЛЬКО подозрение на нечестную схему (накрутки, обман людей, мошенничество). Бюджет, объём, сложность, тема,
юридические тонкости — это НЕ risk, оставь пусто. У обычного заказа risk пустой.
Отсутствие бюджета, ТЗ во вложении, незнакомая технология — НЕ причина отказа (take=true, укажи learn).
big=true — проект явно крупный для одного человека (больше ~2 недель работы)."""


def triage(client: Anthropic, o: Order) -> dict:
    r = client.messages.create(model=TRIAGE_MODEL, max_tokens=400, system=TRIAGE, thinking=NO_THINKING, messages=[{"role": "user", "content":
        f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\nЗаголовок: {o.title}\nОписание: {o.desc[:4000]}"}])
    text = "".join(b.text for b in r.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        tri = json.loads(m.group(0))
        if not tri.get("take") and tri.get("risk"):
            # только подозрение — не отбрасываем молча: карточка придёт с пометкой, решает Евгений
            tri["take"] = True
        return tri
    except (AttributeError, json.JSONDecodeError):
        # лучше показать лишний заказ, чем потерять подходящий
        return {"take": True, "reason": "сортировка не разобралась — проверь сам", "learn": "", "big": False}


# ---------- 2. черновик отклика (Sonnet + база услуг) ----------
DRAFT_SYSTEM = f"""Ты пишешь отклик на заказ FL.ru от имени Евгения — фрилансера-новичка на FL.ru (Python, Telegram-боты,
AI-автоматизация, n8n, парсеры; за сайты и дизайн берётся с помощью конструкторов, Figma/Canva и нейросетей).
Цель сейчас — первые заказы и отзывы, поэтому откликаемся почти на всё.

Правила:
1. НИКАКОГО ВЫДУМАННОГО ОПЫТА. Запрещены фразы вида «делал похожее», «работаю в … не первый раз», «уже настраивал»,
   если такого проекта нет в переданных фрагментах портфолио. Можно сослаться только на проект/ссылку из фрагментов,
   и только если он реально похож. Если похожего нет — покажи компетентность ПЛАНОМ: как сделаешь (инструменты, шаги,
   как проверишь результат). Не пиши и обратного («опыта нет», «честно скажу, не делал») — просто план.
1а. Отклик пишешь ТЫ (Евгений) заказчику. Не обращайся к заказчику по имени Евгений и вообще не называй имён,
   если заказчик сам не подписался в тексте заказа.
2. Цена: на {DISCOUNT}% ниже рыночной для такой задачи (или ниже цены из базы, если услуга там есть), не ниже {MIN_PRICE} ₽.
   Если у заказа указан бюджет — предложи бюджет или немного ниже. Округляй до сотен.
   Цена — от рынка за ОБЪЁМ работы, а не от срока: сначала оцени, сколько такой объём стоит на бирже (например,
   информационный плакат/макет — несколько тысяч за штуку, лендинг — 15–30 тыс., корпоративный сайт на 5–15 страниц
   со структурой, ТЗ дизайнеру и SEO — 60–100 тыс., сайт с каталогом и админкой — от 50 тыс.), потом ставь срок. НИКОГДА не завышай цену, чтобы оправдать длинный срок.
   Одной фразой объясни: «беру по сниженной цене — набираю первые отзывы на FL.ru».
3. СРОК для заказчика так же важен, как цена: предлагай минимальный адекватный срок, без «запаса». Ориентиры:
   цена до 15 000 ₽ — 1–2 дня (1 день для простой задачи); до 40 000 ₽ — не больше 5–7 дней; дороже — по объёму
   всего ТЗ. Длинный срок за небольшую сумму отпугивает — так не пиши.
   Если в тексте отклика называешь срок — он должен совпадать со строкой СРОК.
   СРОК — одно целое число дней (не диапазон, не «2 недели»).
3г. ЦЕНА и СРОК — за ВСЁ, что есть в заказе и ТЗ, одним предложением. ЗАПРЕЩЕНО выносить часть ТЗ в «следующий
   этап», «отдельный бюджет», «доплату», «второй заказ» или «потом обсудим» — заказчик хочет одну цену за весь результат.
   Объём большой — не урезай состав: цена по рынку за весь объём (ориентир — бюджет заказчика, если указан), срок —
   минимальный реальный при плотной работе. Этапы можно назвать только как порядок сдачи внутри этой одной цены и одного срока («к 5-му дню — каталог
   и калькулятор, к 12-му — блог и перенос»).
   Исключение — когда этого требуют условия заказа: заказчик сам пишет про этапы с отдельной оплатой, MVP, первую
   очередь, «сначала X, дальше посмотрим», или его бюджет явно только на часть работ. Тогда отдельные этапы — нормально:
   ЦЕНА и СРОК — за первый этап, остальные этапы перечисли с ценой и сроком каждого.
3а. Прочитай ТЗ до конца. Если заказчик прямо спрашивает (цена, срок, сопровождение в месяц, гарантия, этапы) —
   ответь на КАЖДЫЙ вопрос явно и конкретной цифрой. Сопровождение — фиксированная сумма в месяц и что в неё входит
   (например: исправление ошибок, мелкие доработки до N часов в месяц, мониторинг работы). Никаких «обсудим по факту»,
   «по часам, договоримся». Коротко покажи, что учёл ключевые требования ТЗ (объёмы, ограничения, особые пожелания).
   Для большого ТЗ можно до 9 предложений.
3в. Сначала пойми, КОГО и ЗАЧЕМ ищет заказчик, и не своди заказ к его части. Частые случаи:
   — ищут человека на постоянку / «задач много» / «долгосрочно», а описанная задача — пример или ТЕСТОВОЕ для отбора.
     Тогда ПЕРВАЯ фраза отклика — про сотрудничество: готов брать их задачи постоянно, и почему подходишь под их
     поток задач (вайбкодинг с AI — быстро собираешь рабочие прототипы, AI-функции, боты, автоматизация).
     Тестовое — отдельно, с ценой и сроком; ЦЕНА и СРОК в ответе — за тестовое. Оценку всей задачи давай коротко,
     одной фразой и только если её просят — она не главное.
   — в заголовке/тексте есть «ИИ», «AI», «нейросеть» — ОБЯЗАТЕЛЬНО предложи конкретную AI-функцию для их задачи
     (например, для базы знаний: сотрудник спрашивает своими словами — AI отвечает по регламентам со ссылкой на пункт).
   Отклик обязан отражать КАЖДЫЙ пункт твоего разбора СУТЬ — перечитай разбор перед тем, как писать.
   Если в заказе есть список «От вас / В отклике напишите / Нужно указать» — ответь на КАЖДЫЙ пункт в том же порядке,
   каждый — отдельным абзацем, который начинается словами пункта («Примеры работ: …», «Работа с дизайнером: …»,
   «SEO-подготовка: …»), чтобы заказчик сразу нашёл ответ. Тогда правило 5 про 4–7 предложений не действует.
   Пункт про примеры/портфолио НЕ пропускай никогда: назови 1–2 самые близкие работы из списка портфолио (по названию,
   без ссылок), скажи, чем именно они похожи на их задачу (структура, заявки, адаптив…), и прикрепи их в РАБОТЫ —
   даже если похожи частично. Не пиши «опыта нет», «прямого опыта нет», «честно скажу» — только чем работа похожа.
   Вопрос «как организуете работу с …» — это порядок взаимодействия по шагам (кто что передаёт, когда согласуем,
   как проверяем), а не одна фраза «буду на связи».
3б. Если к заказу приложены файлы (ТЗ, таблицы, скриншоты, макеты) — это главный источник. Объём работ, этапы,
   СРОК и ЦЕНУ считай по ним, а не только по короткому тексту заказа: перечисли про себя все пункты ТЗ и оцени каждый.
   В отклике упомяни 1–2 конкретные детали из файлов — чтобы было видно, что ТЗ прочитано. Если в ТЗ есть вопросы
   к исполнителю — ответь на каждый. Если ТЗ на большой проект — распиши порядок сдачи (что к какому дню готово), но цена и срок — одни на всё ТЗ (3г).
4. Без контактов, мессенджеров, предоплаты вне FL.ru и БЕЗ ССЫЛОК (никаких github, сайтов, URL) — FL.ru их не любит.
   Примеры работ прикрепляются к отклику отдельно (строка РАБОТЫ), в тексте можно написать «прикрепил похожие работы».
5. Стиль: живо и по делу, без канцелярита. 4–7 предложений: приветствие по сути задачи → подтверждение (проект или
   план работы) → срок и цена → один уточняющий вопрос, самый важный для оценки.
   Не пересказывай заказчику свои правила («без деления на этапы с доплатой», «за 1–2 дня такое не сделать»,
   «цена за весь комплект») — пиши только суть: что сделаешь, к какому сроку, за сколько.
   Инструменты называй подходящие задаче: печать/полиграфия → векторный PDF под печать (CMYK, вылеты), Illustrator
   или CorelDRAW, а не Canva.
6. РАБОТЫ: номера до 3 работ портфолио из списка ниже, которые реально похожи на заказ или показывают нужный навык
   (сайт/лендинг → сайт на Tilda для ремонта квартир, AI-консультант на сайт, Mini App; логотип/дизайн → айдентика
   кафе; боты → боты; парсинг → парсеры). Если заказчик просит примеры — «РАБОТЫ: нет» нельзя. Если ничего не
   подходит — «РАБОТЫ: нет».
7. Ответ строго в формате (без markdown). СУТЬ — твой разбор для себя, заказчик его не увидит; отклик пиши по нему.
   СУТЬ — КОРОТКО, до 8 строк по одной фразе (она платная и заказчику не видна):
СУТЬ:
— кого ищут (разовая задача / постоянный исполнитель) и что для заказчика главное;
— что из текста тестовое или пример, а что основная работа;
— что заказчик прямо просит написать в отклике (каждый пункт);
— какие слова заказчика нельзя упустить (ИИ, сроки, ограничения);
— план отклика: о чём первая фраза, какая AI-идея (если уместно), что ответить на каждый пункт.
ОТКЛИК:
<текст>
ЦЕНА: <число> ₽
СРОК: <число> дн.
РАБОТЫ: <номера через запятую или нет>

Портфолио:
""" + fl_submit.portfolio_menu()


def draft_system(o: Order) -> str:
    """Тот же промпт, но с правильной площадкой («набираю отзывы на Kwork», правила Kwork)."""
    if o.source != "kwork":
        return DRAFT_SYSTEM
    extra = ("\n\nKwork: это предложение на бирже проектов Kwork. Заказчик видит десятки предложений за минуты — "
             "первая фраза должна сразу попасть в суть его задачи. Если указано «готов до N ₽», бюджет можно "
             "поднимать, но в режиме набора отзывов держись ближе к нижней границе.")
    return DRAFT_SYSTEM.replace("FL.ru", "Kwork") + extra


def posting_blocks(o: Order, intro: str, with_files: bool = True) -> list[dict]:
    """Текст заказа + скачанные вложения (ТЗ, таблицы, картинки) одним сообщением для модели."""
    posting = f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\n{o.title}\n\n{o.desc}"
    blocks = attachments.to_blocks(attachments.cached(o.id))[0] if with_files else []
    head = intro + f"Текст заказа:\n{posting}"
    if blocks:
        head += "\n\nК заказу приложены файлы — они ниже. Это часть ТЗ, учти их в отклике, цене и сроке."
    return [{"type": "text", "text": head}] + blocks


def draft(o: Order) -> dict:
    posting = f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\n{o.title}\n\n{o.desc}"
    chunks = rag.search(posting)[:6]
    context = "\n\n".join(f"[{c['type']}] {c['text']}" for c in chunks)
    intro = f"Фрагменты базы услуг/портфолио/шаблонов:\n\n{context}\n\n"
    messages = [{"role": "user", "content": posting_blocks(o, intro)}]
    for attempt in range(2):
        try:
            r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)),
                                                  messages=messages)
        except anthropic.BadRequestError:
            # модель не приняла вложение (битый файл, не тот формат) — пишем по тексту заказа, а не падаем
            # (иначе заказ не отмечается просмотренным и бот повторяет его каждый проход)
            if len(messages[0]["content"]) == 1:
                raise
            log.warning("заказ %s: вложения не приняты моделью, черновик без них", o.id)
            o.att = {**(o.att or {}), "read": [], "error": "нейросеть не приняла вложения — черновик по тексту заказа"}
            messages = [{"role": "user", "content": posting_blocks(o, intro, with_files=False)}]
            r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)),
                                                  messages=messages)
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        reply = re.search(r"ОТКЛИК:\s*(.*?)(?:\n\s*ЦЕНА:|\Z)", text, re.S)
        price = re.search(r"ЦЕНА:\s*([\d\s]+)", text)
        days = re.search(r"СРОК:\s*(\d+)", text)
        if reply and reply.group(1).strip() and price and days:
            break
        # модель ушла от формата (рассуждения, пустой ответ) — просим переписать строго по формату
        messages = messages + [{"role": "assistant", "content": text or "…"},
                               {"role": "user", "content": "Перепиши строго в формате: ОТКЛИК: … / ЦЕНА: … ₽ / СРОК: … дн. Без вступлений."}]
    d = parse_draft(text)
    cap = max_days(d.get("price"))
    if cap and d.get("days") and d["days"] > cap:
        # срок длиннее допустимого для такой цены — просим переписать целиком (этапы в тексте тоже),
        # а не подменяем цифру: иначе в тексте остаются этапы на неделю и «итого 2 дня»
        messages = messages + [{"role": "assistant", "content": text},
                               {"role": "user", "content":
                                f"Срок {d['days']} дн. за {d['price']} ₽ слишком длинный — заказчик уйдёт к тому, кто "
                                f"быстрее. Перепиши отклик: уложи весь план (и этапы в тексте) в {cap} дн. максимум, "
                                "без «запаса», работая плотнее. Цену НЕ поднимай, чтобы оправдать срок. Только если "
                                "рыночная стоимость всего объёма действительно выше 40 000 ₽ (крупная разработка, а не "
                                "несколько макетов) — назови рыночную цену и реальный срок на всё ТЗ. Не урезай состав "
                                "и не выноси части в отдельный этап. Цену НЕ снижай: если весь объём за "
                                f"{cap} дн. честно не сделать, значит {d['price']} ₽ — заниженная цена. "
                                "СУТЬ заново не пиши. Формат: ОТКЛИК / ЦЕНА / СРОК / РАБОТЫ."}]
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)),
                                              messages=messages)
        text2 = "".join(b.text for b in r.content if b.type == "text").strip()
        second = parse_draft(text2)
        if second.get("price") and second.get("days") and second["price"] >= d["price"] * 0.8:
            d, text = second, text2
        elif second.get("price"):
            # модель «уложилась в срок», срезав цену (корпоративный сайт: 36 000 → 12 500 ₽ за 2 дня) — так нельзя:
            # оставляем первый вариант, а длинный срок подсветит карточка
            log.warning("заказ %s: при сокращении срока цена упала %s → %s — оставляю первый вариант",
                        o.id, d["price"], second["price"])
            d = {**d, "long_term": True}
    d = fit_days(d)
    # заказчик просил указать в отклике конкретные пункты — проверяем, что ответ есть на каждый
    missing = unanswered(o, d["reply"])
    if missing:
        messages = messages + [{"role": "assistant", "content": text},
                               {"role": "user", "content":
                                "В отклике нет ответа на то, что заказчик прямо просил указать: " + "; ".join(missing)
                                + ". Перепиши отклик: ответь на КАЖДЫЙ пункт из списка заказчика в его порядке, каждый "
                                "отдельным абзацем, начинающимся словами пункта. Про примеры работ — назови самые близкие "
                                "работы из портфолио и прикрепи их. Цену и срок не меняй. "
                                "СУТЬ заново не пиши. Формат: ОТКЛИК / ЦЕНА / СРОК / РАБОТЫ."}]
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)),
                                              messages=messages)
        third = parse_draft("".join(b.text for b in r.content if b.type == "text").strip())
        if third.get("reply") and len(third["reply"]) > 150:
            d = fit_days({**third, "price": third.get("price") or d.get("price"), "days": third.get("days") or d.get("days"),
                          "works": third.get("works") or d.get("works", [])})
            missing = unanswered(o, d["reply"])
    d["unanswered"] = missing
    return d


ASKED = """Ниже заказ с фриланс-биржи и отклик исполнителя. Найди в заказе ЯВНЫЕ просьбы, что указать или написать
в отклике (списки «В отклике укажите / просим указать / От вас / Напишите», прямые вопросы к исполнителю).
Для каждой проверь, есть ли в отклике ответ по существу — конкретный, а не намёк одной фразой. Пункт отвечен, если
ответ есть, даже когда он неидеален (например, пример работы на другой платформе — это ответ). Не придирайся к качеству.
Бери только то, что написал ЗАКАЗЧИК в тексте заказа; вопросы и уточнения самого исполнителя — не пункты.
Пункты ТЗ — что нужно СДЕЛАТЬ в работе (подготовить, согласовать, настроить) — это НЕ просьбы к отклику, их не
включай. Только «в отклике укажите/напишите/опишите …» и прямые вопросы к исполнителю (сколько? как? есть ли?).
Верни только JSON: {"missing": ["пункт заказчика коротко", ...]} — пункты БЕЗ ответа. Если просьб нет или всё
отвечено — {"missing": []}."""


# в заказе есть просьбы к отклику или вопросы — только тогда проверяем (без них модель-проверщик выдумывает пункты)
ASK_MARK = (r"в (отклике|ответе|сообщении)|укажите|напишите|опишите|расскажите|пришлите|приложите|от вас\s*:|"
            r"просим|прошу (указать|написать|прислать)|\?")


def unanswered(o: Order, reply: str) -> list[str]:
    """Какие пункты из «в отклике укажите» остались без ответа (дешёвая модель). Ошибка проверки — не мешаем черновику."""
    if not re.search(ASK_MARK, f"{o.title} {o.desc}", re.I):
        return []
    try:
        r = rag._get_claude().messages.create(model=TRIAGE_MODEL, max_tokens=600, system=ASKED, thinking=NO_THINKING, messages=[{
            "role": "user", "content": f"ЗАКАЗ:\n{o.title}\n\n{o.desc[:6000]}\n\nОТКЛИК:\n{reply}"}])
        m = re.search(r"\{.*\}", "".join(b.text for b in r.content if b.type == "text"), re.S)
        got, _ = json.JSONDecoder().raw_decode(m.group(0))      # после JSON модель иногда дописывает пояснение
        return [str(x)[:120] for x in got.get("missing", [])][:6]
    except Exception:
        log.warning("проверка пунктов заказа не удалась", exc_info=True)
        return []


# потолок срока по цене (решение Евгения): заказчику срок важен так же, как стоимость
DAYS_CAP = [(15000, 2), (40000, 7)]


def max_days(price: int | None) -> int | None:
    for limit, cap in DAYS_CAP:
        if price and price <= limit:
            return cap
    return None


def fit_days(d: dict) -> dict:
    """Срок не длиннее потолка для этой цены; в тексте отклика «N дн.» тоже правим."""
    cap = max_days(d.get("price"))
    if cap and d.get("days") and d["days"] > cap and not d.get("long_term"):
        old = d["days"]
        d = {**d, "days": cap, "days_cut": old}
        word = "день" if cap == 1 else "дня"
        d["reply"] = re.sub(rf"\b{old}\s*(дней|дня|день|дн\.?|рабочих дней|рабочих дня)",
                            f"{cap} {word}", d["reply"])
    return d


BANNED = [r"\bделал[аи]?\b", r"не первый раз", r"уже (настраивал|делал|работал)", r"\bЕвгений\b", r"опыта (нет|пока нет)",
          r"опыт\w*[^.!?]{0,70}(не было|нет|не делал)", r"честно (скажу|признаюсь)", r"без делени\w* на[^.!?]{0,20}этап", r"честно не (сделать|успеть)",
          r"@\w{4,}", r"\+?\d[\d\s()-]{9,}\d", r"t\.me/", r"предоплат", r"github", r"https?://", r"www\."]


# «остальное — следующим этапом отдельным бюджетом»: без просьбы заказчика это урезанное предложение
SPLIT = (r"отдельн\w* (бюджет|оплат|заказ|сч[её]т|стоимост)|за отдельн\w* (плат|деньг|бюджет)|доплат|"
         r"следующ\w* этап\w*[^.!?]{0,60}(бюджет|оплат|стоимост|цен)|втор\w* заказ")
# заказчик сам просит этапы / MVP / первую очередь — тогда отдельный этап уместен
STAGED = (r"поэтапн\w* оплат|оплат\w* поэтапн|оплат\w* (по )?этап|этап\w*[^.!?]{0,40}оплат|\bmvp\b|перв\w* очеред|перв\w* верси|"
          r"прототип|дальше посмотрим|потом доработ|бюджет\w*[^.!?]{0,30}перв\w* (этап|част)")


def draft_problems(d: dict, o: "Order | None" = None) -> list[str]:
    """Что нельзя отправлять: выдуманный опыт, обращение к себе, контакты, пустая цена."""
    probs = [b for b in BANNED if re.search(b, d["reply"], re.I)]
    if not d.get("price"):
        probs.append("нет цены")
    if len(d["reply"]) < 150:
        probs.append("короткий черновик")
    asked = o is not None and re.search(STAGED, f"{o.title} {o.desc}", re.I)
    if re.search(SPLIT, d["reply"], re.I) and not asked:
        probs.append("часть работ вынесена в отдельный этап/бюджет, а заказчик этапов не просил — проверь")
    if d.get("unanswered"):
        probs.append("нет ответа на просьбу заказчика: " + "; ".join(d["unanswered"]))
    if d.get("long_term"):
        probs.append(f"{d.get('days')} дн. за {d.get('price')} ₽ — дольше потолка, а сократить срок модель смогла только "
                     "срезав цену; проверь: возможно, цена занижена для такого объёма")
    if d.get("days_cut"):
        probs.append(f"срок урезан с {d['days_cut']} до {d['days']} дн. автоматически — проверь этапы в тексте")
    if len(d["reply"]) > 2000 and (o is None or o.source == "kwork"):
        probs.append(f"длиннее 2000 символов ({len(d['reply'])}) — Kwork обрежет")
    return probs


def parse_draft(text: str) -> dict:
    reply = re.search(r"ОТКЛИК:\s*(.*?)(?:\n\s*ЦЕНА:|\Z)", text, re.S)
    price = re.search(r"ЦЕНА:\s*([\d\s]+)", text)
    days = re.search(r"СРОК:\s*(\d+)", text)
    works = re.search(r"РАБОТЫ:\s*([^\n]*)", text)
    nums = [int(x) for x in re.findall(r"\d+", works.group(1))] if works else []
    return {"reply": (reply.group(1) if reply else text).strip(),
            "price": int(price.group(1).replace(" ", "")) if price else None,
            "days": int(days.group(1)) if days else None,
            "works": fl_submit.works_from_numbers(nums)}


def revise(o: Order, d: dict, wish: str) -> dict:
    """Переписать черновик по пожеланию Евгения. Правила те же (без выдуманного опыта и контактов);
    если в пожелании новая цена/срок — берём их."""
    ids = {w["id"]: i for i, w in enumerate(fl_submit.PORTFOLIO, 1)}
    works = ", ".join(str(ids[w]) for w in d.get("works", []) if w in ids) or "нет"
    prev = (f"ОТКЛИК:\n{d['reply']}\nЦЕНА: {d.get('price') or ''} ₽\nСРОК: {d.get('days') or ''} дн.\n"
            f"РАБОТЫ: {works}")
    msgs = [{"role": "user", "content": posting_blocks(o, "")},
            {"role": "assistant", "content": prev},
            {"role": "user", "content": f"Перепиши отклик с учётом пожелания: «{wish}». Всё, чего пожелание не касается "
                                        "(цена, срок, работы, факты), оставь ровно как было. Но если пожелание меняет "
                                        "объём или состав работ (добавить или убрать часть, без этапов, всё ТЗ сразу) — "
                                        "пересчитай ЦЕНУ и СРОК под новый объём по тем же правилам, не втискивай больше "
                                        "работы в прежние цифры. Формат тот же: ОТКЛИК / ЦЕНА / СРОК / РАБОТЫ."}]
    try:
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)), messages=msgs)
    except anthropic.BadRequestError:
        msgs[0]["content"] = posting_blocks(o, "", with_files=False)      # вложение не принято — правим без него
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=rag.cached(draft_system(o)), messages=msgs)
    text = "".join(b.text for b in r.content if b.type == "text")
    new = parse_draft(text)
    # если модель потеряла цену/срок/работы — оставляем прежние
    new["price"] = new["price"] or d.get("price")
    new["days"] = new["days"] or d.get("days")
    if "РАБОТЫ:" not in text:
        new["works"] = d.get("works", [])
    new["unanswered"] = unanswered(o, new["reply"])
    return new


# ---------- 3. сообщение в Telegram ----------
def card(o: Order, tri: dict, d: dict, version: int = 1, status: str = "new") -> str:
    e = html.escape
    age = int((datetime.now(timezone.utc) - o.published).total_seconds() // 60)
    money = lambda v: f"{v:,} ₽".replace(",", " ")  # noqa: E731
    budget = f"💰 бюджет: {money(o.budget) if o.budget else 'не указан'}"
    if o.max_budget and o.budget and o.max_budget > o.budget:
        budget += f" (готов до {money(o.max_budget)})"
    if o.source == "fl" and o.for_all:
        budget += " · 🔓 для всех"
    if o.competitors is not None:
        budget += f" · 👥 откликов: {o.competitors}"
    lines = [f"🆕 <b>[{o.platform}] {e(o.title)}</b>",
             f"📂 {e(o.category)} · ⏱ {age} мин назад",
             budget,
             "", f"<i>{e(o.desc[:400])}{'…' if len(o.desc) > 400 else ''}</i>", ""]
    if d.get("price"):
        lines.append(f"🏷 предлагаю: <b>{d['price']:,} ₽</b>".replace(",", " ") + (f" · {d['days']} дн." if d.get("days") else ""))
    att = o.att or {}
    if att.get("read"):
        lines.append("📄 ТЗ прочитано: " + e(", ".join(att["read"])))
    if att.get("skipped") or att.get("error"):
        lines.append("⚠️ вложения: " + e("; ".join(att.get("skipped", []) + ([att["error"]] if att.get("error") else []))))
    if tri.get("learn"):
        lines.append(f"🎓 освоить: {e(tri['learn'])}")
    titles = {w["id"]: w["title"] for w in fl_submit.PORTFOLIO}
    if d.get("works"):
        lines.append("📎 прикреплю: " + e("; ".join(titles.get(w, str(w)) for w in d["works"])))
    rubric = o.category.rsplit(" / ", 1)[-1]
    if o.source == "kwork" and rubric in KWORK_NEED_PORTFOLIO and rubric not in kwork_have_portfolio():
        lines.append(f"⚠️ нет портфолио в рубрике «{e(rubric)}» — Kwork почти не показывает такие отклики; "
                     "сделать демо под эту рубрику")
    if tri.get("big"):
        lines.append("⚠️ крупный проект — оцени, потянешь ли")
    if tri.get("risk"):
        lines.append(f"⚠️ похоже на сомнительную схему: {e(str(tri['risk']))} — уточни у заказчика, прежде чем браться")
    if probs := draft_problems(d, o):
        lines.append("❗ проверь черновик: " + e(", ".join(probs)))
    head = "✅ <b>Утверждённый отклик</b>" if status == "approved" else (
        f"<b>Черновик отклика</b>" + (f" · версия {version}" if version > 1 else ""))
    tail = ["", "<i>Пожелание по правке — кнопка «Править» или ответь (reply) на это сообщение.</i>"] \
        if status != "approved" else []
    # лимит Telegram ~4096: режем сам текст отклика, а не готовый HTML — иначе обрезается закрывающий </code>,
    # Telegram отвергает карточку целиком, и заказ приходит с опозданием на 20 минут
    reply = d["reply"]
    while True:
        cut = len(reply) < len(d["reply"])
        body = e(reply) + ("…\n[полный текст — в форме отклика после «Утвердить»]" if cut else "")
        msg = "\n".join(lines + ["", head + " (нажми, чтобы скопировать):", f"<code>{body}</code>"] + tail)
        if len(msg) <= 4000 or not reply:
            return msg
        reply = reply[:max(0, len(reply) - (len(msg) - 4000) - 60)]


def keyboard(order_id: str, url: str, status: str = "new") -> dict:
    rows = [[{"text": "Открыть заказ на " + ("Kwork" if "kwork.ru" in url else "FL.ru"), "url": url}]]
    if status != "approved":
        rows.insert(0, [{"text": "✏️ Править", "callback_data": f"edit:{order_id}"},
                        {"text": "✅ Утвердить", "callback_data": f"ok:{order_id}"}])
    return {"inline_keyboard": rows}


async def send(session, token, chat, text, url, order_id) -> int | None:
    """Отправляет карточку, возвращает message_id (нужен, чтобы потом найти заказ по ответу на сообщение)."""
    for parse in ("HTML", None):
        body = {"chat_id": chat, "text": text if parse else html.unescape(re.sub(r"<[^>]+>", "", text)),
                "disable_web_page_preview": True, "reply_markup": keyboard(order_id, url)}
        if parse:
            body["parse_mode"] = parse
        async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body) as r:
            if r.status == 200:
                return (await r.json())["result"]["message_id"]
            err = (await r.text())[:200]
            log.warning("Telegram: %s %s", r.status, err)
            if "parse entities" not in err:
                return None
            # разметка сломалась — шлём карточку простым текстом, а не переписываем черновик на следующем проходе
    return None


# ---------- цикл ----------
async def run_once(session, client, token=None, chat=None, dry=False, first_run_hours=3,
                   fetch=None) -> list[tuple[Order, dict, dict | None]]:
    c = db()
    orders = await (fetch or fetch_all)(session)
    src = orders[0].source if orders else "fl"
    # «первый запуск» — отдельно для каждой площадки: Kwork не должен вывалить сотню старых заказов
    fresh_db = c.execute("select count(*) from seen where id like ?", ("kw%" if src == "kwork" else "%",)).fetchone()[0] == 0 \
        if src == "kwork" else c.execute("select count(*) from seen").fetchone()[0] == 0
    done, todo = [], []
    for o in orders:
        if is_seen(c, o.id):
            continue
        age_h = (datetime.now(timezone.utc) - o.published).total_seconds() / 3600
        if (fresh_db and age_h > first_run_hours) or age_h > MAX_AGE_HOURS:   # старые не шлём, только запоминаем
            mark(c, o.id, "old")
            continue
        if narrow(o):
            mark(c, o.id, "skip: проектирование/узкий специалист"); done.append((o, {"reason": "проектирование"}, None))
            continue
        if o.budget and o.budget < MIN_BUDGET:                 # FL.ru: у Kwork такие отсекаются ещё в kwork_watch
            mark(c, o.id, f"skip: бюджет {o.budget} < {MIN_BUDGET}"); done.append((o, {"reason": "дешевле минималки"}, None))
            continue
        todo.append(o)

    async def handle(o: Order):
        if o.source == "fl":
            o.desc = await fetch_full_desc(session, o)    # у Kwork полное описание уже в списке
        tri = await asyncio.to_thread(triage, client, o)
        if not tri.get("take"):
            mark(c, o.id, "skip: " + str(tri.get("reason", ""))[:200])
            return o, tri, None
        if o.source == "fl" or o.files:
            got = await attachments.download(o)
            _, read, skipped = attachments.to_blocks(attachments.cached(o.id))
            if got["files"] or got["error"] or skipped:
                o.att = {"read": read, "skipped": skipped, "error": got["error"]}
        d = await asyncio.to_thread(draft, o)
        if not dry:
            msg_id = await send(session, token, chat, card(o, tri, d), o.link, o.id)
            if not msg_id:
                return None                             # не отправилось — попробуем в следующем проходе
            save_draft(c, o, tri, d, msg_id)
        mark(c, o.id, "sent")
        return o, tri, d

    # несколько новых заказов разом — разбираем параллельно: третий не ждёт, пока напишутся черновики первых двух
    sem = asyncio.Semaphore(PARALLEL)

    async def guarded(o):
        async with sem:
            return await handle(o)

    failed = None
    for o, res in zip(todo, await asyncio.gather(*(guarded(o) for o in todo), return_exceptions=True)):
        if isinstance(res, BaseException):
            log.error("заказ %s: %s", o.id, res, exc_info=res)
            failed = failed or res
        elif res:
            done.append(res)
    if failed is not None and "credit balance" in str(failed).lower():
        raise failed                                    # чтобы watch_forever предупредил о балансе
    return done


PARALLEL = int(os.getenv("DRAFT_PARALLEL") or 3)


async def watch_forever(token, chat, every=60):
    """FL.ru — раз в every секунд, Kwork — раз в KWORK_EVERY_SEC. У каждой биржи свой цикл: пока пишутся черновики
    для FL.ru, Kwork продолжает проверяться (раньше ждал — а там за минуты набегают отклики)."""
    import kwork_watch
    client = rag._get_claude()        # общий клиент — его расход пишется в api_usage
    sources = [("FL", fetch_all, every), ("Kwork", kwork_watch.fetch_all, int(os.getenv("KWORK_EVERY_SEC") or 60))]
    loop = asyncio.get_running_loop()
    warned = [0.0]    # когда последний раз предупреждали о закончившемся балансе API
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers={"User-Agent": UA}) as s:

        async def poll(name, fetch, period):
            while True:
                started = loop.time()
                try:
                    res = await run_once(s, client, token, chat, fetch=fetch)
                    if res:
                        log.info("%s: новых %s, отправлено %s", name, len(res), sum(1 for _, _, d in res if d))
                except Exception as e:
                    log.exception("ошибка прохода %s", name)
                    # кончились деньги на API — без этого бот молча перестаёт присылать заказы; пишем раз в 3 часа
                    if "credit balance" in str(e).lower() and loop.time() - warned[0] > 3 * 3600:
                        warned[0] = loop.time()
                        await s.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                            "chat_id": chat, "text": "⚠️ Закончился баланс API Anthropic — заказы не разбираю и черновики "
                                                     "не пишу. Пополни баланс: console.anthropic.com → Plans & Billing. "
                                                     "Пропущенные за это время свежие заказы разберу после пополнения."})
                await asyncio.sleep(max(5, period - (loop.time() - started)))

        await asyncio.gather(*(poll(*src) for src in sources))


async def _cli():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не слать в Telegram, показать решения")
    ap.add_argument("--hours", type=float, default=3, help="при первом запуске взять заказы не старше N часов")
    a = ap.parse_args()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers={"User-Agent": UA}) as s:
        res = await run_once(s, Anthropic(), os.getenv("BOT_TOKEN"), os.getenv("ADMIN_ID"), dry=a.dry, first_run_hours=a.hours)
    bad = 0
    for o, tri, d in res:
        print(("✅" if d else "⛔"), o.category, "|", o.title[:70], "|", o.budget, "|", tri.get("reason", "")[:80])
        if d:
            probs = draft_problems(d, o)
            bad += bool(probs)
            print("   цена:", d["price"], "срок:", d["days"], "| освоить:", tri.get("learn") or "—",
                  f"| ❗ {probs}" if probs else "")
            print("   ", d["reply"][:300].replace("\n", " "))
    print(f"\nИТОГ: заказов {len(res)}, черновиков {sum(1 for *_, d in res if d)}, с проблемами {bad}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli())
