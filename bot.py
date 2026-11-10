import asyncio
import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

# Токен вашего бота
TOKEN = "9085949690:AAGAQLcj2ZBEG0FU1tqcHT6SZBm73w-gHtk"

bot = Bot(token=TOKEN)
dp = Dispatcher()


# Функция расчета скидки (автоматически находит цену, пересчитывает и ставит акцию)
def apply_auction_logic(text: str, discount_percent: float = 5.0) -> str:
    if not text:
        text = ""
    pattern = r"(\d+[\.,]?\d*)\s*(грн|uah|₴|грн\.)"
    calculated_price_str = ""

    def replace_price(match):
        nonlocal calculated_price_str
        original_price_str = match.group(1).replace(",", ".")
        currency = match.group(2)
        try:
            price = float(original_price_str)
            new_price = price * (1 - discount_percent / 100)
            new_price_formatted = (
                int(new_price) if new_price.is_integer() else round(new_price, 2)
            )
            calculated_price_str = (
                f"🔥 **Аукціонна ціна: {new_price_formatted} {currency} (-{discount_percent}%)**"
            )
            return f"~~{match.group(0)}~~ ➔ {calculated_price_str}"
        except ValueError:
            return match.group(0)

    updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)
    if not calculated_price_str:
        calculated_price_str = f"🔥 **Знижка на акцію: -{discount_percent}%**"

    promo_text = "🚨 **АКЦІЯ НА ТОВАР!** 🚨"
    return f"{promo_text}\n\n{updated_text}"


# Обработчик команды /start
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Бот успішно запущено у режимі Polling.\n\n"
        "Надішліть мені фото товару з описом та ціною, і я надішлю його вам зі знижкою!"
    )


# Обработчик фотографий с подписью
@dp.message(F.photo)
async def handle_photo(message: Message):
    original_caption = message.caption or ""
    new_caption = apply_auction_logic(original_caption, 5.0)
    photo_id = message.photo[-1].file_id

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=photo_id,
        caption=new_caption,
        parse_mode=ParseMode.MARKDOWN,
    )


async def main():
    # Удаляем вебхук на случай, если он остался активным с прошлого раза
    await bot.delete_webhook(drop_pending_updates=True)
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен и ожидает сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
