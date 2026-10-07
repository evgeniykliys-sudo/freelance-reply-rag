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
from anthropic import Anthropic

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
                 on conflict(order_id) do update set draft_json=excluded.draft_json, versions=versions+1, status='new'""",
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
        f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\nЗаголовок: {o.title}\nОписание: {o.desc[:1500]}"}])
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
3. Срок реалистичный, с запасом.
4. Без контактов, мессенджеров, предоплаты вне FL.ru. Ссылки на GitHub-примеры из фрагментов можно.
5. Стиль: живо и по делу, без канцелярита. 4–7 предложений: приветствие по сути задачи → подтверждение (проект или
   план работы) → срок и цена → один уточняющий вопрос, самый важный для оценки.
6. Ответ строго в формате (без markdown):
ОТКЛИК:
<текст>
ЦЕНА: <число> ₽
СРОК: <число> дн."""


def draft(o: Order) -> dict:
    posting = f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\n{o.title}\n\n{o.desc}"
    chunks = rag.search(posting)[:6]
    context = "\n\n".join(f"[{c['type']}] {c['text']}" for c in chunks)
    messages = [{"role": "user", "content": f"Фрагменты базы услуг/портфолио/шаблонов:\n\n{context}\n\nТекст заказа:\n{posting}"}]
    for attempt in range(2):
        r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=1200, system=DRAFT_SYSTEM, messages=messages)
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        reply = re.search(r"ОТКЛИК:\s*(.*?)(?:\n\s*ЦЕНА:|\Z)", text, re.S)
        price = re.search(r"ЦЕНА:\s*([\d\s]+)", text)
        days = re.search(r"СРОК:\s*(\d+)", text)
        if reply and reply.group(1).strip() and price:
            break
        # модель ушла от формата (рассуждения, пустой ответ) — просим переписать строго по формату
        messages = messages + [{"role": "assistant", "content": text or "…"},
                               {"role": "user", "content": "Перепиши строго в формате: ОТКЛИК: … / ЦЕНА: … ₽ / СРОК: … дн. Без вступлений."}]
    return {"reply": (reply.group(1) if reply else text).strip(),
            "price": int(price.group(1).replace(" ", "")) if price else None,
            "days": int(days.group(1)) if days else None}


BANNED = [r"\bделал[аи]?\b", r"не первый раз", r"уже (настраивал|делал|работал)", r"\bЕвгений\b", r"опыта (нет|пока нет)",
          r"@\w{4,}", r"\+?\d[\d\s()-]{9,}\d", r"t\.me/", r"предоплат"]


def draft_problems(d: dict) -> list[str]:
    """Что нельзя отправлять: выдуманный опыт, обращение к себе, контакты, пустая цена."""
    probs = [b for b in BANNED if re.search(b, d["reply"], re.I)]
    if not d.get("price"):
        probs.append("нет цены")
    if len(d["reply"]) < 150:
        probs.append("короткий черновик")
    return probs


def parse_draft(text: str) -> dict:
    reply = re.search(r"ОТКЛИК:\s*(.*?)(?:\n\s*ЦЕНА:|\Z)", text, re.S)
    price = re.search(r"ЦЕНА:\s*([\d\s]+)", text)
    days = re.search(r"СРОК:\s*(\d+)", text)
    return {"reply": (reply.group(1) if reply else text).strip(),
            "price": int(price.group(1).replace(" ", "")) if price else None,
            "days": int(days.group(1)) if days else None}


def revise(o: Order, d: dict, wish: str) -> dict:
    """Переписать черновик по пожеланию Евгения. Правила те же (без выдуманного опыта и контактов);
    если в пожелании новая цена/срок — берём их."""
    posting = f"Раздел: {o.category}\nБюджет: {o.budget or 'не указан'}\n{o.title}\n\n{o.desc}"
    prev = f"ОТКЛИК:\n{d['reply']}\nЦЕНА: {d.get('price') or ''} ₽\nСРОК: {d.get('days') or ''} дн."
    msgs = [{"role": "user", "content": f"Текст заказа:\n{posting}"},
            {"role": "assistant", "content": prev},
            {"role": "user", "content": f"Перепиши отклик с учётом пожелания: «{wish}». Всё остальное оставь как было, "
                                        "если пожелание этого не касается. Формат тот же: ОТКЛИК / ЦЕНА / СРОК."}]
    r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=1200, system=DRAFT_SYSTEM, messages=msgs)
    new = parse_draft("".join(b.text for b in r.content if b.type == "text"))
    # если модель потеряла цену/срок — оставляем прежние
    new["price"] = new["price"] or d.get("price")
    new["days"] = new["days"] or d.get("days")
    return new


# ---------- 3. сообщение в Telegram ----------
def card(o: Order, tri: dict, d: dict, version: int = 1, status: str = "new") -> str:
    e = html.escape
    age = int((datetime.now(timezone.utc) - o.published).total_seconds() // 60)
    lines = [f"🆕 <b>{e(o.title)}</b>",
             f"📂 {e(o.category)} · ⏱ {age} мин назад",
             f"💰 бюджет: {f'{o.budget:,} ₽'.replace(',', ' ') if o.budget else 'не указан'}"
             + (" · 🔓 для всех" if o.for_all else ""),
             "", f"<i>{e(o.desc[:400])}{'…' if len(o.desc) > 400 else ''}</i>", ""]
    if d.get("price"):
        lines.append(f"🏷 предлагаю: <b>{d['price']:,} ₽</b>".replace(",", " ") + (f" · {d['days']} дн." if d.get("days") else ""))
    if tri.get("learn"):
        lines.append(f"🎓 освоить: {e(tri['learn'])}")
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
    rows = [[{"text": "Открыть заказ на FL.ru", "url": url}]]
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
async def run_once(session, client, token=None, chat=None, dry=False, first_run_hours=3) -> list[tuple[Order, dict, dict | None]]:
    c = db()
    orders = await fetch_all(session)
    fresh_db = c.execute("select count(*) from seen").fetchone()[0] == 0
    done = []
    for o in orders:
        if is_seen(c, o.id):
            continue
        age_h = (datetime.now(timezone.utc) - o.published).total_seconds() / 3600
        if fresh_db and age_h > first_run_hours:      # первый запуск: старые заказы не шлём, только запоминаем
            mark(c, o.id, "old")
            continue
        tri = await asyncio.to_thread(triage, client, o)
        if not tri.get("take"):
            mark(c, o.id, "skip: " + str(tri.get("reason", ""))[:200]); done.append((o, tri, None))
            continue
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
    client = Anthropic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers={"User-Agent": UA}) as s:
        while True:
            try:
                res = await run_once(s, client, token, chat)
                if res:
                    log.info("FL: новых %s, отправлено %s", len(res), sum(1 for _, _, d in res if d))
            except Exception:
                log.exception("ошибка прохода FL")
            await asyncio.sleep(every)


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
