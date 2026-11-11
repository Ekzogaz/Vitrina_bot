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

user_settings = {}
user_group_scan = {}  # {user_id: [list of dicts with photo_id and caption]}


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
  waiting_for_group_link = State()     
  waiting_for_group_table = State()    


def get_main_keyboard():
  keyboard = ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="🔄 Обновити ціни за таблицею"),
              KeyboardButton(text="🔍 Сканувати групу"),
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


# Логика для обычных акций со скидкой
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
    return f"🚨 {promo_header} 🚨\n\n{updated_text}"
  return updated_text


# Прямая замена цены по таблице и удаление строки с кодом для чистоты поста
def update_price_straight_and_clean(caption: str, new_price: float) -> str:
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

  # Удаляем строчку с кодом/штрихкодом, чтобы клиенты её не видели
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
      "Я бот для оновлення цін за штрихкодом.\n"
      f"📋 Поточні налаштування:\n"
      f"• Акція: {settings['promo']}\n"
      f"• Знижка: {settings['discount']}%\n\n"
      "Оберіть потрібну дію на клавіатурі нижче:",
      reply_markup=get_main_keyboard(),
  )


@dp.message(
    F.text.in_({
        "🔄 Обновити ціни за таблицею",
        "🔍 Сканувати групу",
        "✍️ Ввести свій текст акції",
        "🔢 Ввести свій % знижки",
        "⚙️ Поточні налаштування",
    })
)
async def handle_menu_buttons(message: Message, state: FSMContext):
  user_id = message.from_user.id
  text = message.text

  if user_id not in user_settings:
    user_settings[user_id] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0}

  if text == "🔄 Обновити ціни за таблицею":
    await message.answer(
        "📥 Ви можете надіслати файл таблиці або скористатись кнопкою **«🔍 Сканувати групу»**.",
        reply_markup=get_main_keyboard(),
    )

  elif text == "🔍 Сканувати групу":
    await state.set_state(AdminStates.waiting_for_group_link)
    await message.answer(
        "🔍 **Сканування групи:**\n\n"
        "Будь ласка, **перешліть сюди будь-яке повідомлення з вашої групи з виробами** "
        "(тепер бот прийме будь-яке переслане повідомлення та спробує витягнути історію).",
        reply_markup=ReplyKeyboardRemove(),
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


# Упрощенный прием пересланного сообщения (без жесткой проверки источника)
@dp.message(AdminStates.waiting_for_group_link)
async def process_group_forward(message: Message, state: FSMContext):
  user_id = message.from_user.id
  
  # Проверяем, есть ли у сообщения вообще признак пересылки
  if not message.forward_date and not message.forward_from_chat:
    await message.answer("❌ Будь ласка, саме **перешліть** повідомлення (а не скопіюйте текст).")
    return

  chat_id = message.forward_from_chat.id if message.forward_from_chat else message.chat.id
  forward_msg_id = message.forward_from_message_id or message.message_id

  await message.answer("⏳ Зчитую пости з групи...")

  scanned_items = []
  success_count = 0
  
  # Сканируем диапазон сообщений вокруг пересланного
  for msg_id in range(max(1, forward_msg_id - 100), forward_msg_id + 20):
    try:
      msg = await bot.get_message(chat_id=chat_id, message_id=msg_id)
      if msg and msg.photo and msg.caption:
        code = extract_code_from_text(msg.caption)
        if code:
          scanned_items.append({"photo": msg.photo[-1].file_id, "caption": msg.caption})
          success_count += 1
      await asyncio.sleep(0.03)
    except Exception:
      continue

  if not scanned_items:
    await message.answer(
        "⚠️ Не вдалося прочитати історію автоматично. "
        "Але ви можете просто переслати потрібні пости пачкою сюди в чат, і бот миттєво їх підхопить!",
        reply_markup=get_main_keyboard()
    )
    await state.clear()
    return

  user_group_scan[user_id] = scanned_items

  await state.set_state(AdminStates.waiting_for_group_table)
  await message.answer(
      f"✅ Успішно знайдено виробів з кодами: **{success_count} шт.**\n\n"
      "**Крок 2 з 2:**\n"
      "Тепер просто **надішліть сюди файл Excel (`.xlsx`)** АБО **скопіюйте і вставте текст із таблиці** (штрихкод та ціна).",
      reply_markup=ReplyKeyboardRemove(),
  )


# Обработка таблицы или текста для отсканированных товаров
@dp.message(AdminStates.waiting_for_group_table, F.document | F.text)
async def process_scanned_table_input(message: Message, state: FSMContext):
  user_id = message.from_user.id
  photos = user_group_scan.get(user_id, [])

  if not photos:
    await message.answer("Спочатку натисніть «🔍 Сканувати групу».", reply_markup=get_main_keyboard())
    await state.clear()
    return

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

  await message.answer(f"🔍 Оброблено позицій з таблиці: {len(price_database)}. Формую оновлені пости...")

  matched_count = 0

  for item in photos:
    caption = item["caption"]
    item_code = extract_code_from_text(caption)

    found_price = None
    if item_code and item_code in price_database:
      found_price = price_database[item_code]
      matched_count += 1

    if found_price is not None:
      new_caption = update_price_straight_and_clean(caption, found_price)
    else:
      new_caption = caption

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=item["photo"],
        caption=new_caption,
    )

  await state.clear()
  user_group_scan[user_id] = []

  await message.answer(
      f"✅ Готово! Оброблено виробів: {len(photos)}\n"
      f"🎯 Успішно оновлено за штрихкодом: {matched_count}\n\n"
      "Усі готові пости вище сформовані **без службового коду** — можете сміливо пересилати їх у вашу групу!",
      reply_markup=get_main_keyboard(),
  )


# Обработка обычных фото со скидкой
@dp.message(F.photo)
async def handle_regular_photos(message: Message, state: FSMContext):
  current_state = await state.get_state()
  if current_state in [AdminStates.waiting_for_group_link.state, AdminStates.waiting_for_group_table.state]:
    return

  user_id = message.from_user.id
  settings = user_settings.get(user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0})
  new_caption = apply_auction_logic(message.caption or "", settings["promo"], settings["discount"])

  await bot.send_photo(
      chat_id=message.from_user.id,
      photo=message.photo[-1].file_id,
      caption=new_caption,
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
  print("Бот запущен с обновленным сканированием без жесткой проверки источника...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
