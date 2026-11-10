import asyncio
import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

# Токен вашего бота
TOKEN = "9085949690:AAGAQLcj2ZBEG0FU1tqcHT6SZBm73w-gHtk"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Словарь для хранения настроек пользователей (текст акции и скидка по умолчанию)
# Ключ — ID пользователя, значение — словарь с настройками
user_settings = {}


# Функция расчета цены
def apply_auction_logic(
    text: str, promo_header: str, discount_percent: float
) -> str:
  if not text:
    text = ""

  # Паттерн для поиска цены (поддерживает пробелы между тысячами вроде "4 143")
  pattern = r"(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.)"
  calculated_price_str = ""

  def replace_price(match):
    nonlocal calculated_price_str
    raw_number = match.group(1).replace(" ", "").replace(",", ".")
    currency = match.group(2)
    try:
      price = float(raw_number)
      new_price = price * (1 - discount_percent / 100)
      new_price_formatted = (
          int(new_price) if new_price.is_integer() else round(new_price, 2)
      )
      calculated_price_str = (
          f"\n\n🔥 **Акційна ціна: {new_price_formatted} {currency}**"
          f" (-{discount_percent}%)"
      )
      return f"~~{match.group(0)}~~{calculated_price_str}"
    except ValueError:
      return match.group(0)

  updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)

  if not calculated_price_str:
    updated_text += f"\n\n🔥 **Акційна знижка: -{discount_percent}%**"

  return f"🚨 **{promo_header}** 🚨\n\n{updated_text}"


# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
  user_id = message.from_user.id
  # Устанавливаем настройки по умолчанию, если их еще нет
  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "АКЦІЯ НА ТОВАР!",
        "discount": 5.0,
    }

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      "Бот налаштований на **пакетну обробку фото**.\n"
      f"Поточні налаштування:\n"
      f"• Текст акції: *{settings['promo']}*\n"
      f"• Знижка: *{settings['discount']}%*\n\n"
      "⚙️ **Як змінити налаштування:**\n"
      "• Щоб змінити текст: `/promo Ваш текст акції`\n"
      "• Щоб змінити знижку: `/discount 10` (або інший відсоток)\n\n"
      "📸 Після цього просто **кидайте пачку фото (до 10 штук за раз)**, і бот"
      " все обробить!",
      parse_mode=ParseMode.MARKDOWN,
  )


# Команда для смены текста акции: /promo Новый текст
@dp.message(Command("promo"))
async def set_promo(message: Message):
  user_id = message.from_user.id
  args = message.text.split(maxsplit=1)

  if len(args) < 2:
    await message.answer(
        "❌ Будь ласка, вкажіть текст після команди.\nПриклад:"
        " `/promo РОЗПРОДАЖ КОЛЕКЦІЇ`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return

  new_promo = args[1]
  if user_id not in user_settings:
    user_settings[user_id] = {"promo": new_promo, "discount": 5.0}
  else:
    user_settings[user_id]["promo"] = new_promo

  await message.answer(
      f"✅ Текст акції успішно змінено на:\n*{new_promo}*",
      parse_mode=ParseMode.MARKDOWN,
  )


# Команда для смены скидки: /discount 10
@dp.message(Command("discount"))
async def set_discount(message: Message):
  user_id = message.from_user.id
  args = message.text.split(maxsplit=1)

  if len(args) < 2:
    await message.answer(
        "❌ Будь ласка, вкажіть число.\nПриклад: `/discount 7.5`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return

  try:
    new_discount = float(args[1].replace(",", "."))
  except ValueError:
    await message.answer("❌ Помилка! Введіть число (наприклад: `5` або `10`).")
    return

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЯ НА ТОВАР!", "discount": new_discount}
  else:
    user_settings[user_id]["discount"] = new_discount

  await message.answer(
      f"✅ Розмір знижки успішно змінено на: *{new_discount}%*",
      parse_mode=ParseMode.MARKDOWN,
  )


# Обработчик фотографий (работает как с одиночными фото, так и с пачками/альбомами)
@dp.message(F.photo)
async def handle_photos(message: Message):
  user_id = message.from_user.id

  # Берем настройки пользователя или дефолтные
  settings = user_settings.get(
      user_id, {"promo": "АКЦІЯ НА ТОВАР!", "discount": 5.0}
  )

  promo_text = settings["promo"]
  discount_percent = settings["discount"]

  original_caption = message.caption or ""
  new_caption = apply_auction_logic(
      original_caption, promo_text, discount_percent
  )
  photo_id = message.photo[-1].file_id

  # Отправляем обработанное фото
  await bot.send_photo(
      chat_id=message.from_user.id,
      photo=photo_id,
      caption=new_caption,
      parse_mode=ParseMode.MARKDOWN,
  )


async def main():
  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен в режиме пакетной обработки...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
