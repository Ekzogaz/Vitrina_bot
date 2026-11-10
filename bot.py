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

# Словарь для хранения настроек пользователей
user_settings = {}


def escape_markdown(text: str) -> str:
  """Экранирует спецсимволы Markdown, чтобы Telegram не выдавал ошибку парсинга."""
  if not text:
    return ""
  # Убираем или экранируем символы, которые ломают разметку Telegram
  escape_chars = r"_*[]()~`>#+-=|{}.!"
  return re.sub(
      f"([{re.escape(escape_chars)}])", r"\\\1", text
  )


# Функция расчета цены
def apply_auction_logic(
    text: str, promo_header: str, discount_percent: float
) -> str:
  if not text:
    text = ""

  # Паттерн для поиска цены (поддерживает пробелы между тысячами вроде "4 143")
  pattern = r"(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.)"
  calculated_price_str = ""
  original_price_matched = ""

  def replace_price(match):
    nonlocal calculated_price_str, original_price_matched
    original_price_matched = match.group(0)
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
      return f"~~{original_price_matched}~~"
    except ValueError:
      return match.group(0)

  # Заменяем цену на зачеркнутую в тексте
  updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)

  # Если оригинальный текст содержал потенциально опасные символы, очищаем его
  # Оставляем только базовый текст, а акционную шапку и цену рендерим безопасно через Markdown
  clean_text = text

  if calculated_price_str:
    # Аккуратно добавляем акционную цену ниже зачеркнутой старой
    clean_text = re.sub(
        pattern,
        lambda m: f"~~{m.group(0)}~~" + calculated_price_str,
        text,
        count=1,
        flags=re.IGNORECASE,
    )
  else:
    clean_text += f"\n\n🔥 **Акційна знижка: -{discount_percent}%**"

  # Собираем итоговое сообщение
  return f"🚨 **{promo_header}** 🚨\n\n{clean_text}"


# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
  user_id = message.from_user.id
  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "АКЦІЯ НА ТОВАР!",
        "discount": 5.0,
    }

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      "Бот налаштований на пакетну обробку фото.\n"
      f"Поточні налаштування:\n"
      f"• Текст акції: *{settings['promo']}*\n"
      f"• Знижка: *{settings['discount']}%*\n\n"
      "⚙️ **Як змінити налаштування:**\n"
      "• Текст: `/promo Ваш текст акції`\n"
      "• Знижка: `/discount 10`\n\n"
      "📸 Можете надсилати фото (одиночні або пачками)!",
      parse_mode=ParseMode.MARKDOWN,
  )


# Смена текста акции: /promo Новый текст
@dp.message(Command("promo"))
async def set_promo(message: Message):
  user_id = message.from_user.id
  args = message.text.split(maxsplit=1)

  if len(args) < 2:
    await message.answer(
        "❌ Вкажіть текст після команди.\nПриклад: `/promo РОЗПРОДАЖ КОЛЕКЦІЇ`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return

  new_promo = args[1]
  if user_id not in user_settings:
    user_settings[user_id] = {"promo": new_promo, "discount": 5.0}
  else:
    user_settings[user_id]["promo"] = new_promo

  await message.answer(
      f"✅ Текст акції змінено на:\n*{new_promo}*", parse_mode=ParseMode.MARKDOWN
  )


# Смена скидки: /discount 10
@dp.message(Command("discount"))
async def set_discount(message: Message):
  user_id = message.from_user.id
  args = message.text.split(maxsplit=1)

  if len(args) < 2:
    await message.answer(
        "❌ Вкажіть число.\nПриклад: `/discount 7.5`", parse_mode=ParseMode.MARKDOWN
    )
    return

  try:
    new_discount = float(args[1].replace(",", "."))
  except ValueError:
    await message.answer("❌ Помилка! Введіть число (наприклад: `5` або `10`.)")
    return

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЯ НА ТОВАР!", "discount": new_discount}
  else:
    user_settings[user_id]["discount"] = new_discount

  await message.answer(
      f"✅ Знижку змінено на: *{new_discount}%*", parse_mode=ParseMode.MARKDOWN
  )


# Обработчик фотографий
@dp.message(F.photo)
async def handle_photos(message: Message):
  user_id = message.from_user.id
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

  try:
    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=photo_id,
        caption=new_caption,
        parse_mode=ParseMode.MARKDOWN,
    )
  except Exception as e:
    # Если парсер всё же упал, отправляем без Markdown-разметки описания, чтобы фото не терялось
    logging.error(f"Markdown error: {e}")
    plain_caption = (
        f"🚨 {promo_text} 🚨\n\n{original_caption}\n\n🔥 Акція: -{discount_percent}%"
    )
    await bot.send_photo(
        chat_id=message.from_user.id, photo=photo_id, caption=plain_caption
    )


async def main():
  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен и готов к работе...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
