import asyncio
import http.server
import io
import json
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

user_settings = {}
DB_FILE = "group_posts_db.json"


def load_database():
  if os.path.exists(DB_FILE):
    try:
      with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
    except Exception:
      return {}
  return {}


def save_database(db):
  try:
    with open(DB_FILE, "w", encoding="utf-8") as f:
      json.dump(db, f, ensure_ascii=False, indent=2)
  except Exception as e:
    logging.error(f"Ошибка сохранения базы: {e}")


group_database = load_database()


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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
  httpd.serve_forever()


class AdminStates(StatesGroup):
  waiting_for_custom_promo = State()
  waiting_for_custom_discount = State()
  waiting_for_group_table = State()      # Ждем таблицу 1С для обычной смены цен
  waiting_for_promo_photos = State()     # Ждем пачку фото для акции (на лету)


def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="🔄 Отримати оновлені пости за таблицею"),
              KeyboardButton(text="🔥 Акція"),
          ],
          [
              KeyboardButton(text="✍️ Ввести свій текст акції"),
              KeyboardButton(text="🔢 Ввести свій % знижки"),
          ],
          [
              KeyboardButton(text="⚙️ Поточні налаштування"),
              KeyboardButton(text="📊 Статистика бази"),
              KeyboardButton(text="🗑 Очистити базу"),
          ],
      ],
      resize_keyboard=True,
  )
  return keyboard


def get_done_promo_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [KeyboardButton(text="✅ Готово, застосувати знижку")]
      ],
      resize_keyboard=True,
  )
  return keyboard


# Нормализация штрихкода
def normalize_code(code_str: str) -> str:
  if not code_str:
    return ""
  clean_digits = re.sub(r"\D", "", str(code_str))
  return clean_digits.lstrip("0")


# Точное извлечение штрихкода (именно по слову Код/Штрихкод)
def extract_code_from_text(text: str):
  if not text:
    return None
  match = re.search(r"(?:штрихкод|код)\s*[:\-]?\s*(\d[\d\s]*\d)", text, re.IGNORECASE)
  if match:
    return normalize_code(match.group(1))
  return None


# Полное удаление строки с кодом для чистоты поста
def remove_code_line(text: str) -> str:
  if not text:
    return ""
  cleaned = re.sub(r"^(код|штрихкод)\s*[:\-]?\s*\d+\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
  cleaned = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned).strip()
  return cleaned


# Обновление цены по таблице 1С + удаление кода
def update_price_and_clean_caption(caption: str, new_price: float) -> str:
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

  return remove_code_line(updated_text)


# Логика расчета скидки на лету для акций
def apply_promo_logic(text: str, promo_header: str, discount_percent: float) -> str:
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
      new_price = round(price * (1 - discount_percent / 100))
      currency = match.group(3) or "грн"
      original_full = match.group(0)
      return f"~~{original_full}~~" + f"\n\n🔥 Акційна ціна: {new_price} {currency}"
    except ValueError:
      return match.group(0)

  updated_text = re.sub(pattern, replace_price, text, count=1, flags=re.IGNORECASE)
  if not price_found:
    updated_text += f"\n\n🔥 Акційна ціна зі знижкою -{discount_percent}%"

  if promo_header and promo_header not in updated_text:
    updated_text = f"🚨 {promo_header} 🚨\n\n{updated_text}"

  return remove_code_line(updated_text)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 6.0}

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "• Пересилайте звичайні пости сюди для збереження в базу (для звичайної зміни цін за таблицею).\n"
      "• Натискайте «🔥 Акція», щоб переслати товари і на лету отримати розрахунок знижки!",
      reply_markup=get_main_keyboard(),
  )


# --- ОБРАБОТКА ФОТО (ФОНОВАЯ БАЗА ИЛИ ПЕРЕСЫЛКА ДЛЯ АКЦИЙ) ---
@dp.message(F.photo)
async def auto_collect_posts(message: Message, state: FSMContext):
  current_state = await state.get_state()
  user_id = message.from_user.id

  # РЕЖИМ АКЦИИ: собираем пересланные фото во временный пакет
  if current_state == AdminStates.waiting_for_promo_photos.state:
    if not hasattr(message.bot, "promo_batch"):
      message.bot.promo_batch = {}
    if user_id not in message.bot.promo_batch:
      message.bot.promo_batch[user_id] = []
    
    message.bot.promo_batch[user_id].append({
        "photo": message.photo[-1].file_id,
        "caption": message.caption or ""
    })
    count = len(message.bot.promo_batch[user_id])
    await message.answer(f"✅ Додано товар для акції №{count}. Надсилайте ще або натисніть «Готово».")
    return

  if current_state == AdminStates.waiting_for_group_table.state:
    return

  # ОБЫЧНЫЙ РЕЖИМ: сохраняем в постоянную базу для цен из 1С
  caption = message.caption or ""
  code = extract_code_from_text(caption)

  if code:
    chat_id = str(message.chat.id)
    if chat_id not in group_database:
      group_database[chat_id] = {}

    group_database[chat_id][code] = {
        "photo": message.photo[-1].file_id,
        "caption": caption
    }
    save_database(group_database)
    
    total_saved = sum(len(posts) for posts in group_database.values())
    await message.answer(f"✅ Товар із кодом `{code}` записано в базу! (Всього в базі: {total_saved} шт.)", parse_mode=ParseMode.MARKDOWN)
  else:
    await message.answer("ℹ️ У тексті цього фото не знайдено рядок «Код ...». Воно не додано до бази.", reply_markup=get_main_keyboard())


@dp.message(
    F.text.in_({
        "🔄 Отримати оновлені пости за таблицею",
        "🔥 Акція",
        "✍️ Ввести свій текст акції",
        "🔢 Ввести свій % знижки",
        "⚙️ Поточні налаштування",
        "📊 Статистика бази",
        "🗑 Очистити базу",
    })
)
async def handle_menu_buttons(message: Message, state: FSMContext):
  global group_database
  user_id = message.from_user.id
  text = message.text

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 6.0}

  if text == "🔄 Отримати оновлені пости за таблицею":
    total_saved = sum(len(posts) for posts in group_database.values())
    if total_saved == 0:
      await message.answer("⚠️ База даних порожня! Перешліть пости з групи сюди.", reply_markup=get_main_keyboard())
      return

    await state.set_state(AdminStates.waiting_for_group_table)
    await message.answer(
        f"📥 У базі даних збережено виробів: **{total_saved} шт.**\n\n"
        "Надішліть файл Excel (`.xlsx`) або скопіюйте текст із таблиці ціноутворення 1С.",
        reply_markup=ReplyKeyboardRemove(),
    )

  elif text == "🔥 Акція":
    if not hasattr(message.bot, "promo_batch"):
      message.bot.promo_batch = {}
    message.bot.promo_batch[user_id] = []

    await state.set_state(AdminStates.waiting_for_promo_photos)
    settings = user_settings[user_id]
    await message.answer(
        f"🔥 **Режим акції (поточна знижка: {settings['discount']}%)**\n\n"
        "Перешліть сюди товари (наприклад, золото або срібло), для яких робите акцію. "
        "Коли перешліть усі, натисніть кнопку нижче ⬇️",
        reply_markup=get_done_promo_keyboard(),
    )

  elif text == "📊 Статистика бази":
    total_saved = sum(len(posts) for posts in group_database.values())
    settings = user_settings[user_id]
    await message.answer(
        f"📊 Статистика:\n"
        f"• Загалом збережених товарів у базі: {total_saved} шт.\n"
        f"• Поточна акція: {settings['promo']}\n"
        f"• Поточна знижка: {settings['discount']}%",
        reply_markup=get_main_keyboard()
    )

  elif text == "🗑 Очистити базу":
    group_database = {}
    if os.path.exists(DB_FILE):
      os.remove(DB_FILE)
    await message.answer("🗑 База даних повністю очищена.", reply_markup=get_main_keyboard())

  elif text == "✍️ Ввести свій текст акції":
    await state.set_state(AdminStates.waiting_for_custom_promo)
    await message.answer("✍️ Надішліть текстом новий заголовок для вашої акції:", reply_markup=ReplyKeyboardRemove())

  elif text == "🔢 Ввести свій % знижки":
    await state.set_state(AdminStates.waiting_for_custom_discount)
    await message.answer("🔢 Введіть числом бажаний відсоток знижки (наприклад: 6 або 12):", reply_markup=ReplyKeyboardRemove())

  elif text == "⚙️ Поточні налаштування":
    settings = user_settings[user_id]
    await message.answer(
        f"📋 Поточні налаштування:\n"
        f"• Акція: {settings['promo']}\n"
        f"• Знижка: {settings['discount']}%",
        reply_markup=get_main_keyboard(),
    )


# --- ЗАВЕРШЕНИЕ И ВЫДАЧА АКЦИИ НА ЛЕТУ ---
@dp.message(AdminStates.waiting_for_promo_photos, F.text == "✅ Готово, застосувати знижку")
async def finish_promo_batch(message: Message, state: FSMContext):
  user_id = message.from_user.id
  batch = getattr(message.bot, "promo_batch", {}).get(user_id, [])

  if not batch:
    await message.answer("❌ Ви не надіслали жодного товару для акції!", reply_markup=get_main_keyboard())
    await state.clear()
    return

  settings = user_settings.get(user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 6.0})
  await message.answer(f"🔄 Формую акційні пости (знижка -{settings['discount']}%)...")

  for item in batch:
    new_caption = apply_promo_logic(item["caption"], settings["promo"], settings["discount"])
    try:
      await bot.send_photo(
          chat_id=user_id,
          photo=item["photo"],
          caption=new_caption
      )
      await asyncio.sleep(0.3)
    except Exception as e:
      logging.error(f"Ошибка отправки акционного фото: {e}")

  message.bot.promo_batch[user_id] = []
  await state.clear()

  await message.answer(
      f"✅ Готово! Надіслано акційних постів: {len(batch)}\n"
      "Усі ціни зі знижкою, штрихкоди приховані — можете пересилати у групу!",
      reply_markup=get_main_keyboard(),
  )


# --- ОБРАБОТКА ТАБЛИЦЫ ДЛЯ ОБНОВЛЕНИЯ ЦЕН ИЗ БАЗЫ ---
@dp.message(AdminStates.waiting_for_group_table, F.document | F.text)
async def process_table_and_send_posts(message: Message, state: FSMContext):
  user_id = message.from_user.id
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
    await message.answer("❌ Не вдалося розпізнати штрихкоди та ціни.")
    await state.clear()
    return

  await message.answer("🔄 Формую чисті пости з новими цінами з бази...")

  sent_count = 0
  for chat_str_id, posts in group_database.items():
    for code, post_info in posts.items():
      if code in price_database:
        new_price = price_database[code]
        cleaned_caption = update_price_and_clean_caption(post_info["caption"], new_price)

        try:
          await bot.send_photo(
              chat_id=user_id,
              photo=post_info["photo"],
              caption=cleaned_caption
          )
          sent_count += 1
          await asyncio.sleep(0.3)
        except Exception as e:
          logging.error(f"Ошибка отправки поста: {e}")

  await state.clear()
  await message.answer(
      f"✅ Готово! Надіслано оновлених постів: {sent_count}\n"
      "Усі ціни оновлено за таблицею, штрихкоди приховані.",
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
  print("Бот запущен успешно...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
