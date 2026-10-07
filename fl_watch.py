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
from datetime import datetime, timezone
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
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL") or "claude-haiku-4-5-20251001"
DISCOUNT = int(os.getenv("DISCOUNT_PCT") or 40)        # насколько ниже рынка/прайса предлагать на старте
MIN_PRICE = int(os.getenv("MIN_PRICE") or 1500)
# заказы старше этого возраста не берём: на них уже набралось откликов (решение Евгения — 2 часа)
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS") or 2)
# Kwork требует портфолио только в этих рубриках (для ботов и скриптов хватает кворков);
# где работы уже есть — перечислить через запятую в KWORK_PORTFOLIO
KWORK_NEED_PORTFOLIO = {"Создание сайта", "Верстка", "Мобильные приложения", "Игры"}
KWORK_HAVE_PORTFOLIO = {x.strip() for x in (os.getenv("KWORK_PORTFOLIO") or "Создание сайта").split(",") if x.strip()}


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
{"take": true|false, "reason": "до 12 слов", "learn": "что освоить, до 12 слов, или пусто", "big": true|false}
take=false ТОЛЬКО если: не IT (офлайн, выезд, звонки, физический труд); нужны десятки живых людей/аккаунтов;
накрутки, фейковые отзывы, подписки, спам, обход защит; учебные работы за студента; вакансия в штат/на полный день.
Отсутствие бюджета, ТЗ во вложении, незнакомая технология — НЕ причина отказа (take=true, укажи learn).
big=true — проект явно крупный для одного человека (больше ~2 недель работы)."""


def triage(client: Anthropic, o: Order) -> dict:
    r = client.messages.create(model=TRIAGE_MODEL, max_tokens=200, system=TRIAGE, messages=[{"role": "user", "content":
        f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\nЗаголовок: {o.title}\nОписание: {o.desc[:4000]}"}])
    text = "".join(b.text for b in r.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0))
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
   Одной фразой объясни: «беру по сниженной цене — набираю первые отзывы на FL.ru».
3. Срок реалистичный, с запасом. СРОК — одно целое число дней (не диапазон, не «2 недели»).
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
   Если в заказе есть список «От вас / В отклике напишите / Нужно указать» — ответь на КАЖДЫЙ пункт в том же порядке
   (портфолио → «прикрепил работы» + какие из портфолио похожи; стек; сроки и стоимость; цена тестового и т.д.).
3б. Если к заказу приложены файлы (ТЗ, таблицы, скриншоты, макеты) — это главный источник. Объём работ, этапы,
   СРОК и ЦЕНУ считай по ним, а не только по короткому тексту заказа: перечисли про себя все пункты ТЗ и оцени каждый.
   В отклике упомяни 1–2 конкретные детали из файлов — чтобы было видно, что ТЗ прочитано. Если в ТЗ есть вопросы
   к исполнителю — ответь на каждый. Если ТЗ на большой проект — предложи этапы с ценой и сроком каждого.
4. Без контактов, мессенджеров, предоплаты вне FL.ru и БЕЗ ССЫЛОК (никаких github, сайтов, URL) — FL.ru их не любит.
   Примеры работ прикрепляются к отклику отдельно (строка РАБОТЫ), в тексте можно написать «прикрепил похожие работы».
5. Стиль: живо и по делу, без канцелярита. 4–7 предложений: приветствие по сути задачи → подтверждение (проект или
   план работы) → срок и цена → один уточняющий вопрос, самый важный для оценки.
6. РАБОТЫ: номера до 3 работ портфолио из списка ниже, которые реально похожи на заказ или показывают нужный навык
   (сайт/дизайн → ближе всего AI-консультант на сайт и Mini App; боты → боты; парсинг → парсеры). Если ничего не
   подходит — «РАБОТЫ: нет».
7. Ответ строго в формате (без markdown). СУТЬ — твой разбор для себя, заказчик его не увидит; отклик пиши по нему:
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
            r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=draft_system(o),
                                                  messages=messages)
        except anthropic.BadRequestError:
            # модель не приняла вложение (битый файл, не тот формат) — пишем по тексту заказа, а не падаем
            # (иначе заказ не отмечается просмотренным и бот повторяет его каждый проход)
            if len(messages[0]["content"]) == 1:
                raise
            log.warning("заказ %s: вложения не приняты моделью, черновик без них", o.id)
            o.att = {**(o.att or {}), "read": [], "error": "нейросеть не приняла вложения — черновик по тексту заказа"}
            messages = [{"role": "user", "content": posting_blocks(o, intro, with_files=False)}]
            r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=draft_system(o),
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
    return parse_draft(text)


BANNED = [r"\bделал[аи]?\b", r"не первый раз", r"уже (настраивал|делал|работал)", r"\bЕвгений\b", r"опыта (нет|пока нет)",
          r"опыт\w*[^.!?]{0,40}(не было|нет|не делал)",
          r"@\w{4,}", r"\+?\d[\d\s()-]{9,}\d", r"t\.me/", r"предоплат", r"github", r"https?://", r"www\."]


def draft_problems(d: dict) -> list[str]:
    """Что нельзя отправлять: выдуманный опыт, обращение к себе, контакты, пустая цена."""
    probs = [b for b in BANNED if re.search(b, d["reply"], re.I)]
    if not d.get("price"):
        probs.append("нет цены")
    if len(d["reply"]) < 150:
        probs.append("короткий черновик")
    if len(d["reply"]) > 2000:
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
                                        "(цена, срок, работы, факты), оставь ровно как было. Формат тот же: "
                                        "ОТКЛИК / ЦЕНА / СРОК / РАБОТЫ."}]
    try:
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=draft_system(o), messages=msgs)
    except anthropic.BadRequestError:
        msgs[0]["content"] = posting_blocks(o, "", with_files=False)      # вложение не принято — правим без него
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=4000, system=draft_system(o), messages=msgs)
    text = "".join(b.text for b in r.content if b.type == "text")
    new = parse_draft(text)
    # если модель потеряла цену/срок/работы — оставляем прежние
    new["price"] = new["price"] or d.get("price")
    new["days"] = new["days"] or d.get("days")
    if "РАБОТЫ:" not in text:
        new["works"] = d.get("works", [])
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
    if o.source == "kwork" and rubric in KWORK_NEED_PORTFOLIO and rubric not in KWORK_HAVE_PORTFOLIO:
        lines.append(f"⚠️ нет портфолио в рубрике «{e(rubric)}» — Kwork почти не показывает такие отклики; "
                     "сделать демо под эту рубрику")
    if tri.get("big"):
        lines.append("⚠️ крупный проект — оцени, потянешь ли")
    if probs := draft_problems(d):
        lines.append("❗ проверь черновик: " + e(", ".join(probs)))
    head = "✅ <b>Утверждённый отклик</b>" if status == "approved" else (
        f"<b>Черновик отклика</b>" + (f" · версия {version}" if version > 1 else ""))
    lines += ["", head + " (нажми, чтобы скопировать):", f"<code>{e(d['reply'])}</code>"]
    if status != "approved":
        lines += ["", "<i>Пожелание по правке — кнопка «Править» или ответь (reply) на это сообщение.</i>"]
    msg = "\n".join(lines)
    return msg[:4000]


def keyboard(order_id: str, url: str, status: str = "new") -> dict:
    rows = [[{"text": "Открыть заказ на " + ("Kwork" if "kwork.ru" in url else "FL.ru"), "url": url}]]
    if status != "approved":
        rows.insert(0, [{"text": "✏️ Править", "callback_data": f"edit:{order_id}"},
                        {"text": "✅ Утвердить", "callback_data": f"ok:{order_id}"}])
    return {"inline_keyboard": rows}


async def send(session, token, chat, text, url, order_id) -> int | None:
    """Отправляет карточку, возвращает message_id (нужен, чтобы потом найти заказ по ответу на сообщение)."""
    async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": keyboard(order_id, url)}) as r:
        if r.status != 200:
            log.warning("Telegram: %s %s", r.status, (await r.text())[:200])
            return None
        return (await r.json())["result"]["message_id"]


# ---------- цикл ----------
async def run_once(session, client, token=None, chat=None, dry=False, first_run_hours=3,
                   fetch=None) -> list[tuple[Order, dict, dict | None]]:
    c = db()
    orders = await (fetch or fetch_all)(session)
    src = orders[0].source if orders else "fl"
    # «первый запуск» — отдельно для каждой площадки: Kwork не должен вывалить сотню старых заказов
    fresh_db = c.execute("select count(*) from seen where id like ?", ("kw%" if src == "kwork" else "%",)).fetchone()[0] == 0 \
        if src == "kwork" else c.execute("select count(*) from seen").fetchone()[0] == 0
    done = []
    for o in orders:
        if is_seen(c, o.id):
            continue
        age_h = (datetime.now(timezone.utc) - o.published).total_seconds() / 3600
        if (fresh_db and age_h > first_run_hours) or age_h > MAX_AGE_HOURS:   # старые не шлём, только запоминаем
            mark(c, o.id, "old")
            continue
        if o.source == "fl":
            o.desc = await fetch_full_desc(session, o)    # у Kwork полное описание уже в списке
        tri = await asyncio.to_thread(triage, client, o)
        if not tri.get("take"):
            mark(c, o.id, "skip: " + str(tri.get("reason", ""))[:200]); done.append((o, tri, None))
            continue
        if o.source == "fl" or o.files:
            got = await attachments.download(o)
            _, read, skipped = attachments.to_blocks(attachments.cached(o.id))
            if got["files"] or got["error"] or skipped:
                o.att = {"read": read, "skipped": skipped, "error": got["error"]}
        d = await asyncio.to_thread(draft, o)
        msg_id = None
        if not dry:
            msg_id = await send(session, token, chat, card(o, tri, d), o.link, o.id)
            if not msg_id:
                continue                                # не отправилось — попробуем в следующем проходе
            save_draft(c, o, tri, d, msg_id)
        mark(c, o.id, "sent"); done.append((o, tri, d))
    return done


async def watch_forever(token, chat, every=180):
    """FL.ru — раз в every секунд, Kwork — раз в KWORK_EVERY_SEC (там на заказ за 10 минут приходят десятки откликов)."""
    import kwork_watch
    client = Anthropic()
    sources = [("FL", fetch_all, every), ("Kwork", kwork_watch.fetch_all, int(os.getenv("KWORK_EVERY_SEC") or 120))]
    last = {name: 0.0 for name, *_ in sources}
    loop = asyncio.get_running_loop()
    warned = 0.0      # когда последний раз предупреждали о закончившемся балансе API
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers={"User-Agent": UA}) as s:
        while True:
            for name, fetch, period in sources:
                if loop.time() - last[name] < period:
                    continue
                last[name] = loop.time()
                try:
                    res = await run_once(s, client, token, chat, fetch=fetch)
                    if res:
                        log.info("%s: новых %s, отправлено %s", name, len(res), sum(1 for _, _, d in res if d))
                except Exception as e:
                    log.exception("ошибка прохода %s", name)
                    # кончились деньги на API — без этого бот молча перестаёт присылать заказы; пишем раз в 3 часа
                    if "credit balance" in str(e).lower() and loop.time() - warned > 3 * 3600:
                        warned = loop.time()
                        await s.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                            "chat_id": chat, "text": "⚠️ Закончился баланс API Anthropic — заказы не разбираю и черновики "
                                                     "не пишу. Пополни баланс: console.anthropic.com → Plans & Billing. "
                                                     "Пропущенные за это время свежие заказы разберу после пополнения."})
            await asyncio.sleep(20)


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
            probs = draft_problems(d)
            bad += bool(probs)
            print("   цена:", d["price"], "срок:", d["days"], "| освоить:", tri.get("learn") or "—",
                  f"| ❗ {probs}" if probs else "")
            print("   ", d["reply"][:300].replace("\n", " "))
    print(f"\nИТОГ: заказов {len(res)}, черновиков {sum(1 for *_, d in res if d)}, с проблемами {bad}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli())
