import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

import fl_submit
import inbox_watch
from inbox_watch import ack
import kwork_submit
import fl_watch
from rag import draft_reply

load_dotenv()

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
    "«✅ Утвердить» — зафиксирую итоговый текст.\n\n"
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


@dp.callback_query(F.data.startswith("ok:"))
async def on_approve(cb: CallbackQuery):
    """Утвердить: заполняем форму отклика в Chrome (без отправки) и показываем скриншот."""
    order_id = cb.data.split(":", 1)[1]
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await ack(cb, "Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, _, ver = got
    if not d.get("price") or not d.get("days"):
        await ack(cb, "В черновике нет цены или срока — поправь через «Править»", show_alert=True)
        return
    if o.source == "kwork":
        await approve_kwork(cb, c, o, d, order_id)
        return
    await ack(cb, "Заполняю форму отклика…")
    wait = await cb.message.answer("⏳ Открываю заказ в Chrome и заполняю форму (не отправляю)…")
    try:
        st = await fl_submit.prepare(o.link, d["reply"], d["price"], d["days"], d.get("works", []), order_id)
    except fl_submit.NotReady as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    except Exception:
        logging.exception("prepare")
        await wait.edit_text("⚠️ Не получилось заполнить форму — открой заказ и откликнись вручную.")
        return
    c.execute("update drafts set status='approved' where order_id=?", (order_id,))
    c.commit()
    comp = f"конкурентов: {st['competitors']}"
    if st.get("prices"):
        comp += f", цены {st['prices'][0]:,}–{st['prices'][1]:,} ₽".replace(",", " ")
    if st.get("terms"):
        comp += f", сроки {st['terms'][0]}–{st['terms'][1]} дн."
    caption = (f"Форма заполнена, <b>ещё не отправлено</b>.\n💰 {d['price']:,} ₽ · {d['days']} дн.".replace(",", " ")
               + f"\n👥 {comp}" + (f"\n🎟 откликов осталось: {st['left']}" if st.get("left") is not None else "")
               + f"\n📎 работ прикреплено: {len(st['works'])}")
    await wait.delete()
    await cb.message.answer_photo(FSInputFile(st["shot"]), caption=caption, parse_mode="HTML",
                                  reply_markup=send_kb(order_id, o.link, st.get("left")))


async def approve_kwork(cb: CallbackQuery, c, o, d, order_id: str):
    await ack(cb, "Заполняю форму предложения…")
    wait = await cb.message.answer("⏳ Открываю заказ на Kwork в Chrome и заполняю форму (не отправляю)…")
    try:
        st = await kwork_submit.prepare(o.link, d["reply"], d["price"], d["days"], o.title, order_id)
    except fl_submit.NotReady as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    except Exception:
        logging.exception("kwork prepare")
        await wait.edit_text("⚠️ Не получилось заполнить форму — открой заказ и предложи услугу вручную.")
        return
    c.execute("update drafts set status='approved' where order_id=?", (order_id,))
    c.commit()
    price = f"{st['price']:,}".replace(",", " ")
    note = f" (поднял до минимума Kwork, в черновике {d['price']})" if st["price"] != d["price"] else ""
    net = f"{round(st['price'] * 0.8):,}".replace(",", " ")     # Kwork берёт 20% — в «Моих откликах» видна эта сумма
    caption = (f"Форма заполнена, <b>ещё не отправлено</b>.\n💰 заказчик платит {price} ₽{note}, тебе {net} ₽ "
               f"(−20% Kwork) · {st['term']}"
               f"\n👥 предложений уже: {st['competitors']}"
               + ("\n⚠️ Kwork подсветил контакты/стоп-слова в тексте — поправь" if st.get("stopwords") else ""))
    await wait.delete()
    await cb.message.answer_photo(FSInputFile(st["shot"]), caption=caption, parse_mode="HTML",
                                  reply_markup=send_kb(order_id, o.link, None))


@dp.callback_query(F.data.startswith("send:"))
async def on_send(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await ack(cb, "Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, status, _ = got
    if status in ("sending", "sent_fl", "sent_kw"):
        await ack(cb, "Уже отправлено или отправляется", show_alert=True)
        return
    c.execute("update drafts set status='sending' where order_id=?", (order_id,))
    c.commit()
    await ack(cb, "Отправляю…")
    await cb.message.edit_reply_markup(reply_markup=None)
    kw = o.source == "kwork"
    try:
        if kw:
            res = await kwork_submit.submit(o.link, d["reply"], d["price"], d["days"], o.title)
        else:
            res = await fl_submit.submit(o.link, d["reply"], d["price"], d["days"], d.get("works", []), o.id)
    except Exception as e:
        logging.exception("submit")
        c.execute("update drafts set status='approved' where order_id=?", (order_id,))
        c.commit()
        msg = str(e) if isinstance(e, fl_submit.NotReady) else "ошибка при отправке"
        await cb.message.answer(f"⚠️ Отклик не отправлен: {msg}. Проверь заказ вручную.",
                                reply_markup=send_kb(order_id, o.link, None))
        return
    c.execute("update drafts set status=? where order_id=?",
              (("sent_kw" if kw else "sent_fl") if res["ok"] else "approved", order_id))
    c.commit()
    if res["ok"] and kw:
        await cb.message.answer("✅ Предложение отправлено заказчику на Kwork (видно в «Биржа → Мои предложения»).")
    elif res["ok"]:
        await cb.message.answer(f"✅ Отклик отправлен заказчику и виден в «Мои отклики»."
                                + (f"\n🎟 откликов осталось: {res['left']}" if res.get("left") is not None else ""))
    else:
        await cb.message.answer("❓ Кнопку нажал, но не убедился, что отклик ушёл. Открой заказ и проверь вручную, "
                                "прежде чем отправлять ещё раз.",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="Открыть заказ на " + ("Kwork" if kw else "FL.ru"),
                                                          url=o.link)]]))


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


async def stop_at_night():
    """В конце рабочего дня останавливаемся сами; утром бота запустит Планировщик Windows (start_bot.bat)."""
    await asyncio.sleep(fl_watch.seconds_until_end())
    logging.info("рабочий день окончен (%s) — останавливаюсь до утра", fl_watch.WORK_HOURS)
    await dp.stop_polling()


async def main():
    lock = fl_watch.single_instance()
    if lock is None:
        logging.info("бот уже запущен — второй экземпляр не нужен")
        return
    if not fl_watch.in_work_hours():
        logging.info("сейчас нерабочее время (%s, Новосибирск) — запустит Планировщик утром", fl_watch.WORK_HOURS)
        return
    # в 8:00 / сразу после включения ПК сети или Telegram может ещё не быть — ждём, а не падаем
    while True:
        try:
            await bot.get_me()
            break
        except TelegramNetworkError as e:
            if not fl_watch.in_work_hours():
                return
            logging.warning("Telegram пока недоступен (%s) — повтор через 30 с", e)
            await asyncio.sleep(30)
    watcher = asyncio.create_task(fl_watch.watch_forever(BOT_TOKEN, ADMIN_ID, every=FL_EVERY))
    inbox = asyncio.create_task(inbox_watch.watch_forever(bot, ADMIN_ID))
    night = asyncio.create_task(stop_at_night())
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        inbox.cancel()
        night.cancel()
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
