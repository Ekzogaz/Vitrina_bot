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
  waiting_for_table_data = State()


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
          [KeyboardButton(text="✅ Готово, завантажити таблицю з цінами")]
      ],
      resize_keyboard=True,
  )
  return keyboard


# Нормализация кода: убираем ведущие нули и пробелы для точного сравнения
def normalize_code(code_str: str) -> str:
  if not code_str:
    return ""
  clean_digits = re.sub(r"\D", "", code_str)
  return clean_digits.lstrip("0")


# Функция замены цены (просто меняет старую цену на новую из таблицы, без скидок)
def update_price_straight(caption: str, new_price: float) -> str:
  if not caption:
    caption = ""

  # Форматируем новую цену (округляем до целых или оставляем как есть)
  formatted_new_price = f"{round(new_price):,}".replace(",", " ")
  
  # Паттерн для поиска старой цены (например, "Ціна 9 281 грн" или "🔥 Акційна ціна: 10 123 грн")
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

  return updated_text


# Извлечение штрихкода из текста поста
def extract_code_from_text(text: str):
  if not text:
    return None
  match = re.search(r"код\s*[:\-]?\s*(\d[\d\s]*\d)", text, re.IGNORECASE)
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
      "Я бот для оновлення цін за кодом.\n"
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
        "Перешліть або надішліть сюди пачку фотографій з описами виробів (з «Ізбранного»), "
        "де вказано рядок **Код ...**\n\n"
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


@dp.message(AdminStates.waiting_for_batch_photos, F.text == "✅ Готово, завантажити таблицю з цінами")
async def finish_photos_collection(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_batch_photos.get(user_id, [])

  if not photos:
    await message.answer("❌ Ви не надали жодного фото! Надішліть хоча б одне фото.")
    return

  await state.set_state(AdminStates.waiting_for_table_data)
  await message.answer(
      f"📥 Отримано фото: **{len(photos)} шт.**\n\n"
      "**Крок 2 з 2:**\n"
      "Тепер скопіюйте з таблиці (Колонку з Кодом та Колонку з Ціною) і **надішліть цей текст сюди**.",
      reply_markup=ReplyKeyboardRemove(),
  )


# Обработка скопированной таблицы (Код + Цена) без скидок
@dp.message(AdminStates.waiting_for_table_data, F.text)
async def process_table_text(message: Message, state: FSMContext):
  user_id = message.from_user.id
  table_text = message.text
  photos = user_batch_photos.get(user_id, [])

  price_database = {}  # { нормалізований_код: ціна }

  lines = table_text.split("\n")
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
    await message.answer("❌ Не вдалося розпізнати коди та ціни у надісланому тексті. Перевірте формат.")
    return

  await message.answer(f"🔍 Шукаю збіги за кодом для {len(photos)} виробів...")

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
      new_caption = caption  # Если код не найден в таблице, текст не меняем

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=item["photo"],
        caption=new_caption,
    )

  await state.clear()
  user_batch_photos[user_id] = []

  await message.answer(
      f"✅ Готово! Оброблено виробів: {len(photos)}\n"
      f"🎯 Успішно оновлено за кодом: {matched_count}\n\n"
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
  print("Бот запущен с прямым обновлением цен по коду...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
