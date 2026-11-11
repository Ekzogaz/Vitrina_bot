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
from aiogram.fsm.state import State, StatesGroup
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


# --- УПРОЩЕННЫЙ ВЕБ-СЕРВЕР С ПОДДЕРЖКОЙ GET И HEAD ДЛЯ UPTIMEROBOT ---
def run_dummy_web_server():
  port = int(os.environ.get("PORT", 10000))

  class FastHealthHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
      self.send_response(200)
      self.send_header("Content-type", "text/plain; charset=utf-8")
      self.end_headers()
      self.wfile.write(b"OK")

    def do_HEAD(self):
      self.send_response(200)
      self.send_header("Content-type", "text/plain; charset=utf-8")
      self.end_headers()

    def log_message(self, format, *args):
      pass

  server_address = ("0.0.0.0", port)
  httpd = http.server.HTTPServer(server_address, FastHealthHandler)
  print(f"Быстрый веб-сервер запущен на порту {port}")
  httpd.serve_forever()


# Состояния для ввода через интерактивный диалог по кнопкам
class AdminStates(StatesGroup):
  waiting_for_custom_promo = State()
  waiting_for_custom_discount = State()


# Упрощенная клавиатура
def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
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


# Функция формирования текста: округление до целых и без процента в скобках
def apply_auction_logic(
    text: str, promo_header: str, discount_percent: float
) -> str:
  if not text:
    text = ""

  # Паттерн для поиска цены (например, "Ціна 8 572 грн")
  pattern = (
      r"(Ціна\s*:?\s*)(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.|грнн)?\b"
  )
  price_found = False

  def replace_price(match):
    nonlocal price_found
    raw_number = match.group(2).replace(" ", "").replace(",", ".")
    try:
      price = float(raw_number)
      if price <= 0:
        return match.group(0)

      price_found = True
      new_price = price * (1 - discount_percent / 100)
      new_price_formatted = round(new_price)

      currency = match.group(3) or "грн"
      original_full = match.group(0)

      return (
          f"~~{original_full}~~"
          f"\n\n🔥 Акційна ціна: {new_price_formatted} {currency}"
      )
    except ValueError:
      return match.group(0)

  updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)

  if not price_found:
    updated_text += f"\n\n🔥 Акційна ціна зі знижкою -{discount_percent}%"

  return f"🚨 {promo_header} 🚨\n\n{updated_text}"


# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "АКЦІЙНА ПРОПОЗИЦІЯ",
        "discount": 5.0,
    }

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      "Я бот для пакетної обробки фото товарів зі знижками.\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "Оберіть дію на клавіатурі нижче або просто кидайте пачку фото з"
      " товарами!",
      reply_markup=get_main_keyboard(),
  )


# Обработка нажатий на кнопки меню
@dp.message(
    F.text.in_({
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
        "promo": "АКЦІЙНА ПРОПОЗИЦІЯ",
        "discount": 5.0,
    }

  if text == "✍️ Ввести свій текст акції":
    await state.set_state(AdminStates.waiting_for_custom_promo)
    await message.answer(
        "✍️ Надішліть текстом новий заголовок для вашої акції:",
        reply_markup=ReplyKeyboardRemove(),
    )

  elif text == "🔢 Ввести свій % знижки":
    await state.set_state(AdminStates.waiting_for_custom_discount)
    await message.answer(
        "🔢 Введіть числом бажаний відсоток знижки (наприклад: 7.5 або 12):",
        reply_markup=ReplyKeyboardRemove(),
    )

  elif text == "⚙️ Поточні налаштування":
    settings = user_settings[user_id]
    await message.answer(
        f"📋 Поточні налаштування:\n"
        f"• Акція: {settings['promo']}\n"
        f"• Знижка: {settings['discount']}%",
        reply_markup=get_main_keyboard(),
    )


# Шаг: сохранение своего текста акции
@dp.message(AdminStates.waiting_for_custom_promo, F.text)
async def save_custom_promo(message: Message, state: FSMContext):
  user_id = message.from_user.id
  new_promo = message.text.strip()

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": new_promo, "discount": 5.0}
  else:
    user_settings[user_id]["promo"] = new_promo

  await state.clear()
  await message.answer(
      f"✅ Текст акції змінено на:\n{new_promo}",
      reply_markup=get_main_keyboard(),
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
        "❌ Помилка! Введіть лише число (наприклад: 7 або 12.5):"
    )
    return

  if user_id not in user_settings:
    user_settings[user_id] = {
        "promo": "АКЦІЙНА ПРОПОЗИЦІЯ",
        "discount": new_discount,
    }
  else:
    user_settings[user_id]["discount"] = new_discount

  await state.clear()
  await message.answer(
      f"✅ Розмір знижки змінено на: {new_discount}%",
      reply_markup=get_main_keyboard(),
  )


# Обработчик фотографий
@dp.message(F.photo)
async def handle_photos(message: Message):
  user_id = message.from_user.id
  settings = user_settings.get(
      user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0}
  )

  promo_text = settings["promo"]
  discount_percent = settings["discount"]

  original_caption = message.caption or ""
  new_caption = apply_auction_logic(
      original_caption, promo_text, discount_percent
  )
  photo_id = message.photo[-1].file_id

  await bot.send_photo(
      chat_id=message.from_user.id,
      photo=photo_id,
      caption=new_caption,
      reply_markup=get_main_keyboard(),
  )


async def main():
  threading.Thread(target=run_dummy_web_server, daemon=True).start()

  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен с поддержкой GET и HEAD...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
