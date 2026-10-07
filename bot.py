import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

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


@dp.callback_query(F.data.startswith("ok:"))
async def on_approve(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    c = fl_watch.db()
    got = fl_watch.load_draft(c, order_id)
    if not got:
        await cb.answer("Не нашёл этот заказ", show_alert=True)
        return
    o, tri, d, _, ver = got
    c.execute("update drafts set status='approved' where order_id=?", (order_id,))
    c.commit()
    await cb.answer("Утверждено")
    await cb.message.edit_reply_markup(reply_markup=kb(order_id, o.link, "approved"))
    await cb.message.answer(fl_watch.card(o, tri, d, ver, "approved"), parse_mode="HTML",
                            disable_web_page_preview=True, reply_markup=kb(order_id, o.link, "approved"))
    await cb.message.answer("Скопируй текст выше и вставь в отклик на FL.ru (кнопка «Открыть заказ»). "
                            "Цена и срок — в карточке.")


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
