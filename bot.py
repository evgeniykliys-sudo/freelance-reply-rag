import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

import fl_submit
import fl_watch
from rag import draft_reply

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)   # кому слать новые заказы FL.ru и кому отвечать
FL_EVERY = int(os.getenv("FL_EVERY_SEC") or 180)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

HELP_TEXT = (
    "Слежу за новыми заказами FL.ru (IT, сайты, дизайн, боты, AI) и присылаю их сюда с готовым черновиком "
    "отклика и ценой.\n\n"
    "Под карточкой: «✏️ Править» — напиши пожелание (или просто ответь на карточку), перепишу черновик; "
    "«✅ Утвердить» — зафиксирую итоговый текст.\n\n"
    "Можно и вручную: пришли текст любого заказа — предложу черновик отклика по базе услуг."
)

# бот личный: чужим не отвечает (иначе любой может тратить ваш ключ нейросети)
dp.message.filter(F.from_user.id == ADMIN_ID)
dp.callback_query.filter(F.from_user.id == ADMIN_ID)

# заказ, для которого ждём пожелание после кнопки «Править»
pending_edit: dict[int, str] = {}


def kb(order_id: str, url: str, status: str = "new") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.model_validate(fl_watch.keyboard(order_id, url, status))


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@dp.callback_query(F.data.startswith("edit:"))
async def on_edit(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    pending_edit[cb.from_user.id] = order_id
    await cb.answer()
    await cb.message.answer("Что поправить в черновике? Например: «короче», «цена 3 000», «убери про Figma».",
                            reply_markup=ForceReply(input_field_placeholder="пожелание к черновику"))


def send_kb(order_id: str, url: str, left) -> InlineKeyboardMarkup:
    tail = f" (останется {left - 1})" if isinstance(left, int) else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 Отправить на FL.ru{tail}", callback_data=f"send:{order_id}")],
        [InlineKeyboardButton(text="✏️ Править", callback_data=f"edit:{order_id}"),
         InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{order_id}")],
        [InlineKeyboardButton(text="Открыть заказ на FL.ru", url=url)]])


@dp.callback_query(F.data.startswith("ok:"))
async def on_approve(cb: CallbackQuery):
    """Утвердить: заполняем форму отклика в Chrome (без отправки) и показываем скриншот."""
    order_id = cb.data.split(":", 1)[1]
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await cb.answer("Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, _, ver = got
    if not d.get("price") or not d.get("days"):
        await cb.answer("В черновике нет цены или срока — поправь через «Править»", show_alert=True)
        return
    await cb.answer("Заполняю форму отклика…")
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


@dp.callback_query(F.data.startswith("send:"))
async def on_send(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await cb.answer("Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, status, _ = got
    if status in ("sending", "sent_fl"):
        await cb.answer("Уже отправлено или отправляется", show_alert=True)
        return
    c.execute("update drafts set status='sending' where order_id=?", (order_id,))
    c.commit()
    await cb.answer("Отправляю…")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        res = await fl_submit.submit(o.link, d["reply"], d["price"], d["days"], d.get("works", []), o.id)
    except Exception as e:
        logging.exception("submit")
        c.execute("update drafts set status='approved' where order_id=?", (order_id,))
        c.commit()
        msg = str(e) if isinstance(e, fl_submit.NotReady) else "ошибка при отправке"
        await cb.message.answer(f"⚠️ Отклик не отправлен: {msg}. Проверь заказ вручную.",
                                reply_markup=send_kb(order_id, o.link, None))
        return
    c.execute("update drafts set status=? where order_id=?", ("sent_fl" if res["ok"] else "approved", order_id))
    c.commit()
    if res["ok"]:
        await cb.message.answer(f"✅ Отклик отправлен заказчику и виден в «Мои отклики»."
                                + (f"\n🎟 откликов осталось: {res['left']}" if res.get("left") is not None else ""))
    else:
        await cb.message.answer("❓ Кнопку нажал, но в «Мои отклики» заказ не нашёл. Открой заказ и проверь вручную, "
                                "прежде чем отправлять ещё раз (отклики платные).",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="Открыть заказ на FL.ru", url=o.link)]]))


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(cb: CallbackQuery):
    await cb.answer("Не отправляю")
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


async def main():
    watcher = asyncio.create_task(fl_watch.watch_forever(BOT_TOKEN, ADMIN_ID, every=FL_EVERY))
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
