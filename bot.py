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
          [KeyboardButton(text="✅ Готово, завантажити таблицю")]
      ],
      resize_keyboard=True,
  )
  return keyboard


# Функция обновления цены в тексте поста (меняет только цену, остальное не трогает)
def update_price_in_caption(caption: str, new_price: float, discount_percent: float, promo_header: str) -> str:
  if not caption:
    caption = ""

  # Считаем акционную цену с округлением до целых
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

  if promo_header and promo_header not in updated_text:
    return f"🚨 {promo_header} 🚨\n\n{updated_text}"
  
  return updated_text


# Функция извлечения характеристик (проба, вес, размер) из текста
def extract_specs(text: str):
  if not text:
    return None, None, 0.0
  
  text_lower = text.lower()
  
  # Ищем пробу (3 цифры, например 585)
  purity_match = re.search(r"\b(\d{3})\b", text_lower)
  purity = purity_match.group(1) if purity_match else None

  # Ищем вес (число с точкой или запятой перед 'г' или в тексте)
  weight = None
  weight_match = re.search(r"(\d+[\.,]\d+|\d+)\s*(?:г\b|грам)", text_lower)
  if weight_match:
    try:
      weight = float(weight_match.group(1).replace(",", "."))
    except ValueError:
      pass

  # Ищем размер (если есть, например 17 или 17.5)
  size = 0.0
  size_match = re.search(r"(?:розмір\D*)?(\d+[\.,]?\d*)\s*(?:см|мм)?\b", text_lower)
  if size_match and not weight_match: # Убеждаемся что это не вес
    try:
      size = float(size_match.group(1).replace(",", "."))
    except ValueError:
      pass

  return purity, weight, size


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
      "Я бот для оновлення цін за характеристиками (проба, вага, розмір).\n"
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
        "Перешліть або надішліть сюди пачку фотографій з описами виробів (з «Ізбранного»).\n\n"
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


@dp.message(AdminStates.waiting_for_batch_photos, F.text == "✅ Готово, завантажити таблицю")
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
      "Тепер просто **скопіюйте дані з таблиці в LibreOffice/Excel** (де є Проба, Розмір, Вага, Ціна) та **надішліть цей текст сюди**.",
      reply_markup=ReplyKeyboardRemove(),
  )


# Обработка скопированного текста из таблицы 1С
@dp.message(AdminStates.waiting_for_table_data, F.text)
async def process_table_text(message: Message, state: FSMContext):
  user_id = message.from_user.id
  table_text = message.text
  photos = user_batch_photos.get(user_id, [])
  settings = user_settings.get(user_id, {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 5.0})

  promo_text = settings["promo"]
  discount_percent = settings["discount"]

  # Парсим строки из вставленного текста
  table_rows = []
  lines = table_text.split("\n")
  for line in lines:
    purity, weight, size = extract_specs(line)
    
    # Ищем цену (число больше 10 в строке)
    prices = re.findall(r"\b\d+[\.,]?\d*\b", line.replace(" ", ""))
    price = None
    if prices:
      for p_str in reversed(prices):
        try:
          val = float(p_str.replace(",", "."))
          if val > 10:  # Цены изделий больше 10 грн
            price = val
            break
        except ValueError:
          continue

    if purity and weight and price:
      table_rows.append({"purity": purity, "weight": weight, "size": size, "price": price})

  if not table_rows:
    await message.answer("❌ Не вдалося розпізнати дані у надісланому тексті. Переконайтеся, що скопіювали таблицю правильно.")
    return

  await message.answer(f"🔍 Шукаю збіги для {len(photos)} виробів за пробою, вагою та розміром...")

  matched_count = 0

  for item in photos:
    caption = item["caption"]
    p_purity, p_weight, p_size = extract_specs(caption)

    found_price = None
    if p_purity and p_weight is not None:
      # Ищем соответствие в таблице (проба должна совпасть точно, вес с погрешностью до 0.02 г)
      for row in table_rows:
        if row["purity"] == p_purity and row["weight"] is not None:
          if abs(row["weight"] - p_weight) <= 0.02:
            # Если в посте указан размер, сверяем и его
            if p_size > 0 and row["size"] > 0:
              if abs(row["size"] - p_size) <= 0.2:
                found_price = row["price"]
                break
            else:
              found_price = row["price"]
              break

    if found_price is not None:
      new_caption = update_price_in_caption(caption, found_price, discount_percent, promo_text)
      matched_count += 1
    else:
      new_caption = caption  # Если збіг не найден, оставляем текст без изменений

    await bot.send_photo(
        chat_id=message.from_user.id,
        photo=item["photo"],
        caption=new_caption,
    )

  await state.clear()
  user_batch_photos[user_id] = []

  await message.answer(
      f"✅ Готово! Оброблено виробів: {len(photos)}\n"
      f"🎯 Успішно оновлено за характеристиками: {matched_count}\n\n"
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
  print("Бот запущен с поиском по характеристикам...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
