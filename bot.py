import asyncio
import html
import logging
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

# .env — до импорта своих модулей: они читают настройки (MIN_BUDGET и др.) при загрузке
load_dotenv(Path(__file__).parent / ".env")

import fl_submit  # noqa: E402
import inbox_watch  # noqa: E402
from inbox_watch import ack  # noqa: E402
import kwork_submit  # noqa: E402
import fl_watch  # noqa: E402
from rag import draft_reply  # noqa: E402

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)   # кому слать новые заказы FL.ru и кому отвечать
FL_EVERY = int(os.getenv("FL_EVERY_SEC") or 60)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

HELP_TEXT = (
    "Слежу за новыми заказами FL.ru (IT, сайты, дизайн, боты, AI) и присылаю их сюда с готовым черновиком "
    "отклика и ценой.\n\n"
    "Под карточкой: «✏️ Править» — напиши пожелание (или просто ответь на карточку), перепишу черновик; "
    "«🚀 Утвердить и отправить» — сразу отправлю отклик на бирже (без предпросмотра) и пришлю итог: "
    "ушёл или нет, сколько откликов осталось.\n\n"
    "Сообщения от заказчиков Kwork и FL.ru тоже присылаю сюда с черновиком ответа: «🚀 Отправить», «✏️ Править», "
    "«✍️ Свой текст» (отправлю твой текст как есть) или «Без ответа». Без кнопки заказчику ничего не уходит.\n\n"
    "Можно и вручную: пришли текст любого заказа — предложу черновик отклика по базе услуг.\n\n"
    "/cost — сколько потрачено на нейросеть: сегодня, за неделю, за месяц."
)

# бот личный: чужим не отвечает (иначе любой может тратить ваш ключ нейросети)
dp.message.filter(F.from_user.id == ADMIN_ID)
dp.callback_query.filter(F.from_user.id == ADMIN_ID)
inbox_watch.router.message.filter(F.from_user.id == ADMIN_ID)
inbox_watch.router.callback_query.filter(F.from_user.id == ADMIN_ID)
dp.include_router(inbox_watch.router)

# заказ, для которого ждём пожелание после кнопки «Править»
pending_edit: dict[int, str] = {}


def kb(order_id: str, url: str, status: str = "new") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.model_validate(fl_watch.keyboard(order_id, url, status))


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("cost"))
async def cmd_cost(message: Message):
    """Реальный расход на нейросеть по строкам api_usage (пишутся с 10.10.2026)."""
    import sqlite3
    import aiohttp
    c = sqlite3.connect(fl_watch.DB)
    c.execute("create table if not exists api_usage (at text, model text, inp int, out int, cw int, cr int, usd real)")
    rows = {name: c.execute(f"select count(*), coalesce(sum(usd), 0) from api_usage where at >= date('now', 'localtime', '{d}')").fetchone()
            for name, d in (("сегодня", "start of day"), ("7 дней", "-6 days"), ("30 дней", "-29 days"))}
    drafts = c.execute("select count(*) from seen where verdict='sent' and at >= date('now', 'localtime', '-6 days')").fetchone()[0]
    rate = None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=aiohttp.ClientTimeout(total=10)) as r:
                rate = (await r.json(content_type=None))["Valute"]["USD"]["Value"]
    except Exception:
        pass
    rub = (lambda usd: f" ≈ {usd * rate:,.0f} ₽".replace(",", " ")) if rate else (lambda usd: "")
    lines = [f"💸 Расход на нейросеть" + (f" (курс ЦБ {rate:.2f} ₽/$)" if rate else "")]
    for name, (n, usd) in rows.items():
        lines.append(f"{name}: ${usd:.2f}{rub(usd)} · запросов {n}")
    week = rows["7 дней"][1]
    if drafts:
        lines.append(f"на один черновик за 7 дней: ${week / drafts:.3f}{rub(week / drafts)} (черновиков {drafts})")
    lines.append(f"прогноз на месяц по последним 7 дням: ${week / 7 * 30:.0f}{rub(week / 7 * 30)}")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("edit:"))
async def on_edit(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    pending_edit[cb.from_user.id] = order_id
    await ack(cb)
    await cb.message.answer("Что поправить в черновике? Например: «короче», «цена 3 000», «убери про Figma».",
                            reply_markup=ForceReply(input_field_placeholder="пожелание к черновику"))


def send_kb(order_id: str, url: str, left) -> InlineKeyboardMarkup:
    site = "Kwork" if "kwork.ru" in url else "FL.ru"
    tail = f" (останется {left - 1})" if isinstance(left, int) else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Отправить на {site}{tail}", callback_data=f"send:{order_id}")],
        [InlineKeyboardButton(text="✏️ Править", callback_data=f"edit:{order_id}"),
         InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{order_id}")],
        [InlineKeyboardButton(text=f"Открыть заказ на {site}", url=url)]])


SEND_TIMEOUT = int(os.getenv("SEND_TIMEOUT_SEC") or 150)     # дольше — считаем, что отправка зависла, и сверяемся с биржей


def money(v: int) -> str:
    return f"{v:,}".replace(",", " ")


async def confirm_sent(o) -> bool | None:
    """Независимая проверка по бирже: есть ли наш отклик/предложение на этом заказе. None — проверить не вышло."""
    if o.source == "kwork":
        return await kwork_submit.offer_exists(o.link)
    return await fl_submit.offer_exists(o.id)


def sent_text(o, d, res: dict) -> str:
    """Итоговое сообщение об успешной отправке."""
    if o.source == "kwork":
        price = res.get("price") or d["price"]
        note = f" (поднял до минимума Kwork, в черновике {money(d['price'])})" if price != d["price"] else ""
        lines = [f"✅ <b>Предложение отправлено на Kwork</b>: «{html.escape(o.title)}»",
                 f"💰 заказчик платит {money(price)} ₽{note}, тебе {money(round(price * 0.8))} ₽ (−20% Kwork)"
                 + (f" · {res['term']}" if res.get("term") else "")]
        if res.get("competitors"):
            lines.append(f"👥 предложений уже: {res['competitors']}")
        return "\n".join(lines)
    lines = [f"✅ <b>Отклик отправлен на FL.ru</b>: «{html.escape(o.title)}»",
             f"💰 {money(d['price'])} ₽ · {d['days']} дн."]
    if d.get("works"):
        lines.append(f"📎 работ прикреплено: {len(d['works'])}")
    if res.get("competitors"):
        lines.append(f"👥 откликов уже: {res['competitors']}")
    if res.get("left") is not None:
        lines.append(f"🎟 откликов осталось: {res['left']}")
    if res.get("already"):
        lines.append("ℹ️ Отклик на этот заказ уже был — второй не отправлял.")
    return "\n".join(lines)


async def send_now(cb: CallbackQuery, order_id: str):
    """«Утвердить и отправить»: сразу заполняем форму и отправляем, без предпросмотра. Итоговое сообщение приходит
    всегда — успех, отказ биржи или «не дождался» (тогда сверяемся с самой биржей, ушёл ли отклик)."""
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await ack(cb, "Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, status, _ = got
    if not d.get("price") or not d.get("days"):
        await ack(cb, "В черновике нет цены или срока — поправь через «Править»", show_alert=True)
        return
    if status in ("sending", "sent_fl", "sent_kw"):
        await ack(cb, "Уже отправлено или отправляется", show_alert=True)
        return
    kw = o.source == "kwork"
    site = "Kwork" if kw else "FL.ru"
    found = fl_watch.contacts_in(d["reply"])
    if found:
        # ник, телефон, ссылка или почта в отклике — биржа может заблокировать аккаунт; отправлять не будем
        await ack(cb)
        await cb.message.answer(f"⚠️ <b>Не отправляю</b>: в тексте контакты — {html.escape(', '.join(found))}. "
                                f"{site} за такое может заблокировать аккаунт. Убери их через «✏️ Править» "
                                "(например: «убери ник и ссылку») и утверди снова.", parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup.model_validate(fl_watch.keyboard(order_id, o.link)))
        return
    c.execute("update drafts set status='sending' where order_id=?", (order_id,))
    c.commit()
    await ack(cb, f"Отправляю на {site}…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    wait = await cb.message.answer(f"⏳ Отправляю на {site}…")

    res, problem = None, None
    try:
        coro = (kwork_submit.submit(o.link, d["reply"], d["price"], d["days"], o.title) if kw else
                fl_submit.submit(o.link, d["reply"], d["price"], d["days"], d.get("works", []), o.id))
        res = await asyncio.wait_for(coro, SEND_TIMEOUT)
    except asyncio.TimeoutError:
        problem = f"не дождался ответа от {site} за {SEND_TIMEOUT} с"
        logging.error("отправка %s зависла дольше %s с", order_id, SEND_TIMEOUT)
    except fl_submit.NotReady as e:
        problem = str(e)
    except Exception:
        logging.exception("submit")
        problem = "ошибка при отправке"

    ok = bool(res and res.get("ok"))
    if not ok:
        # результат неясен или отказ — смотрим на самой бирже: вдруг отклик всё же ушёл (так было при зависании)
        if await confirm_sent(o):
            ok, res = True, {**(res or {}), "already": bool(problem and "уже" in problem)}
            if kw:
                res.setdefault("price", d["price"])

    if ok:
        c.execute("update drafts set status=? where order_id=?", ("sent_kw" if kw else "sent_fl", order_id))
        c.commit()
        text = sent_text(o, d, res)
    else:
        c.execute("update drafts set status='new' where order_id=?", (order_id,))
        c.commit()
        text = (f"⚠️ <b>Отклик не отправлен</b>: {html.escape((problem or f'не вижу его на {site}').rstrip('.'))}.\n"
                + ("Открой заказ и проверь вручную, прежде чем отправлять ещё раз." if not problem or "дождался" in problem
                   else "Поправь и нажми «Утвердить и отправить» ещё раз."))
    markup = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"Открыть заказ на {site}", url=o.link)]])
              if ok or not problem or "дождался" in problem else InlineKeyboardMarkup.model_validate(fl_watch.keyboard(order_id, o.link)))
    try:
        await wait.delete()
    except Exception:
        pass
    await cb.message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith("ok:"))
async def on_approve(cb: CallbackQuery):
    """Утвердить и отправить сразу (предпросмотр убран)."""
    await send_now(cb, cb.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("send:"))
async def on_send(cb: CallbackQuery):
    """Кнопка «Отправить» на старых карточках с предпросмотром — работает так же."""
    await send_now(cb, cb.data.split(":", 1)[1])


async def reconcile_sending(bot_: Bot):
    """После перезапуска/зависания в базе могли остаться заказы «отправляется». Сверяем их с биржей и сообщаем итог."""
    c = fl_watch.db()
    stuck = c.execute("select order_id from drafts where status='sending'").fetchall()
    for (oid,) in stuck:
        got = fl_watch.load_draft(c, oid)
        if not got:
            continue
        o = got[0]
        exists = await confirm_sent(o)
        if exists is None:
            continue                                         # биржа/Chrome недоступны — проверим при следующем запуске
        c.execute("update drafts set status=? where order_id=?",
                  (("sent_kw" if o.source == "kwork" else "sent_fl") if exists else "new", oid))
        c.commit()
        site = "Kwork" if o.source == "kwork" else "FL.ru"
        await bot_.send_message(ADMIN_ID, (f"✅ Отправка на {site} прервалась на «{html.escape(o.title)}», но отклик на бирже "
                                           "есть — отметил как отправленный." if exists else
                                           f"ℹ️ Отправка на {site} прервалась на «{html.escape(o.title)}», отклика на бирже нет "
                                           "— можно утвердить заново."), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                    text=f"Открыть заказ на {site}", url=o.link)]]))


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(cb: CallbackQuery):
    await ack(cb, "Не отправляю")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Ок, отклик не отправлен. Карточку можно поправить и утвердить снова.")


async def revise_and_send(message: Message, order_id: str, wish: str):
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await message.answer("Не нашёл этот заказ — возможно, бот перезапускался с чистой базой.")
        return
    o, tri, d, _, ver = got
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        new = await asyncio.to_thread(fl_watch.revise, o, d, wish)
    except Exception:
        logging.exception("Ошибка правки черновика")
        await message.answer("Не получилось переписать черновик, попробуй ещё раз.")
        return
    sent = await message.answer(fl_watch.card(o, tri, new, ver + 1), parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb(order_id, o.link))
    fl_watch.save_draft(c, o, tri, new, sent.message_id)


@dp.message(F.text)
async def handle_text(message: Message):
    # 0) переписка с заказчиком: правка ответа / свой текст / reply на карточку сообщения
    if await inbox_watch.handle_text(message, bot):
        return
    c = fl_watch.db()
    # 1) ответ (reply) на карточку заказа — это пожелание к черновику
    if message.reply_to_message:
        order_id = fl_watch.order_by_msg(c, message.reply_to_message.message_id)
        if order_id:
            pending_edit.pop(message.from_user.id, None)
            await revise_and_send(message, order_id, message.text)
            return
    # 2) после кнопки «Править»
    if message.from_user.id in pending_edit:
        await revise_and_send(message, pending_edit.pop(message.from_user.id), message.text)
        return
    # 3) иначе — вставленный вручную текст заказа
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        text, chunks = await asyncio.to_thread(draft_reply, message.text)
    except Exception:
        logging.exception("Ошибка при составлении черновика")
        await message.answer("Не получилось обработать заказ, попробуй ещё раз.")
        return
    await message.answer(text)


async def stop_at_night(watcher=None):
    """В конце рабочего дня останавливаемся сами; утром бота запустит Планировщик Windows (start_bot.bat).
    Если в очереди есть ответы Kwork (ждут конца проверки заказчика) — до 01:00 НСК работаем только ради них:
    Kwork проверяет до 21:00 МСК, и ответ должен уйти сразу, а не утром."""
    if fl_watch.in_work_hours():
        # запуск ради очереди после полуночи: «до 22:00» считалось бы до завтрашних 22:00 — бот висел бы весь день
        await asyncio.sleep(fl_watch.seconds_until_end())
    if inbox_watch.queued_count() and inbox_watch.in_queue_hours():
        logging.info("рабочий день окончен, но в очереди ответы Kwork — слежу только за ними до %02d:00",
                     inbox_watch.QUEUE_END_HOUR)
        if watcher:
            watcher.cancel()                                 # новые заказы — утром
        inbox_watch.queue_only = True
        while inbox_watch.queued_count() and inbox_watch.in_queue_hours():
            await asyncio.sleep(60)
    logging.info("рабочий день окончен (%s) — останавливаюсь до утра", fl_watch.WORK_HOURS)
    await dp.stop_polling()


async def main():
    # пауза «на сегодня»: в pause_until.txt дата (ГГГГ-ММ-ДД) — до неё включительно бот не запускается
    pause = Path(__file__).parent / "pause_until.txt"
    if pause.exists() and pause.read_text(encoding="utf-8").strip() >= datetime.now(fl_watch.NSK).date().isoformat():
        logging.info("бот на паузе до %s включительно (pause_until.txt)", pause.read_text(encoding="utf-8").strip())
        return
    lock = fl_watch.single_instance()
    if lock is None:
        logging.info("бот уже запущен — второй экземпляр не нужен")
        return
    # ПК включили между 22:00 и 01:00, а в очереди ответы Kwork — запускаемся только ради очереди
    queue_mode = not fl_watch.in_work_hours() and inbox_watch.queued_count() > 0 and inbox_watch.in_queue_hours()
    if not fl_watch.in_work_hours() and not queue_mode:
        logging.info("сейчас нерабочее время (%s, Новосибирск) — запустит Планировщик утром", fl_watch.WORK_HOURS)
        return
    # в 8:00 / сразу после включения ПК сети или Telegram может ещё не быть — ждём, а не падаем
    while True:
        try:
            await bot.get_me()
            break
        except TelegramNetworkError as e:
            if not fl_watch.in_work_hours() and not queue_mode:
                return
            logging.warning("Telegram пока недоступен (%s) — повтор через 30 с", e)
            await asyncio.sleep(30)
    if queue_mode:
        logging.info("нерабочее время, но в очереди ответы Kwork — слежу только за ними")
        inbox_watch.queue_only = True
        watcher = asyncio.create_task(asyncio.sleep(0))      # заказы не отслеживаем
    else:
        asyncio.create_task(reconcile_sending(bot))      # зависшие «отправляется» после прошлого запуска
        watcher = asyncio.create_task(fl_watch.watch_forever(BOT_TOKEN, ADMIN_ID, every=FL_EVERY))
    inbox = asyncio.create_task(inbox_watch.watch_forever(bot, ADMIN_ID))
    night = asyncio.create_task(stop_at_night(watcher))
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        inbox.cancel()
        night.cancel()
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
