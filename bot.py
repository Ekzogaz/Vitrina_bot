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
import openpyxl  # Библиотека для чтения Excel файлов

# Токен вашего бота
TOKEN = "9085949690:AAGAQLcj2ZBEG0FU1tqcHT6SZBm73w-gHtk"

bot = Bot(token=TOKEN)
dp = Dispatcher()

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
  waiting_for_table_file = State()


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
          [KeyboardButton(text="✅ Готово, завантажити файл з цінами")]
      ],
      resize_keyboard=True,
  )
  return keyboard


# Функция обновления цены в тексте поста
def update_price_in_caption(caption: str, new_price: float, discount_percent: float, promo_header: str) -> str:
  if not caption:
    caption = ""

  # Считаем акционную цену с учетом округления до целых
  auction_price = round(new_price * (1 - discount_percent / 100))
  
  pattern = r"(Ціна\s*:?\s*)(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.|грнн)?\b"
  price_found = False

  def replace_price(match):
    nonlocal price_found
    price_found = True
    original_full = match.group(0)
    currency = match.group(3) or "грн"
    return f"~~{original_full}~~" + f"\n\n🔥 Акційна ціна: {auction_price} {currency}"

  updated_text = re.sub(pattern, replace_price, caption, count=1, flags=re.IGNORECASE)

  if not price_found:
    updated_text += f"\n\n🔥 Акційна ціна: {auction_price} грн"

  # Если есть заголовок акции, добавляем его сверху, если его еще нет
  if promo_header and promo_header not in updated_text:
    return f"🚨 {promo_header} 🚨\n\n{updated_text}"
  
  return updated_text


# Функция поиска ИД (артикула) в тексте описания поста
def extract_id_from_text(text: str):
  if not text:
    return None
  # Ищем последовательность из 12 цифр (ИД из колонки А, например 000092120426)
  match = re.search(r"\b(\d{12})\b", text)
  return match.group(1) if match else None


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
      "Я бот для пакетного оновлення цін за унікальним ІД з таблиць 1С.\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "Оберіть потрібну дію на клавіатурі нижче:",
      reply_markup=get_main_keyboard(),
  )


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
        "📥 **Крок 1 з 2:**\n"
        "Перешліть або надішліть сюди пачку фотографій з описами виробів (наприклад, з «Ізбранного»). "
        "В описі кожного фото обов'язково має бути унікальний **ІД** (артикул з колонки А).\n\n"
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


@dp.message(AdminStates.waiting_for_batch_photos, F.text == "✅ Готово, завантажити файл з цінами")
async def finish_photos_collection(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_batch_photos.get(user_id, [])

  if not photos:
    await message.answer("❌ Ви не надали жодного фото! Надішліть хоча б одне фото.")
    return

  await state.set_state(AdminStates.waiting_for_table_file)
  await message.answer(
      f"📥 Отримано фото: **{len(photos)} шт.**\n\n"
      "**Крок 2 з 2:**\n"
      "Тепер надішліть сюди файл Excel (`.xlsx`) із таблицею цін або просто скопіюйте колонки з таблиці і надішліть текстом.",
      reply_markup=ReplyKeyboardRemove(),
  )


# Обработка файла `.xlsx` или текста с таблицей
@dp.message(AdminStates.waiting_for_table_file, F.document | F.text)
async def process_table_document_or_text(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_batch_photos.get(user_id, [])
  settings = user_settings.get(user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0})

  promo_text = settings["promo"]
  discount_percent = settings["discount"]

  price_database = {}  # { '000092120426': 2133.0 }

  # Если прислали файл (например .xlsx)
  if message.document:
    file_info = await bot.get_file(message.document.file_id)
    downloaded_file = await bot.download_file(file_info.file_path)
    
    try:
      wb = openpyxl.load_workbook(io.BytesIO(downloaded_file.read()), data_only=True)
      sheet = wb.active
      for row in sheet.iter_rows(values_only=True):
        # Ищем ИД в первой колонке (индекс 0) и цену в последней/нужной (ищем число)
        row_str_values = [str(cell) for cell in row if cell is not None]
        if not row_str_values:
          continue
        
        # Ищем ИД (12 цифр)
        item_id = None
        item_price = None
        for val in row_str_values:
          if re.fullmatch(r"\d{12}", val.strip()):
            item_id = val.strip()
            break
        
        # Ищем цену (число больше 10)
        for val in reversed(row_str_values):
          val_clean = val.replace(" ", "").replace(",", ".")
          try:
            num = float(val_clean)
            if num > 10:
              item_price = num
              break
          except ValueError:
            continue

        if item_id and item_price:
          price_database[item_id] = item_price
    except Exception as e:
      await message.answer(f"❌ Помилка читання файлу Excel: {e}")
      return

  elif message.text:
    # Если прислали текстом
    lines = message.text.split("\n")
    for line in lines:
      match_id = re.search(r"\b(\d{12})\b", line)
      if match_id:
        item_id = match_id.group(1)
        prices = re.findall(r"\b\d+[\.,]?\d*\b", line.replace(" ", ""))
        if prices:
          try:
            item_price = float(prices[-1].replace(",", "."))
            price_database[item_id] = item_price
          except ValueError:
            continue

  if not price_database:
    await message.answer("❌ Не вдалося знайти дані ІД та ціни у надісланому файлі/тексті. Спробуйте ще раз.")
    return

  await message.answer(f"🔍 Оброблено рядків з цінами: {len(price_database)}. Оновлюю товари...")

  matched_count = 0

  for item in photos:
    caption = item["caption"]
    item_id = extract_id_from_text(caption)

    found_price = None
    if item_id and item_id in price_database:
      found_price = price_database[item_id]
      matched_count += 1

    # Если нашли цену в таблице — обновляем только её, название и остальное не трогаем
    if found_price is not None:
      new_caption = update_price_in_caption(caption, found_price, discount_percent, promo_text)
    else:
      new_caption = caption  # Если ИД не найдено в таблице, оставляем текст без изменений

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=item["photo"],
        caption=new_caption,
    )

  await state.clear()
  user_batch_photos[user_id] = []

  await message.answer(
      f"✅ Готово! Оброблено виробів: {len(photos)}\n"
      f"🎯 Знайдено за ІД та оновлено цін: {matched_count}\n\n"
      "Усі готові посты вище можна переслати в групу!",
      reply_markup=get_main_keyboard(),
  )


@dp.message(AdminStates.waiting_for_custom_promo, F.text)
async def save_custom_promo(message: Message, state: FSMContext):
  user_id = message.from_user.id
  user_settings[user_id]["promo"] = message.text.strip()
  await state.clear()
  await message.answer("✅ Текст акції змінено!", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_custom_discount, F.text)
async def save_custom_discount(message: Message, state: FSMContext):
  user_id = message.from_user.id
  try:
    val = float(message.text.strip().replace(",", "."))
  except ValueError:
    await message.answer("❌ Введіть число:")
    return
  user_settings[user_id]["discount"] = val
  await state.clear()
  await message.answer(f"✅ Знижку змінено на: {val}%", reply_markup=get_main_keyboard())


async def main():
  threading.Thread(target=run_dummy_web_server, daemon=True).start()
  await bot.delete_webhook(drop_pending_updates=True)
  logging.basicConfig(level=logging.INFO)
  print("Бот запущен с поиском по уникальному ИД...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
