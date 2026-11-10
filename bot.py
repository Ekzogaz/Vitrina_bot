import asyncio
import http.server
import logging
import os
import re
import threading
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup  # <-- Вот этот импорт был нужен
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Токен вашего бота
TOKEN = "9085949690:AAGAQLcj2ZBEG0FU1tqcHT6SZBm73w-gHtk"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Словарь для хранения настроек пользователей
user_settings = {}


# --- ВСТРОЕННЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ---
def run_dummy_web_server():
  port = int(os.environ.get("PORT", 10000))

  class HealthCheckHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
      self.send_response(200)
      self.end_headers()
      self.wfile.write(b"Bot is alive and running!")

    def log_message(self, format, *args):
      pass

  server_address = ("0.0.0.0", port)
  httpd = http.server.HTTPServer(server_address, HealthCheckHandler)
  print(f"Веб-заглушка запущена на порту {port}")
  httpd.serve_forever()


# Состояния для ввода через интерактивный диалог по кнопкам
class AdminStates(StatesGroup):
  waiting_for_custom_promo = State()
  waiting_for_custom_discount = State()


# Главная клавиатура с кнопками управления
def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="💎 Pre-Black Friday (-6%)"),
              KeyboardButton(text="🔥 Розпродаж (-10%)"),
          ],
          [
              KeyboardButton(text="✍️ Ввести свій текст акції"),
              KeyboardButton(text="🔢 Ввести свій % знижки"),
          ],
          [
              KeyboardButton(text="⚙️ Поточні налаштування"),
          ],
      ],
      resize_keyboard=True,
  )
  return keyboard


def apply_auction_logic(
    text: str, promo_header: str, discount_percent: float
) -> str:
  if not text:
    text = ""

  # Паттерн для поиска цены (поддерживает пробелы между тысячами вроде "8 572")
  pattern = r"(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.)"
  price_found = False

  def replace_price(match):
    nonlocal price_found
    price_found = True
    original_price_str = match.group(0)
    raw_number = match.group(1).replace(" ", "").replace(",", ".")
    currency = match.group(2)
    try:
      price = float(raw_number)
      new_price = price * (1 - discount_percent / 100)
      new_price_formatted = (
          int(new_price) if new_price.is_integer() else round(new_price, 2)
      )
      return (
          f"~~{original_price_str}~~"
          f"\n🔥 **Акційна ціна: {new_price_formatted} {currency}**"
          f" (-{discount_percent}%)"
      )
    except ValueError:
      return original_price_str

  updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)

  if not price_found:
    updated_text += f"\n\n🔥 **Акційна знижка: -{discount_percent}%**"

  return f"🚨 **{promo_header}** 🚨\n\n{updated_text}"


# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "Ювелірна Pre-Black Friday",
        "discount": 6.0,
    }

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      "Я бот для пакетної обробки фото товарів зі знижками.\n"
      f"📋 **Поточні налаштування:**\n"
      f"• Акція: *{settings['promo']}*\n"
      f"• Знижка: *{settings['discount']}%*\n\n"
      "Оберіть дію на клавіатурі нижче або просто **кидайте пачку фото** з"
      " товарами!",
      reply_markup=get_main_keyboard(),
      parse_mode=ParseMode.MARKDOWN,
  )


# Обработка нажатий на кнопки меню
@dp.message(
    F.text.in_({
        "💎 Pre-Black Friday (-6%)",
        "🔥 Розпродаж (-10%)",
        "✍️ Ввести свій текст акції",
        "🔢 Ввести свій % знижки",
        "⚙️ Поточні налаштування",
    })
)
async def handle_menu_buttons(message: Message, state: FSMContext):
  user_id = message.from_user.id
  text = message.text

  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "Ювелірна Pre-Black Friday",
        "discount": 6.0,
    }

  if text == "💎 Pre-Black Friday (-6%)":
    user_settings[user_id]["promo"] = "Ювелірна Pre-Black Friday"
    user_settings[user_id]["discount"] = 6.0
    await message.answer(
        "✅ Успішно встановлено:\n• Акція: *Ювелірна Pre-Black"
        " Friday*\n• Знижка: *6%*",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )

  elif text == "🔥 Розпродаж (-10%)":
    user_settings[user_id]["promo"] = "ГАВЯЧИЙ РОЗПРОДАЖ КОЛЕКЦІЇ"
    user_settings[user_id]["discount"] = 10.0
    await message.answer(
        "✅ Успішно встановлено:\n• Акція: *ГАВЯЧИЙ РОЗПРОДАЖ"
        " КОЛЕКЦІЇ*\n• Знижка: *10%*",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )

  elif text == "✍️ Ввести свій текст акції":
    await state.set_state(AdminStates.waiting_for_custom_promo)
    await message.answer(
        "✍️ Надішліть текстом новий заголовок для вашої акції:",
        reply_markup=ReplyKeyboardRemove(),
    )

  elif text == "🔢 Ввести свій % знижки":
    await state.set_state(AdminStates.waiting_for_custom_discount)
    await message.answer(
        "🔢 Введіть числом бажаний відсоток знижки (наприклад: `7.5` або `15`):",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )

  elif text == "⚙️ Поточні налаштування":
    settings = user_settings[user_id]
    await message.answer(
        f"📋 **Поточні налаштування:**\n"
        f"• Акція: *{settings['promo']}*\n"
        f"• Знижка: *{settings['discount']}%*",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# Шаг: сохранение своего текста акции
@dp.message(AdminStates.waiting_for_custom_promo, F.text)
async def save_custom_promo(message: Message, state: FSMContext):
  user_id = message.from_user.id
  new_promo = message.text.strip()

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": new_promo, "discount": 6.0}
  else:
    user_settings[user_id]["promo"] = new_promo

  await state.clear()
  await message.answer(
      f"✅ Текст акції змінено на:\n*{new_promo}*",
      reply_markup=get_main_keyboard(),
      parse_mode=ParseMode.MARKDOWN,
  )


# Шаг: сохранение своей скидки
@dp.message(AdminStates.waiting_for_custom_discount, F.text)
async def save_custom_discount(message: Message, state: FSMContext):
  user_id = message.from_user.id
  text_input = message.text.strip().replace(",", ".")

  try:
    new_discount = float(text_input)
  except ValueError:
    await message.answer(
        "❌ Помилка! Введіть лише число (наприклад: `7` або `12.5`):"
    )
    return

  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "Ювелірна Pre-Black Friday",
        "discount": new_discount,
    }
  else:
    user_settings[user_id]["discount"] = new_discount

  await state.clear()
  await message.answer(
      f"✅ Розмір знижки змінено на: *{new_discount}%*",
      reply_markup=get_main_keyboard(),
      parse_mode=ParseMode.MARKDOWN,
  )


# Обработчик фотографий
@dp.message(F.photo)
async def handle_photos(message: Message):
  user_id = message.from_user.id
  settings = user_settings.get(
      user_id, {"promo": "Ювелірна Pre-Black Friday", "discount": 6.0}
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
        reply_markup=get_main_keyboard(),
    )
  except Exception as e:
    logging.error(f"Markdown parse error: {e}")
    plain_caption = f"🚨 {promo_text} 🚨\n\n{original_caption}\n\n🔥 Акція: -{discount_percent}%"
    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=photo_id,
        caption=plain_caption,
        reply_markup=get_main_keyboard(),
    )


async def main():
  threading.Thread(target=run_dummy_web_server, daemon=True).start()

  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен с удобным кнопочным меню и веб-заглушкой...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
