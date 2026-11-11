import asyncio
import http.server
import io
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
import openpyxl

# Токен вашего бота
TOKEN = "9085949690:AAGAQLcj2ZBEG0FU1tqcHT6SZBm73w-gHtk"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Словарь для хранения настроек пользователей
user_settings = {}
user_batch_photos = {}  # {user_id: [list of dicts with photo_id and caption]}


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (GET + HEAD) ---
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
  print(f"Веб-сервер запущен на порту {port}")
  httpd.serve_forever()


class AdminStates(StatesGroup):
  waiting_for_custom_promo = State()
  waiting_for_custom_discount = State()
  waiting_for_batch_photos = State()
  waiting_for_table_input = State()


def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="🔄 Обновити ціни за таблицею"),
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


def get_done_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [KeyboardButton(text="✅ Готово, надіслати таблицю / текст")]
      ],
      resize_keyboard=True,
  )
  return keyboard


# Нормализация кода для табличного обновления
def normalize_code(code_str: str) -> str:
  if not code_str:
    return ""
  clean_digits = re.sub(r"\D", "", str(code_str))
  return clean_digits.lstrip("0")


# Логика для обычных акций (считает скидку, зачеркивает цену, добавляет заголовок акции)
def apply_auction_logic(text: str, promo_header: str, discount_percent: float) -> str:
  if not text:
    text = ""

  pattern = r"(Ціна\s*:?\s*)(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.|грнн)?\b"
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

  # Добавляем заголовок акции сверху, если его еще нет
  if promo_header and promo_header not in updated_text:
    return f"🚨 {promo_header} 🚨\n\n{updated_text}"
  
  return updated_text


# Прямая замена цены для табличного режима (без скидок, с удалением строки кода)
def update_price_straight(caption: str, new_price: float) -> str:
  if not caption:
    caption = ""

  formatted_new_price = f"{round(new_price):,}".replace(",", " ")
  
  pattern = r"(Ціна\s*:?\s*)(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.|грнн)?\b"
  price_found = False

  def replace_price(match):
    nonlocal price_found
    price_found = True
    prefix = match.group(1)
    currency = match.group(3) or "грн"
    return f"{prefix}{formatted_new_price} {currency}"

  updated_text = re.sub(pattern, replace_price, caption, count=1, flags=re.IGNORECASE)

  if not price_found:
    updated_text += f"\n\nЦіна {formatted_new_price} грн"

  # Удаляем строчку с кодом/штрихкодом для клиентов
  updated_text = re.sub(r"^(код|штрихкод)\s*[:\-]?\s*\d+\s*$", "", updated_text, flags=re.IGNORECASE | re.MULTILINE)
  updated_text = re.sub(r"\n\s*\n\s*\n", "\n\n", updated_text).strip()

  return updated_text


# Извлечение кода из текста поста
def extract_code_from_text(text: str):
  if not text:
    return None
  match = re.search(r"(?:штрихкод|код)\s*[:\-]?\s*(\d[\d\s]*\d)", text, re.IGNORECASE)
  if match:
    return normalize_code(match.group(1))
  
  match_num = re.search(r"\b(\d{8,15})\b", text)
  if match_num:
    return normalize_code(match_num.group(1))
    
  return None


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
      "Я бот для управління акціями та оновлення цін.\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "Оберіть потрібну дію на клавіатурі нижче:",
      reply_markup=get_main_keyboard(),
  )


# Обработка кнопок главного меню
@dp.message(
    F.text.in_({
        "🔄 Обновити ціни за таблицею",
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

  if text == "🔄 Обновити ціни за таблицею":
    user_batch_photos[user_id] = []
    await state.set_state(AdminStates.waiting_for_batch_photos)
    await message.answer(
        "📥 **Режим оновлення за таблицею (Крок 1):**\n"
        "Перешліть або надішліть сюди пачку фотографій з описами виробів (з «Ізбранного»). "
        "У кожному описі має бути рядок **Код ...**\n\n"
        "Коли надішлете всі фото, натисніть кнопку нижче ⬇️",
        reply_markup=get_done_keyboard(),
    )

  elif text == "✍️ Ввести свій текст акції":
    await state.set_state(AdminStates.waiting_for_custom_promo)
    await message.answer("✍️ Надішліть текстом новий заголовок для вашої акції:", reply_markup=ReplyKeyboardRemove())

  elif text == "🔢 Ввести свій % знижки":
    await state.set_state(AdminStates.waiting_for_custom_discount)
    await message.answer("🔢 Введіть числом бажаний відсоток знижки (наприклад: 5 або 10):", reply_markup=ReplyKeyboardRemove())

  elif text == "⚙️ Поточні налаштування":
    settings = user_settings[user_id]
    await message.answer(
        f"📋 Поточні налаштування:\n"
        f"• Акція: {settings['promo']}\n"
        f"• Знижка: {settings['discount']}%",
        reply_markup=get_main_keyboard(),
    )


# Шаг 1 табличного режима: сбор фото
@dp.message(AdminStates.waiting_for_batch_photos, F.photo)
async def collect_photos(message: Message):
  user_id = message.from_user.id
  photo_id = message.photo[-1].file_id
  caption = message.caption or ""

  if user_id not in user_batch_photos:
    user_batch_photos[user_id] = []

  user_batch_photos[user_id].append({"photo": photo_id, "caption": caption})
  count = len(user_batch_photos[user_id])
  await message.answer(f"✅ Збережено фото №{count}. Продовжуйте надсилати або натисніть «Готово».")


# Кнопка завершения сбора фото для табличного режима
@dp.message(AdminStates.waiting_for_batch_photos, F.text == "✅ Готово, надіслати таблицю / текст")
async def finish_photos_collection(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_batch_photos.get(user_id, [])

  if not photos:
    await message.answer("❌ Ви не надали жодного фото! Надішліть хоча б одне фото.")
    return

  await state.set_state(AdminStates.waiting_for_table_input)
  await message.answer(
      f"📥 Отримано фото: **{len(photos)} шт.**\n\n"
      "**Режим оновлення за таблицею (Крок 2):**\n"
      "Тепер **надішліть сюди файл Excel (`.xlsx`)** АБО **скопіюйте і вставте текст із таблиці** (код та ціна).",
      reply_markup=ReplyKeyboardRemove(),
  )


# Обработка таблицы/текста в табличном режиме
@dp.message(AdminStates.waiting_for_table_input, F.document | F.text)
async def process_table_input(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_batch_photos.get(user_id, [])

  price_database = {}

  if message.document:
    file_info = await bot.get_file(message.document.file_id)
    downloaded_file = await bot.download_file(file_info.file_path)

    try:
      wb = openpyxl.load_workbook(io.BytesIO(downloaded_file.read()), data_only=True)
      sheet = wb.active
      
      for row in sheet.iter_rows(values_only=True):
        row_values = [cell for cell in row if cell is not None]
        if not row_values:
          continue
        
        raw_code = str(row_values[0]).strip()
        norm_code = normalize_code(raw_code)

        item_price = None
        for val in reversed(row_values):
          try:
            clean_val = float(str(val).replace(" ", "").replace(",", "."))
            if clean_val > 10:
              item_price = clean_val
              break
          except ValueError:
            continue

        if norm_code and item_price and len(norm_code) >= 5:
          price_database[norm_code] = item_price
    except Exception as e:
      await message.answer(f"❌ Помилка читання файлу: {e}")
      return

  elif message.text:
    lines = message.text.split("\n")
    for line in lines:
      numbers = re.findall(r"\b\d+[\.,]?\d*\b", line.replace(" ", ""))
      if len(numbers) >= 2:
        raw_code = numbers[0]
        raw_price = numbers[-1].replace(",", ".")
        
        norm_code = normalize_code(raw_code)
        try:
          price_val = float(raw_price)
          if price_val > 10 and len(norm_code) >= 5:
            price_database[norm_code] = price_val
        except ValueError:
          continue

  if not price_database:
    await message.answer("❌ Не вдалося розпізнати штрихкоди та ціни. Перевірте дані.")
    return

  await message.answer(f"🔍 Оброблено позицій: {len(price_database)}. Шукаю збіги...")

  matched_count = 0

  for item in photos:
    caption = item["caption"]
    item_code = extract_code_from_text(caption)

    found_price = None
    if item_code and item_code in price_database:
      found_price = price_database[item_code]
      matched_count += 1

    if found_price is not None:
      new_caption = update_price_straight(caption, found_price)
    else:
      new_caption = caption

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=item["photo"],
        caption=new_caption,
    )

  await state.clear()
  user_batch_photos[user_id] = []

  await message.answer(
      f"✅ Готово! Оброблено виробів: {len(photos)}\n"
      f"🎯 Успішно оновлено: {matched_count}\n\n"
      "Можете пересилати порти в групу!",
      reply_markup=get_main_keyboard(),
  )


# --- ОБЫЧНЫЙ РЕЖИМ АКЦИЙ (КОГДА КИДАЮТ ФОТО ПРОСТО ТАК С НАСТРОЕННОЙ СКИДКОЙ) ---
@dp.message(F.photo)
async def handle_regular_photos(message: Message, state: FSMContext):
  # Если пользователь случайно попал сюда в состоянии ожидания таблиц — игнорируем
  current_state = await state.get_state()
  if current_state in [AdminStates.waiting_for_batch_photos.state, AdminStates.waiting_for_table_input.state]:
    return

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


# Сохранение текста акции
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
      f"✅ Текст акції змінено на:\n{new_promo}\n\n"
      "Тепер можете просто кидати фотографії з описами, і бот застосує цю акцію зі знижкою!",
      reply_markup=get_main_keyboard(),
  )


# Сохранение процента скидки
@dp.message(AdminStates.waiting_for_custom_discount, F.text)
async def save_custom_discount(message: Message, state: FSMContext):
  user_id = message.from_user.id
  try:
    new_discount = float(message.text.strip().replace(",", "."))
  except ValueError:
    await message.answer("❌ Введіть лише число (наприклад: 5 або 10.5):")
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
      f"✅ Розмір знижки змінено на: {new_discount}%\n\n"
      "Тепер можете кидати фотографії, і бот розрахує нову ціну!",
      reply_markup=get_main_keyboard(),
  )


async def main():
  threading.Thread(target=run_dummy_web_server, daemon=True).start()
  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен с поддержкой и акций со скидкой, и табличного обновления!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
