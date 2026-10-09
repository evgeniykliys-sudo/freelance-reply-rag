"""Сообщения от заказчиков Kwork и FL.ru → Telegram с черновиком ответа; ответ уходит заказчику только по кнопке.

Читаем через ваш Chrome (он залогинен на обеих биржах) запросами без вкладок:
  Kwork — список чатов лежит JSON-ом в странице kwork.ru/inbox (window.chatList), история диалога —
          POST /inbox_more_messages;
  FL.ru — переписка по откликам: /projects/offers/?dialogues=1 (список) и /projects/<id>/offers/<id>/messages/.
Новое входящее → карточка: что написал заказчик, файлы (ТЗ читаем), черновик ответа и кнопки
«🚀 Отправить», «✏️ Править» (пожелание → перепишу), «✍️ Свой текст» (отправлю ваш текст как есть), «Без ответа».
Отправка — в отдельной вкладке Chrome, которая сразу закрывается; после отправки проверяем, что сообщение в чате.
"""
import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.types import (CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message)
from playwright.async_api import async_playwright

import attachments
import fl_watch
import rag
from fl_submit import CDP

log = logging.getLogger("inbox_watch")
DB = Path(__file__).parent / "fl_seen.db"
ATT = attachments.DIR / "chat"
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
FL_OFFERS = "https://www.fl.ru/projects/offers/?limit=20&dialogues=1&deleted=1&sort=lastMessage&offset=0"
FL_COUNTERS = {"orders": "в «Сделках»", "tservices": "по услугам", "fl_team": "от команды FL"}

router = Router()
pending: dict[int, tuple[int, str]] = {}        # кто → (карточка, "edit" | "own") — ждём текст после кнопки


# ---------- хранилище ----------
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    # последнее увиденное входящее по каждому чату (время), чтобы не слать одно сообщение дважды
    c.execute("create table if not exists chat_seen (key text primary key, last integer)")
    c.execute("""create table if not exists chat_items (id integer primary key autoincrement, item_json text,
                 draft text, status text default 'new', created text)""")
    c.execute("create table if not exists chat_msgs (msg_id integer primary key, item_id integer)")
    return c


def seen(c, key: str) -> int | None:
    r = c.execute("select last from chat_seen where key=?", (key,)).fetchone()
    return r[0] if r else None


def set_seen(c, key: str, last: int):
    c.execute("insert or replace into chat_seen values (?,?)", (key, last))
    c.commit()


def load(c, item_id: int):
    r = c.execute("select item_json, draft, status from chat_items where id=?", (item_id,)).fetchone()
    return (json.loads(r[0]), r[1], r[2]) if r else None


def item_by_msg(c, msg_id: int) -> int | None:
    r = c.execute("select item_id from chat_msgs where msg_id=?", (msg_id,)).fetchone()
    return r[0] if r else None


# ---------- чтение бирж ----------
def clean(text: str) -> str:
    """Текст сообщения Kwork/FL: HTML-сущности, BB-коды ссылок, теги переноса."""
    t = html.unescape(text or "")
    t = re.sub(r"\[URL=([^\]]+)\](.*?)\[/URL\]", r"\2 (\1)", t)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t.replace("\r\n", "\n").strip()


async def kwork_new(req, c) -> list[dict]:
    r = await req.get("https://kwork.ru/inbox", timeout=40000)
    page = await r.text()
    i = page.find("window.chatList=")
    if i < 0:
        raise RuntimeError("Kwork: нет списка чатов — в Chrome не выполнен вход?")
    chats, _ = json.JSONDecoder().raw_decode(page[i + len("window.chatList="):])
    first = seen(c, "kw:init") is None
    out = []
    for ch in chats:
        last = ch.get("lastMessage") or {}
        key, me, t = f"kw:{ch['USERID']}", ch.get("member_id"), int(last.get("time") or ch.get("time") or 0)
        before = seen(c, key)
        if first:
            set_seen(c, key, t)
            continue
        before = before or 0
        if t <= before:
            continue
        if last.get("MSGFROM") == me:            # последним писали мы (например, ответили на сайте) — всё отвечено
            set_seen(c, key, t)
            continue
        hist = await kwork_history(req, ch["USERID"], ch["username"])
        new = [m for m in hist if not m["mine"] and m["time"] > before]
        if new:
            subject = next((re.sub(r"^Добрый день\. Тема: ", "", m["text"]) for m in hist if "Тема:" in m["text"]), "")
            out.append({"site": "kwork", "key": key, "peer": ch["username"], "peer_id": ch["USERID"],
                        "subject": subject[:120], "url": f"https://kwork.ru/inbox/{ch['username']}",
                        "history": hist[-12:], "new": new, "last": t})
        else:
            set_seen(c, key, t)
    if first:
        set_seen(c, "kw:init", 1)
    return out


async def kwork_history(req, user_id: int, username: str) -> list[dict]:
    r = await req.post("https://kwork.ru/inbox_more_messages", multipart={"userId": str(user_id), "allUnread": "true"},
                       headers={**XHR, "Referer": f"https://kwork.ru/inbox/{username}"}, timeout=40000)
    msgs = (await r.json())["data"]["messages"]
    out = []
    for m in sorted(msgs, key=lambda m: m["time"]):
        files = m.get("files") or {}
        files = list(files.values()) if isinstance(files, dict) else files
        out.append({"mine": m.get("MSGFROM") != user_id, "time": int(m["time"]), "text": clean(m.get("message")),
                    "files": [{"name": f.get("fname") or "файл", "url": f["url"]} for f in files if f.get("url")]})
    return out


async def fl_new(req, c) -> list[dict]:
    r = await req.get(FL_OFFERS, headers=XHR, timeout=40000)
    items = (await r.json())["items"]
    first = seen(c, "fl:init") is None
    out = []
    for it in items:
        key, t = f"fl:{it['id']}", int(it.get("last_message_at") or 0)
        before = seen(c, key)
        if first:
            set_seen(c, key, t)
            continue
        before = before or 0
        if t <= before:
            continue
        me = (it.get("author") or {}).get("id")
        r = await req.get(f"https://www.fl.ru/projects/{it['project_id']}/offers/{it['id']}/messages/?limit=40&offset=0",
                          headers=XHR, timeout=40000)
        hist = [{"mine": m["from_id"] == me, "time": int(m["time"]), "text": clean(m.get("text")),
                 "files": [{"name": f.get("name") or f.get("original_name") or "файл", "url": f.get("url") or f.get("link")}
                           for f in (m.get("files") or []) if f.get("url") or f.get("link")]}
                for m in sorted((await r.json())["items"], key=lambda m: m["time"])]
        new = [m for m in hist if not m["mine"] and m["time"] > before]
        if new:
            out.append({"site": "fl", "key": key, "peer": "заказчик", "subject": it.get("title", "")[:120],
                        "project_id": it["project_id"], "offer_id": it["id"],
                        "url": f"https://www.fl.ru/messages/?dialogId={it['id']}&dialogType=offer",
                        "history": hist[-12:], "new": new, "last": t})
        else:
            set_seen(c, key, t)
    if first:
        set_seen(c, "fl:init", 1)
    # переписка не по откликам (сделки, услуги, команда FL) — только сигнал, что там что-то новое
    r = await req.get("https://www.fl.ru/user/cnt-new/chat/", headers=XHR, timeout=40000)
    cnt = await r.json()
    for name, where in FL_COUNTERS.items():
        n, key = int(cnt.get(name) or 0), f"fl:cnt:{name}"
        if n and n != (seen(c, key) or 0):
            out.append({"site": "fl", "key": key, "notice": f"На FL.ru новые сообщения {where}: {n}",
                        "url": "https://www.fl.ru/messages/", "last": n})
        elif not n and seen(c, key):
            set_seen(c, key, 0)
    return out


async def download_files(req, item: dict) -> list[Path]:
    d = ATT / re.sub(r"\W", "_", item["key"])
    paths = []
    for m in item["new"]:
        for f in m["files"][:attachments.MAX_FILES]:
            p = d / attachments.safe_name(f["name"])
            if not p.exists():
                r = await req.get(f["url"], timeout=60000)
                body = await r.body()
                if r.status != 200 or len(body) > attachments.MAX_BYTES:
                    continue
                d.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
            paths.append(p)
    return paths


# ---------- черновик ответа ----------
SYSTEM = """Ты отвечаешь заказчику в чате {site} от имени фрилансера (Python, Telegram-боты, AI-автоматизация, парсеры,
сайты на Tilda/WordPress и конструкторах). Это продолжение переписки, не новый отклик.

Правила:
1. Ответь на КАЖДЫЙ вопрос и просьбу из новых сообщений заказчика — конкретно: цена и срок цифрами, если спрашивают.
   Держись того, что уже обещано в переписке (цены, сроки, этапы); не меняй без причины.
2. Если заказчик прислал файлы (ТЗ, отчёты, скриншоты) — прочитай их и опирайся на 1–2 конкретные детали.
3. Срок важен заказчику так же, как цена: до 15 000 ₽ — 1–2 дня, до 40 000 ₽ — не больше 5–7 дней.
   Не дроби работу на отдельно оплачиваемые этапы и «остальное отдельным бюджетом», если заказчик сам об этом не просит
   и это не было уже предложено в переписке: одна цена и один срок за весь объём.
   Цена и срок — одним числом, не диапазоном («4 500 ₽, 2 дня», а не «10–12 тыс., 2–3 дня»). Сейчас набираем первые
   отзывы — цены ниже рынка; если в переписке цена уже названа, держись её.
4. Никакого выдуманного опыта и клиентов («делал такое», «уже настраивал»). Не называй себя по имени.
5. Без контактов вне биржи (телефон, Telegram, почта, ссылки) и без оплаты мимо биржи — {site} за это блокирует.
   Если заказчик зовёт в мессенджер — вежливо предложи продолжить здесь и оформить заказ через биржу.
6. Коротко и живо, без канцелярита: 2–6 предложений; длинно — только если отвечаешь на большое ТЗ.
   Если для работы чего-то не хватает (доступы, материалы) — попроси одним конкретным списком.
7. Если отвечать не нужно (заказчик написал «спасибо»/«ок», системное уведомление) — выведи ровно «НЕ НУЖЕН: <почему>».
8. Чат биржи не понимает разметку: никаких **звёздочек**, #заголовков и markdown. Списки — строками с «—» или «1.».
Выведи только текст ответа заказчику — без кавычек, пояснений и подписи."""


def conversation(item: dict, file_blocks: list[dict]) -> list[dict]:
    lines = [f"Площадка: {item['site']}. Тема: {item.get('subject') or 'не указана'}", "", "Переписка (старое → новое):"]
    for m in item["history"]:
        if m in item["new"]:
            continue
        who = "Я" if m["mine"] else "Заказчик"
        lines.append(f"[{who}] {m['text'][:1500]}" + (f"  (файлы: {', '.join(f['name'] for f in m['files'])})" if m["files"] else ""))
    lines += ["", "НОВЫЕ сообщения заказчика — на них отвечаем:"]
    for m in item["new"]:
        lines.append(f"[Заказчик] {m['text']}" + (f"  (файлы: {', '.join(f['name'] for f in m['files'])})" if m["files"] else ""))
    return [{"type": "text", "text": "\n".join(lines)}] + file_blocks


def ask(item: dict, content: list[dict], extra: list[dict] | None = None) -> str:
    site = "Kwork" if item["site"] == "kwork" else "FL.ru"
    msgs = [{"role": "user", "content": content}] + (extra or [])
    r = rag._get_claude().messages.create(model=rag.CLAUDE_MODEL, max_tokens=2500, system=SYSTEM.format(site=site),
                                          messages=msgs)
    return "".join(b.text for b in r.content if b.type == "text").strip()


def draft(item: dict, file_blocks: list[dict]) -> str:
    try:
        return ask(item, conversation(item, file_blocks))
    except Exception as e:
        if not file_blocks or "credit balance" in str(e).lower():
            raise
        log.warning("%s: вложения не приняты моделью, черновик без них", item["key"])
        item["att_error"] = "нейросеть не приняла вложения — черновик без них"
        return ask(item, conversation(item, []))


def revise(item: dict, old: str, wish: str) -> str:
    return ask(item, conversation(item, []), [
        {"role": "assistant", "content": old},
        {"role": "user", "content": f"Перепиши ответ с учётом пожелания: «{wish}». Всё, чего пожелание не касается, "
                                    "оставь как было. Выведи только новый текст ответа."}])


def problems(text: str) -> list[str]:
    probs = [b for b in fl_watch.BANNED if b not in (r"\bделал[аи]?\b",) and re.search(b, text, re.I)]
    if re.search(r"\*\*|^#+ ", text, re.M):
        probs.append("markdown — в чате будут видны звёздочки")
    if len(text) > 4000:
        probs.append(f"длинно: {len(text)} символов")
    return probs


# ---------- Telegram ----------
def card(item: dict, text: str, status: str = "new") -> str:
    e = html.escape
    site = "Kwork" if item["site"] == "kwork" else "FL.ru"
    head = f"💬 <b>{site} · {e(item['peer'])}</b>" + (f" — {e(item['subject'])}" if item.get("subject") else "")
    said = "\n\n".join(m["text"] for m in item["new"])
    lines = [head, "", f"<i>{e(said[:1500])}{'…' if len(said) > 1500 else ''}</i>"]
    files = [f["name"] for m in item["new"] for f in m["files"]]
    if files:
        lines.append(f"📎 {e(', '.join(files))}" + (f" · ⚠️ {e(item['att_error'])}" if item.get("att_error") else " · прочитал"))
    lines.append("")
    if text.startswith("НЕ НУЖЕН"):
        lines.append(f"🤷 Ответ, похоже, не нужен: {e(text.split(':', 1)[-1].strip())}")
    else:
        lines += ["✍️ <b>Черновик ответа:</b>", e(text[:2300])]
        probs = problems(text)
        if probs:
            lines.append(f"\n❗ проверь: {e(', '.join(probs))}")
    if status == "sent":
        lines.append("\n✅ <b>Отправлено</b>")
    return "\n".join(lines)[:4000]


def keyboard(item_id: int, url: str, sendable: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if sendable:
        rows.append([InlineKeyboardButton(text="🚀 Отправить", callback_data=f"cs:{item_id}"),
                     InlineKeyboardButton(text="✏️ Править", callback_data=f"ce:{item_id}")])
    rows.append([InlineKeyboardButton(text="✍️ Свой текст", callback_data=f"co:{item_id}"),
                 InlineKeyboardButton(text="🙈 Без ответа", callback_data=f"cx:{item_id}")])
    rows.append([InlineKeyboardButton(text="Открыть чат", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def post_card(bot, chat: int, c, item_id: int, item: dict, text: str):
    sent = await bot.send_message(chat, card(item, text), parse_mode="HTML", disable_web_page_preview=True,
                                  reply_markup=keyboard(item_id, item["url"], not text.startswith("НЕ НУЖЕН")))
    c.execute("update chat_items set draft=?, status='new' where id=?", (text, item_id))
    c.execute("insert or replace into chat_msgs values (?,?)", (sent.message_id, item_id))
    c.commit()


# ---------- отправка ответа заказчику ----------
async def send_reply(item: dict, text: str) -> bool:
    """Пишем в чат биржи в отдельной вкладке (сразу закрываем) и проверяем, что сообщение появилось."""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            if item["site"] == "kwork":
                ed = page.locator(".trumbowyg-editor").first
                await ed.click()
                body = "<br>".join(html.escape(x) for x in text.split("\n"))
                await page.evaluate("(h) => { const e = document.querySelector('.trumbowyg-editor'); e.innerHTML = h;"
                                    " e.dispatchEvent(new Event('input', {bubbles: true})); }", body)
                await page.keyboard.press("End"); await page.keyboard.type(" "); await page.keyboard.press("Backspace")
                await page.wait_for_timeout(600)
                if len(await page.eval_on_selector("#message_body", "e => e.value")) < len(text) // 2:
                    raise RuntimeError("текст не попал в поле сообщения Kwork")
                await page.locator(".js-box-submit.btn-send-message").first.click()
                await page.wait_for_timeout(5000)
                hist = await kwork_history(ctx.request, item["peer_id"], item["peer"])
            else:
                box = page.locator("textarea:visible").last
                await box.fill(text)
                await page.wait_for_timeout(600)
                await page.locator("button.uw__round-button:visible").last.click()
                await page.wait_for_timeout(5000)
                r = await ctx.request.get(f"https://www.fl.ru/projects/{item['project_id']}/offers/{item['offer_id']}"
                                          "/messages/?limit=10&offset=0", headers=XHR)
                hist = [{"mine": True, "text": clean(m.get("text"))} for m in (await r.json())["items"]]
            probe = re.sub(r"\s+", " ", text)[:60]
            return any(m["mine"] and probe in re.sub(r"\s+", " ", m["text"]) for m in hist)
        finally:
            await page.close()


# ---------- цикл ----------
async def run_once(bot, chat: int) -> int:
    c = db()
    found = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
        except Exception:
            log.info("Chrome не запущен — сообщения не проверяю")
            return 0
        req = browser.contexts[0].request
        for name, fn in (("Kwork", kwork_new), ("FL", fl_new)):
            try:
                for item in await fn(req, c):
                    if "notice" not in item:
                        item["paths"] = [str(x) for x in await download_files(req, item)]
                    found.append(item)
            except Exception:
                log.exception("сообщения %s", name)
    for item in found:
        if "notice" in item:
            await bot.send_message(chat, "📨 " + item["notice"], reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть чаты FL.ru", url=item["url"])]]))
            set_seen(c, item["key"], item["last"])
            continue
        blocks, read, skipped = attachments.to_blocks([Path(x) for x in item.pop("paths")])
        if skipped:
            item["att_error"] = "не прочитал: " + ", ".join(skipped)
        text = await asyncio.to_thread(draft, item, blocks)
        cur = c.execute("insert into chat_items (item_json, created) values (?,?)",
                        (json.dumps(item, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
        await post_card(bot, chat, c, cur.lastrowid, item, text)
        set_seen(c, item["key"], item["last"])       # только после карточки: упали раньше — повторим в след. проход
    return len(found)


async def watch_forever(bot, chat: int, every: int = int(os.getenv("INBOX_EVERY_SEC") or 120)):
    while True:
        try:
            n = await run_once(bot, chat)
            if n:
                log.info("новых сообщений от заказчиков: %s", n)
        except Exception:
            log.exception("проверка сообщений")
        await asyncio.sleep(every)


# ---------- кнопки и ответы в Telegram ----------
@router.callback_query(F.data.startswith("cs:"))
async def on_send(cb: CallbackQuery):
    item_id = int(cb.data[3:])
    c = db()
    got = load(c, item_id)
    if not got:
        await cb.answer("Не нашёл это сообщение", show_alert=True)
        return
    item, text, status = got
    if status in ("sending", "sent"):
        await cb.answer("Уже отправлено или отправляется", show_alert=True)
        return
    c.execute("update chat_items set status='sending' where id=?", (item_id,))
    c.commit()
    await cb.answer("Отправляю…")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        ok = await send_reply(item, text)
    except Exception as e:
        log.exception("отправка ответа")
        ok, err = None, str(e)[:200]
    if ok:
        c.execute("update chat_items set status='sent' where id=?", (item_id,))
        c.commit()
        await cb.message.edit_text(card(item, text, "sent"), parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                       [InlineKeyboardButton(text="Открыть чат", url=item["url"])]]))
        return
    c.execute("update chat_items set status='new' where id=?", (item_id,))
    c.commit()
    why = f"ошибка: {err}" if ok is None else "кнопку нажал, но в чате сообщения не вижу"
    await cb.message.answer(f"⚠️ Ответ, возможно, не ушёл ({why}). Открой чат и проверь, прежде чем отправлять снова.",
                            reply_markup=keyboard(item_id, item["url"]))


@router.callback_query(F.data.startswith("ce:") | F.data.startswith("co:"))
async def on_edit(cb: CallbackQuery):
    item_id, mode = int(cb.data[3:]), ("edit" if cb.data.startswith("ce:") else "own")
    pending[cb.from_user.id] = (item_id, mode)
    await cb.answer()
    if mode == "edit":
        await cb.message.answer("Что поправить в ответе? Например: «короче», «цена 6 000», «спроси про хостинг».",
                                reply_markup=ForceReply(input_field_placeholder="пожелание к ответу"))
    else:
        await cb.message.answer("Пришли текст ответа — отправлю заказчику как есть (сначала покажу, кнопкой подтвердишь).",
                                reply_markup=ForceReply(input_field_placeholder="текст ответа заказчику"))


@router.callback_query(F.data.startswith("cx:"))
async def on_skip(cb: CallbackQuery):
    c = db()
    c.execute("update chat_items set status='skipped' where id=?", (int(cb.data[3:]),))
    c.commit()
    await cb.answer("Ок, без ответа")
    await cb.message.edit_reply_markup(reply_markup=None)


async def handle_text(message: Message, bot) -> bool:
    """Текст после «Править»/«Свой текст» или ответ (reply) на карточку сообщения. True — обработали здесь."""
    c = db()
    mode = None
    if message.from_user.id in pending:
        item_id, mode = pending.pop(message.from_user.id)
    elif message.reply_to_message and (item_id := item_by_msg(c, message.reply_to_message.message_id)):
        mode = "edit"
    if not mode:
        return False
    got = load(c, item_id)
    if not got:
        await message.answer("Не нашёл это сообщение — возможно, база бота очищалась.")
        return True
    item, old, _ = got
    if mode == "own":
        text = message.text.strip()
    else:
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            text = await asyncio.to_thread(revise, item, old or "", message.text)
        except Exception:
            log.exception("правка ответа")
            await message.answer("Не получилось переписать ответ, попробуй ещё раз.")
            return True
    await post_card(bot, message.chat.id, c, item_id, item, text)
    return True
