import asyncio
import html
import http.server
import io
import json
import logging
import math
import os
import re
import shutil
import threading

import openpyxl
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties  # aiogram >= 3.7
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "group_posts_db.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "user_settings.json")

MIN_CODE_LEN = 5          
CAPTION_LIMIT = 1024      
SUMMARY_DELAY = 1.5       

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def esc(text) -> str:
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Хранилище (JSON, атомарная запись)
# ---------------------------------------------------------------------------
def load_json(filename, default_val):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения {filename}: {e}")
    return default_val


def save_json(filename, data):
    tmp = filename + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filename)
    except Exception as e:
        logging.error(f"Ошибка сохранения {filename}: {e}")


def flatten_db(user_db) -> dict:
    if not isinstance(user_db, dict):
        return {}
    flat = {}
    for k, v in user_db.items():
        if isinstance(v, dict) and "photo" in v:
            flat[k] = v
        elif isinstance(v, dict):
            flat.update(flatten_db(v))
    return flat


user_databases = load_json(DB_FILE, {})
users_settings = load_json(SETTINGS_FILE, {})

for _uid in list(user_databases.keys()):
    user_databases[_uid] = flatten_db(user_databases[_uid])
save_json(DB_FILE, user_databases)

promo_batches: dict[int, list] = {}


def get_user_settings(user_id):
    uid = str(user_id)
    if uid not in users_settings:
        users_settings[uid] = {"promo": "АКЦІЙНА ПРОПОЗИЦІЯ", "discount": 6.0}
        save_json(SETTINGS_FILE, users_settings)
    return users_settings[uid]


def get_user_total_count(user_id) -> int:
    return len(user_databases.get(str(user_id), {}))


# ---------------------------------------------------------------------------
# Веб-сервер для Render
# ---------------------------------------------------------------------------
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

    httpd = http.server.HTTPServer(("0.0.0.0", port), FastHealthHandler)
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# Состояния и клавиатуры
# ---------------------------------------------------------------------------
class AdminStates(StatesGroup):
    waiting_for_custom_promo = State()
    waiting_for_custom_discount = State()
    waiting_for_group_table = State()      
    waiting_for_sync_confirm = State()     
    waiting_for_promo_photos = State()     
    waiting_for_clear_confirm = State()    


BTN_SYNC = "🔄 Отримати оновлені пости за таблицею"
BTN_PROMO = "🔥 Акція"
BTN_PROMO_TEXT = "✍️ Ввести свій текст акції"
BTN_DISCOUNT = "🔢 Ввести свій % знижки"
BTN_SETTINGS = "⚙️ Поточні налаштування"
BTN_STATS = "📊 Статистика бази"
BTN_CLEAR = "🗑 Очистити базу"
BTN_PROMO_DONE = "✅ Готово, застосувати знижку"
BTN_BACK = "🔙 Повернутися в головне меню"
BTN_SYNC_CONFIRM = "✅ Так, оновити базу"
BTN_CLEAR_CONFIRM = "✅ Так, очистити базу"

MENU_TEXTS = {BTN_SYNC, BTN_PROMO, BTN_PROMO_TEXT, BTN_DISCOUNT, BTN_SETTINGS, BTN_STATS, BTN_CLEAR, BTN_BACK}

NO_AUTOCOLLECT_STATES = {
    AdminStates.waiting_for_group_table.state,
    AdminStates.waiting_for_sync_confirm.state,
    AdminStates.waiting_for_promo_photos.state,
    AdminStates.waiting_for_clear_confirm.state,
}


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SYNC), KeyboardButton(text=BTN_PROMO)],
            [KeyboardButton(text=BTN_PROMO_TEXT), KeyboardButton(text=BTN_DISCOUNT)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_CLEAR)],
        ],
        resize_keyboard=True,
    )


def get_promo_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_PROMO_DONE)], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def get_cancel_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BACK)]], resize_keyboard=True)


def get_confirm_keyboard(confirm_text: str):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=confirm_text)], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


# ---------------------------------------------------------------------------
# Накопитель уведомлений
# ---------------------------------------------------------------------------
_pending: dict[int, dict] = {}


async def _flush_after(user_id: int, delay: float):
    await asyncio.sleep(delay)
    entry = _pending.pop(user_id, None)
    if not entry:
        return
    try:
        await entry["send"](entry["stats"])
    except Exception:
        logging.exception("Ошибка отправки итогового сообщения")


def schedule_summary(user_id: int, send, **inc):
    entry = _pending.get(user_id)
    if entry is None:
        entry = {"stats": {}, "send": send, "task": None}
        _pending[user_id] = entry
    for k, v in inc.items():
        entry["stats"][k] = entry["stats"].get(k, 0) + v
    entry["send"] = send
    if entry["task"]:
        entry["task"].cancel()
    entry["task"] = asyncio.create_task(_flush_after(user_id, SUMMARY_DELAY))


def drop_pending(user_id: int):
    entry = _pending.pop(user_id, None)
    if entry and entry["task"]:
        entry["task"].cancel()


# ---------------------------------------------------------------------------
# Работа с кодами, ценами и подписями
# ---------------------------------------------------------------------------
def normalize_code(code_str) -> str:
    if not code_str:
        return ""
    return re.sub(r"\D", "", str(code_str)).lstrip("0")


CODE_RE = re.compile(r"(?:штрихкод|код)[ \t]*[:\-]?[ \t]*(\d[\d ]*)", re.IGNORECASE)
CODE_LINE_RE = re.compile(
    r"^[ \t]*(?:код|штрихкод)[ \t]*[:\-]?[ \t]*[\d ]+[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
PRICE_RE = re.compile(
    r"(Ціна[ \t]*:?[ \t]*)"
    r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:[ \t]*(грнн|грн\.?|uah|₴))?",
    re.IGNORECASE,
)


def extract_code_from_text(text: str):
    if not text:
        return None
    m = CODE_RE.search(text)
    if not m:
        return None
    code = normalize_code(m.group(1))
    return code if len(code) >= MIN_CODE_LEN else None


def remove_code_line(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = CODE_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned).strip()
    return cleaned


def fmt_price(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def parse_price_number(raw: str) -> float:
    return float(raw.replace("\u00a0", "").replace(" ", "").replace(",", "."))


def update_price_and_clean_caption(caption: str, new_price: float) -> str:
    text = esc(caption or "")
    formatted = fmt_price(new_price)
    found = False

    def repl(m):
        nonlocal found
        found = True
        return f"{m.group(1)}{formatted} {m.group(3) or 'грн'}"

    text = PRICE_RE.sub(repl, text, count=1)
    if not found:
        text += f"\n\nЦіна {formatted} грн"
    return remove_code_line(text)


def apply_promo_logic(caption: str, promo_header: str, discount_percent: float) -> str:
    raw_caption = caption or ""
    # Защита от повторного применения акции
    if "🔥 Акційна ціна" in raw_caption or "<s>" in raw_caption:
        return remove_code_line(esc(raw_caption))

    text = esc(raw_caption)
    found = False

    def repl(m):
        nonlocal found
        try:
            price = parse_price_number(m.group(2))
        except ValueError:
            return m.group(0)
        if price <= 0:
            return m.group(0)
        found = True
        new_price = fmt_price(price * (1 - discount_percent / 100))
        currency = m.group(3) or "грн"
        return f"<s>{m.group(0).rstrip()}</s>\n\n🔥 Акційна ціна: {new_price} {currency}"

    text = PRICE_RE.sub(repl, text, count=1)
    if not found:
        text += f"\n\n🔥 Акційна ціна зі знижкою -{discount_percent:g}%"

    header = esc(promo_header or "")
    if header and header not in text:
        text = f"🚨 {header} 🚨\n\n{text}"
    return remove_code_line(text)


async def send_post(chat_id: int, photo: str, caption: str) -> bool:
    for _ in range(3):
        try:
            if len(caption) <= CAPTION_LIMIT:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
            else:
                await bot.send_photo(chat_id=chat_id, photo=photo)
                try:
                    await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)
                except TelegramAPIError:
                    await bot.send_message(chat_id=chat_id, text=caption[:4096], parse_mode=None)
            return True
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control: retry after {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramAPIError as e:
            logging.error(f"Ошибка отправки поста: {e}")
            return False
    return False


# ---------------------------------------------------------------------------
# Разбор таблицы 1С
# ---------------------------------------------------------------------------
def as_digits(cell):
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, float):
        if not cell.is_integer():
            return None
        return str(int(cell))
    if isinstance(cell, int):
        return str(cell)
    s = str(cell).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?[eE]\+?\d+", s):
        try:
            return str(int(float(s)))
        except ValueError:
            return None
    if re.fullmatch(r"\d[\d \u00a0]*", s):
        return re.sub(r"\D", "", s)
    return None


def to_number(cell):
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, float)):
        n = float(cell)
    else:
        s = str(cell).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            n = float(s)
        except ValueError:
            return None
    return n if math.isfinite(n) else None


def parse_row(cells):
    code, code_idx = None, None
    for i, c in enumerate(cells):
        digits = as_digits(c)
        if digits:
            norm = normalize_code(digits)
            if len(norm) >= MIN_CODE_LEN:
                code, code_idx = norm, i
                break
    if code is None:
        return None
    for c in reversed(cells[code_idx + 1:]):
        n = to_number(c)
        if n is not None and n > 10:
            return code, n
    return None


def parse_excel(data: bytes) -> dict:
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    prices = {}
    try:
        for row in wb.active.iter_rows(values_only=True):
            cells = [c for c in row if c is not None and str(c).strip() != ""]
            parsed = parse_row(cells)
            if parsed:
                prices[parsed[0]] = parsed[1]
    finally:
        wb.close()
    return prices


def parse_text_table(text: str) -> dict:
    prices = {}
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[;\t]", line) if p.strip()]
        if len(parts) < 2:
            parts = line.split()
        parsed = parse_row(parts)
        if parsed:
            prices[parsed[0]] = parsed[1]
    return prices


# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    drop_pending(uid)
    promo_batches.pop(uid, None)
    s = get_user_settings(uid)
    await message.answer(
        "Привіт! 👋 Я ваш персональний помічник для роботи з каталогом.\n\n"
        f"📋 Ваші поточні налаштування:\n"
        f"• Акція: {esc(s['promo'])}\n"
        f"• Знижка: {s['discount']:g}%\n\n"
        "• Пересилайте звичайні пости сюди — база захищена від дублікатів.\n"
        "• Натискайте «🔥 Акція», щоб переслати товари і на лету отримати розрахунок знижки!",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text == BTN_BACK)
async def cancel_to_main_menu(message: Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    drop_pending(uid)
    promo_batches.pop(uid, None)
    await message.answer("🏠 Ви повернулися до головного меню.", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_promo_photos, F.photo)
async def collect_promo_photos(message: Message, state: FSMContext):
    uid = message.from_user.id
    batch = promo_batches.setdefault(uid, [])
    batch.append({"photo": message.photo[-1].file_id, "caption": message.caption or ""})

    async def send(stats):
        count = len(promo_batches.get(uid, []))
        await message.answer(
            f"✅ Додано товарів для акції: <b>{count}</b>. Надсилайте ще або натисніть «Готово».",
            reply_markup=get_promo_keyboard(),
        )

    schedule_summary(uid, send)


@dp.message(F.photo)
async def auto_collect_posts(message: Message, state: FSMContext):
    if await state.get_state() in NO_AUTOCOLLECT_STATES:
        return

    uid_int = message.from_user.id
    uid = str(uid_int)
    caption = message.caption or ""
    code = extract_code_from_text(caption)

    if code:
        db = user_databases.setdefault(uid, {})
        is_update = code in db
        db[code] = {"photo": message.photo[-1].file_id, "caption": caption}
        save_json(DB_FILE, user_databases)
        inc = {"updated": 1} if is_update else {"added": 1}
    else:
        inc = {"nocode": 1}

    async def send(stats):
        lines = []
        if stats.get("added"):
            lines.append(f"✅ Додано нових товарів: <b>{stats['added']}</b>")
        if stats.get("updated"):
            lines.append(f"🔄 Оновлено (вже були в базі): <b>{stats['updated']}</b>")
        if stats.get("nocode"):
            lines.append(f"ℹ️ Без коду в тексті (не додано): <b>{stats['nocode']}</b>")
        lines.append(f"📦 Всього у базі: <b>{get_user_total_count(uid_int)} шт.</b>")
        await message.answer("\n".join(lines), reply_markup=get_main_keyboard())

    schedule_summary(uid_int, send, **inc)


@dp.message(F.text.in_(MENU_TEXTS))
async def handle_menu_buttons(message: Message, state: FSMContext):
    uid_int = message.from_user.id
    uid = str(uid_int)
    text = message.text
    await state.clear()
    drop_pending(uid_int)
    promo_batches.pop(uid_int, None)

    settings = get_user_settings(uid_int)
    total = get_user_total_count(uid_int)

    if text == BTN_SYNC:
        if total == 0:
            await message.answer(
                "⚠️ Ваша база даних порожня! Перешліть пости з групи сюди.",
                reply_markup=get_main_keyboard(),
            )
            return
        await state.set_state(AdminStates.waiting_for_group_table)
        await message.answer(
            f"📥 У вашій базі даних збережено виробів: <b>{total} шт.</b>\n\n"
            "Надішліть файл Excel (<code>.xlsx</code>) або скопіюйте текст із таблиці ціноутворення 1С.\n"
            "<i>(Після підтвердження все, чого немає в цій таблиці, буде видалено як продане)</i>",
            reply_markup=get_cancel_keyboard(),
        )

    elif text == BTN_PROMO:
        promo_batches[uid_int] = []
        await state.set_state(AdminStates.waiting_for_promo_photos)
        await message.answer(
            f"🔥 <b>Режим акції (поточна знижка: {settings['discount']:g}%)</b>\n\n"
            "Перешліть сюди товари, для яких робите акцію (ніяких кодів не потрібно!). "
            "Коли перешліть усі, натисніть кнопку нижче ⬇️",
            reply_markup=get_promo_keyboard(),
        )

    elif text == BTN_STATS:
        await message.answer(
            f"📊 Статистика:\n"
            f"• Загалом збережених товарів у вашій базі: <b>{total} шт.</b>\n"
            f"• Ваша поточна акція: {esc(settings['promo'])}\n"
            f"• Ваша поточна знижка: {settings['discount']:g}%",
            reply_markup=get_main_keyboard(),
        )

    elif text == BTN_CLEAR:
        await state.set_state(AdminStates.waiting_for_clear_confirm)
        await message.answer(
            f"⚠️ Ви впевнені? Буде видалено всі товари з бази (<b>{total} шт.</b>).",
            reply_markup=get_confirm_keyboard(BTN_CLEAR_CONFIRM),
        )

    elif text == BTN_PROMO_TEXT:
        await state.set_state(AdminStates.waiting_for_custom_promo)
        await message.answer(
            "✍️ Надішліть текстом новий заголовок для вашої акції:",
            reply_markup=get_cancel_keyboard(),
        )

    elif text == BTN_DISCOUNT:
        await state.set_state(AdminStates.waiting_for_custom_discount)
        await message.answer(
            "🔢 Введіть числом бажаний відсоток знижки (наприклад: 6 або 12):",
            reply_markup=get_cancel_keyboard(),
        )

    elif text == BTN_SETTINGS:
        await message.answer(
            f"📋 Ваші поточні налаштування:\n"
            f"• Акція: {esc(settings['promo'])}\n"
            f"• Знижка: {settings['discount']:g}%",
            reply_markup=get_main_keyboard(),
        )


@dp.message(AdminStates.waiting_for_clear_confirm, F.text == BTN_CLEAR_CONFIRM)
async def confirm_clear(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    if os.path.exists(DB_FILE):
        shutil.copy2(DB_FILE, DB_FILE + ".bak")
    user_databases[uid] = {}
    save_json(DB_FILE, user_databases)
    await state.clear()
    await message.answer("🗑 Ваша база даних повністю очищена.", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_promo_photos, F.text == BTN_PROMO_DONE)
async def finish_promo_batch(message: Message, state: FSMContext):
    uid = message.from_user.id
    drop_pending(uid)
    batch = promo_batches.pop(uid, [])
    await state.clear()

    if not batch:
        await message.answer("❌ Ви не надіслали жодного товару для акції!", reply_markup=get_main_keyboard())
        return

    settings = get_user_settings(uid)
    await message.answer(f"🔄 Формую акційні пости (знижка -{settings['discount']:g}%)...")

    sent, failed = 0, 0
    for item in batch:
        caption = apply_promo_logic(item["caption"], settings["promo"], settings["discount"])
        if await send_post(uid, item["photo"], caption):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.3)

    report = (
        f"✅ Готово! Надіслано акційних постів: <b>{sent}</b>\n"
        "Усі ціни зі знижкою, штрихкоди приховані — можете пересилати у групу!"
    )
    if failed:
        report += f"\n⚠️ Не вдалося надіслати: <b>{failed}</b> (деталі в логах)"
    await message.answer(report, reply_markup=get_main_keyboard())


@dp.message(
    AdminStates.waiting_for_group_table,
    F.document | (F.text & ~F.text.in_(MENU_TEXTS))
)
async def process_table(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    price_database = {}

    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith(".xlsx"):
            await message.answer(
                "❌ Підтримується лише формат <code>.xlsx</code> (або надішліть текст таблиці).",
                reply_markup=get_cancel_keyboard(),
            )
            return
        try:
            file_info = await bot.get_file(message.document.file_id)
            buf = await bot.download_file(file_info.file_path)
            price_database = await asyncio.to_thread(parse_excel, buf.read())
        except Exception as e:
            await message.answer(
                f"❌ Помилка читання файлу: {esc(e)}",
                reply_markup=get_cancel_keyboard(),
            )
            return
    elif message.text:
        price_database = parse_text_table(message.text)

    if not price_database:
        await message.answer(
            "❌ Не вдалося розпізнати штрихкоди та ціни. Перевірте таблицю й спробуйте ще раз.",
            reply_markup=get_cancel_keyboard(),
        )
        return

    flat_posts = user_databases.get(uid, {})
    matched = [c for c in flat_posts if c in price_database]
    to_remove = len(flat_posts) - len(matched)
    not_found = [c for c in price_database if c not in flat_posts]

    if not matched:
        await state.clear()
        await message.answer(
            f"❌ Жоден товар із бази не знайдено в таблиці (розпізнано позицій: {len(price_database)}).\n"
            "Базу не змінено. Перевірте, що в таблиці саме штрихкоди.",
            reply_markup=get_main_keyboard(),
        )
        return

    await state.update_data(prices=price_database)
    await state.set_state(AdminStates.waiting_for_sync_confirm)
    await message.answer(
        f"🔎 Розпізнано позицій у таблиці: <b>{len(price_database)}</b>\n"
        f"• У базі: <b>{len(flat_posts)}</b>\n"
        f"• Збігів (будуть оновлені): <b>{len(matched)}</b>\n"
        f"• Буде видалено з бази як продані: <b>{to_remove}</b>\n"
        f"• Є в таблиці, але немає в базі: <b>{len(not_found)}</b>\n\n"
        "Підтвердити оновлення?",
        reply_markup=get_confirm_keyboard(BTN_SYNC_CONFIRM),
    )


@dp.message(AdminStates.waiting_for_sync_confirm, F.text == BTN_SYNC_CONFIRM)
async def confirm_sync(message: Message, state: FSMContext):
    uid_int = message.from_user.id
    uid = str(uid_int)
    data = await state.get_data()
    price_database = data.get("prices") or {}
    await state.clear()

    if not price_database:
        await message.answer("❌ Дані таблиці втрачено. Почніть спочатку.", reply_markup=get_main_keyboard())
        return

    await message.answer("🔄 Оновлюю ціни та синхронізую базу з 1С...")

    flat_posts = user_databases.get(uid, {})
    new_user_db = {}
    removed = 0
    sent, failed = 0, 0

    for code, post in flat_posts.items():
        if code in price_database:
            new_user_db[code] = post
            caption = update_price_and_clean_caption(post["caption"], price_database[code])
            if await send_post(uid_int, post["photo"], caption):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.3)
        else:
            removed += 1

    not_found = [c for c in price_database if c not in flat_posts]

    if os.path.exists(DB_FILE):
        shutil.copy2(DB_FILE, DB_FILE + ".bak")
    user_databases[uid] = new_user_db
    save_json(DB_FILE, user_databases)

    report = (
        f"✅ Готово!\n"
        f"• Надіслано оновлених постів: <b>{sent}</b>\n"
        f"• Видалено з бази (продано / немає в 1С): <b>{removed} шт.</b>\n"
        f"• У вашій базі тепер актуальних товарів: <b>{len(new_user_db)} шт.</b>"
    )
    if failed:
        report += f"\n• ⚠️ Не вдалося надіслати: <b>{failed}</b> (деталі в логах)"
    if not_found:
        preview = ", ".join(not_found[:20])
        extra = f" (і ще {len(not_found) - 20} шт.)" if len(not_found) > 20 else ""
        report += (
            f"\n\n⚠️ <b>У таблиці 1С є товари, яких немає у вашій базі ({len(not_found)} шт.):</b>\n"
            f"<code>{preview}{extra}</code>"
        )
    await message.answer(report, reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_sync_confirm)
async def sync_confirm_reminder(message: Message):
    await message.answer(
        "Натисніть «✅ Так, оновити базу» або поверніться в головне меню.",
        reply_markup=get_confirm_keyboard(BTN_SYNC_CONFIRM),
    )


@dp.message(AdminStates.waiting_for_custom_promo, F.text)
async def save_custom_promo(message: Message, state: FSMContext):
    settings = get_user_settings(message.from_user.id)
    settings["promo"] = message.text.strip()
    save_json(SETTINGS_FILE, users_settings)
    await state.clear()
    await message.answer("✅ Текст акції змінено для вашого аккаунта!", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_custom_discount, F.text)
async def save_custom_discount(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip().replace("%", "").replace(",", "."))
    except ValueError:
        await message.answer("❌ Введіть число:", reply_markup=get_cancel_keyboard())
        return
    if not (0 < val < 100):
        await message.answer("❌ Відсоток має бути більшим за 0 і меншим за 100:", reply_markup=get_cancel_keyboard())
        return
    settings = get_user_settings(message.from_user.id)
    settings["discount"] = val
    save_json(SETTINGS_FILE, users_settings)
    await state.clear()
    await message.answer(f"✅ Знижку змінено на: {val:g}% для вашого аккаунта!", reply_markup=get_main_keyboard())


async def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("PORT"):
        threading.Thread(target=run_dummy_web_server, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущено...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
