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

# Файл для постоянного сохранения базы постов группы
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


# База данных: {str(chat_id): {norm_code: {"photo": photo_id, "caption": text}}}
group_database = load_database()


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
  waiting_for_group_table = State()  # Ожидание таблицы для выдачи чистых постов


def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="🔄 Отримати оновлені пости без кодів"),
          ],
          [
              KeyboardButton(text="✍️ Ввести свій текст акції"),
              KeyboardButton(text="🔢 Ввести свій % знижки"),
          ],
          [
              KeyboardButton(text="⚙️ Поточні налаштування"),
              KeyboardButton(text="📊 Статистика бази"),
          ],
      ],
      resize_keyboard=True,
  )
  return keyboard


# Нормализация штрихкода (убираем ведущие нули и пробелы)
def normalize_code(code_str: str) -> str:
  if not code_str:
    return ""
  clean_digits = re.sub(r"\D", "", str(code_str))
  return clean_digits.lstrip("0")


# Извлечение штрихкода из текста поста
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


# Обновление цены и ПОЛНОЕ УДАЛЕНИЕ строки с кодом для чистоты итогового поста
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

  # Удаляем строчку с кодом/штрихкодом для клиентов
  updated_text = re.sub(r"^(код|штрихкод)\s*[:\-]?\s*\d+\s*$", "", updated_text, flags=re.IGNORECASE | re.MULTILINE)
  updated_text = re.sub(r"\n\s*\n\s*\n", "\n\n", updated_text).strip()

  return updated_text


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0}

  settings = user_settings[user_id]
  await message.answer(
      "Привіт! 👋\n\n"
      "Я бот для оновлення цін.\n"
      "Просто пересилайте мені пости з групи (один раз), щоб я їх запам'ятав в базу.\n"
      "Потім я буду видавати вам готові пости з новими цінами та без кодів прямо сюди в чат!\n\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "Оберіть дію нижче:",
      reply_markup=get_main_keyboard(),
  )


# --- НАКОПЛЕНИЕ БАЗЫ «НА ЛЕТУ» ПРИ ПЕРЕСЫЛКЕ ---
@dp.message(F.photo)
async def auto_collect_posts(message: Message, state: FSMContext):
  current_state = await state.get_state()
  if current_state == AdminStates.waiting_for_group_table.state:
    return

  caption = message.caption or ""
  code = extract_code_from_text(caption)

  if code:
    chat_id = str(message.chat.id)
    if chat_id not in group_database:
      group_database[chat_id] = {}

    # Сохраняем фото и текст для последующей выдачи в ЛС
    group_database[chat_id][code] = {
        "photo": message.photo[-1].file_id,
        "caption": caption
    }
    save_database(group_database)
    
    total_saved = sum(len(posts) for posts in group_database.values())
    await message.answer(f"✅ Товар із кодом `{code}` записано в базу! (Всього в базі: {total_saved} шт.)", parse_mode=ParseMode.MARKDOWN)
  else:
    # Обработка обычных фото для скидки в ЛС
    user_id = message.from_user.id
    settings = user_settings.get(user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0})
    if message.chat.type == "private":
      from_text = message.caption or ""
      pattern = r"(Ціна\s*:?\s*)(\d[\d\s]*[\.,]?\d*)\s*(грн|uah|₴|грн\.|грнн)?\b"
      def rep(m):
        try:
          p = float(m.group(2).replace(" ", "").replace(",", "."))
          np = round(p * (1 - settings["discount"] / 100))
          return f"~~{m.group(0)}~~\n\n🔥 Акційна ціна: {np} {m.group(3) or 'грн'}"
        except:
          return m.group(0)
      new_c = re.sub(pattern, rep, from_text, count=1, flags=re.IGNORECASE)
      await bot.send_photo(chat_id=user_id, photo=message.photo[-1].file_id, caption=new_c, reply_markup=get_main_keyboard())


@dp.message(
    F.text.in_({
        "🔄 Отримати оновлені пости без кодів",
        "✍️ Ввести свій текст акції",
        "🔢 Ввести свій % знижки",
        "⚙️ Поточні налаштування",
        "📊 Статистика бази",
    })
)
async def handle_menu_buttons(message: Message, state: FSMContext):
  user_id = message.from_user.id
  text = message.text

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0}

  if text == "🔄 Отримати оновлені пости без кодів":
    total_saved = sum(len(posts) for posts in group_database.values())
    if total_saved == 0:
      await message.answer(
          "⚠️ База даних поки що порожня!\n\n"
          "Просто перешліть сюди в чат кілька постів із вашої групи, щоб бот заповнив базу.",
          reply_markup=get_main_keyboard(),
      )
      return

    await state.set_state(AdminStates.waiting_for_group_table)
    await message.answer(
        f"📥 У базі даних збережено виробів: **{total_saved} шт.**\n\n"
        "Тепер **надішліть сюди файл Excel (`.xlsx`)** АБО **скопіюйте і вставте текст із таблиці** (штрихкод та ціна). "
        "Бот підготує для вас чисті пости без кодів прямо в цьому чаті!",
        reply_markup=ReplyKeyboardRemove(),
    )

  elif text == "📊 Статистика бази":
    total_saved = sum(len(posts) for posts in group_database.values())
    await message.answer(f"📊 Статистика:\n• Загалом збережених товарів у базі: {total_saved} шт.", reply_markup=get_main_keyboard())

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


# --- ОБРАБОТКА ТАБЛИЦЫ И ВЫДАЧА ЧИСТЫХ ПОСТОВ В ЛИЧНЫЕ СООБЩЕНИЯ ---
@dp.message(AdminStates.waiting_for_group_table, F.document | F.text)
async def process_table_and_send_posts(message: Message, state: FSMContext):
  price_database = {}

  # 1. Читаем Excel
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

  # 2. Читаем текст
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
    await message.answer("❌ Не вдалося розпізнати штрихкоди та ціни у надісланих даних.")
    await state.clear()
    return

  await message.answer("🔄 Формую чисті пости без кодів...")

  sent_count = 0

  # Ищем товары по базе и отправляем результат в ЛС пользователю
  for chat_str_id, posts in group_database.items():
    for code, post_info in posts.items():
      if code in price_database:
        new_price = price_database[code]
        cleaned_caption = update_price_and_clean_caption(post_info["caption"], new_price)

        try:
          await bot.send_photo(
              chat_id=message.from_user.id,
              photo=post_info["photo"],
              caption=cleaned_caption
          )
          sent_count += 1
          await asyncio.sleep(0.3)  # пауза между отправками
        except Exception as e:
          logging.error(f"Не удалось отправить фото в ЛС: {e}")

  await state.clear()
  await message.answer(
      f"✅ Готово! Надіслано чистих постів у чат: {sent_count}\n\n"
      "Усі пости сформовані **без рядків з кодом** — можете просто пересилати їх у вашу групу!",
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
  print("Бот запущен в режиме выдачи чистых постов в ЛС на основе базы...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
