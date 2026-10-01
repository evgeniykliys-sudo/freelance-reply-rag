import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

from rag import draft_reply

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

HELP_TEXT = (
    "Пришли мне текст заказа с Kwork/FL.ru или из Telegram-канала — я найду "
    "подходящую услугу в твоей базе (FREELANCE.md) и предложу черновик отклика.\n\n"
    "Если заказ не подходит под текущий профиль услуг или просит то, что ты "
    "не берёшь — скажу прямо, без черновика."
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(F.text)
async def handle_posting(message: Message):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        text, chunks = await asyncio.to_thread(draft_reply, message.text)
    except Exception:
        logging.exception("Ошибка при составлении черновика")
        await message.answer("Не получилось обработать заказ, попробуй ещё раз.")
        return

    await message.answer(text)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
