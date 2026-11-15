import asyncio
import csv
import html
import http.server
import io
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import openpyxl
import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Не задана переменная окружения DATABASE_URL")

MIN_CODE_LEN = 5
CAPTION_LIMIT = 1024

DEFAULT_PROMO = "АКЦІЙНА ПРОПОЗИЦІЯ"
DEFAULT_DISCOUNT = 6.0

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def esc(text) -> str:
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Хранилище (PostgreSQL / Supabase)
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.lock = threading.RLock()
        self._init_db()

    def _get_conn(self):
        conn = psycopg2.connect(self.dsn, cursor_factory=psycopg2.extras.DictCursor)
        conn.autocommit = True
        return conn

    def _init_db(self):
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS posts (
                            user_id   BIGINT NOT NULL,
                            code      TEXT    NOT NULL,
                            photo     TEXT    NOT NULL,
                            caption   TEXT    NOT NULL DEFAULT '',
                            updated_at DOUBLE PRECISION NOT NULL,
                            PRIMARY KEY (user_id, code)
                        );
                        CREATE TABLE IF NOT EXISTS settings (
                            user_id   BIGINT PRIMARY KEY,
                            promo     TEXT    NOT NULL,
                            discount  DOUBLE PRECISION NOT NULL,
                            sheet_url TEXT    NOT NULL DEFAULT ''
                        );
                        ALTER TABLE settings ADD COLUMN IF NOT EXISTS sheet_url TEXT NOT NULL DEFAULT '';
                    """)
            finally:
                conn.close()

    # -- посты --
    def upsert_post(self, user_id: int, code: str, photo: str, caption: str) -> bool:
        """Возвращает True, если пост уже существовал (обновление)."""
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM posts WHERE user_id=%s AND code=%s", (user_id, code)
                    )
                    exists = cur.fetchone() is not None

                    cur.execute(
                        """
                        INSERT INTO posts(user_id, code, photo, caption, updated_at) 
                        VALUES(%s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, code) DO UPDATE SET 
                        photo = EXCLUDED.photo, 
                        caption = EXCLUDED.caption, 
                        updated_at = EXCLUDED.updated_at
                        """,
                        (user_id, code, photo, caption, time.time()),
                    )
                return exists
            finally:
                conn.close()

    def count_posts(self, user_id: int) -> int:
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS c FROM posts WHERE user_id=%s", (user_id,))
                    row = cur.fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()

    def get_posts(self, user_id: int) -> dict:
        """dict: code -> {"photo": ..., "caption": ...}"""
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT code, photo, caption FROM posts WHERE user_id=%s", (user_id,))
                    rows = cur.fetchall()
                return {r["code"]: {"photo": r["photo"], "caption": r["caption"]} for r in rows}
            finally:
                conn.close()

    def replace_posts(self, user_id: int, posts: dict):
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM posts WHERE user_id=%s", (user_id,))
                    data = [
                        (user_id, code, post["photo"], post.get("caption", ""), time.time())
                        for code, post in posts.items()
                    ]
                    if data:
                        psycopg2.extras.execute_values(
                            cur,
                            "INSERT INTO posts(user_id, code, photo, caption, updated_at) VALUES %s",
                            data
                        )
            finally:
                conn.close()

    def clear_posts(self, user_id: int):
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM posts WHERE user_id=%s", (user_id,))
            finally:
                conn.close()

    # -- настройки --
    def get_settings(self, user_id: int):
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT promo, discount, COALESCE(sheet_url, '') AS sheet_url FROM settings WHERE user_id=%s",
                        (user_id,),
                    )
                    row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "promo": row["promo"],
                    "discount": float(row["discount"]),
                    "sheet_url": row["sheet_url"] or "",
                }
            finally:
                conn.close()

    def set_settings(self, user_id: int, promo: str, discount: float, sheet_url: str | None = None):
        with self.lock:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    if sheet_url is None:
                        cur.execute(
                            """
                            INSERT INTO settings(user_id, promo, discount, sheet_url) VALUES(%s, %s, %s, '')
                            ON CONFLICT (user_id) DO UPDATE SET
                            promo = EXCLUDED.promo,
                            discount = EXCLUDED.discount
                            """,
                            (user_id, promo, discount),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO settings(user_id, promo, discount, sheet_url) VALUES(%s, %s, %s, %s)
                            ON CONFLICT (user_id) DO UPDATE SET
                            promo = EXCLUDED.promo,
                            discount = EXCLUDED.discount,
                            sheet_url = EXCLUDED.sheet_url
                            """,
                            (user_id, promo, discount, sheet_url),
                        )
            finally:
                conn.close()

    def backup(self, dest_path: str):
        pass


db = Database(DATABASE_URL)
promo_batches: dict[int, list] = {}
last_nocode_posts: dict[int, list] = {}


def get_user_settings(user_id: int) -> dict:
    s = db.get_settings(user_id)
    if s is None:
        s = {"promo": DEFAULT_PROMO, "discount": DEFAULT_DISCOUNT, "sheet_url": ""}
        db.set_settings(user_id, s["promo"], s["discount"], s["sheet_url"])
    return s


def get_user_total_count(user_id: int) -> int:
    return db.count_posts(user_id)


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
    waiting_for_edit_caption = State()
    waiting_for_sheet_url = State()


BTN_SYNC = "🔄 Отримати оновлені пости за таблицею"
BTN_PROMO = "🔥 Акція"
BTN_SHEET_SETUP = "🔗 Підключити Google Таблицю"
BTN_PROMO_TEXT = "✍️ Ввести свій текст акції"
BTN_DISCOUNT = "🔢 Ввести свій % знижки"
BTN_SETTINGS = "⚙️ Поточні налаштування"
BTN_STATS = "📊 Статистика бази"
BTN_CLEAR = "🗑 Очистити базу"
BTN_PROMO_DONE = "✅ Готово, застосувати знижку"
BTN_BACK = "🔙 Повернутися в головне меню"
BTN_SYNC_CONFIRM = "✅ Так, оновити базу"
BTN_CLEAR_CONFIRM = "✅ Так, очистити базу"
BTN_EDIT_CANCEL = "❌ Скасувати редагування"
BTN_SYNC_FROM_SHEET = "☁️ Взяти дані з моєї Google Таблиці"
BTN_SHEET_CLEAR = "🗑 Відв'язати Google Таблицю"

MENU_TEXTS = {
    BTN_SYNC,
    BTN_PROMO,
    BTN_SHEET_SETUP,
    BTN_PROMO_TEXT,
    BTN_DISCOUNT,
    BTN_SETTINGS,
    BTN_STATS,
    BTN_CLEAR,
    BTN_BACK,
}

NO_AUTOCOLLECT_STATES = {
    AdminStates.waiting_for_group_table.state,
    AdminStates.waiting_for_sync_confirm.state,
    AdminStates.waiting_for_promo_photos.state,
    AdminStates.waiting_for_clear_confirm.state,
    AdminStates.waiting_for_custom_promo.state,
    AdminStates.waiting_for_custom_discount.state,
    AdminStates.waiting_for_edit_caption.state,
    AdminStates.waiting_for_sheet_url.state,
}


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SYNC), KeyboardButton(text=BTN_PROMO)],
            [KeyboardButton(text=BTN_SHEET_SETUP)],
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


def get_sync_source_keyboard(has_sheet: bool):
    rows = []
    if has_sheet:
        rows.append([KeyboardButton(text=BTN_SYNC_FROM_SHEET)])
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_sheet_setup_keyboard(has_sheet: bool):
    rows = []
    if has_sheet:
        rows.append([KeyboardButton(text=BTN_SHEET_CLEAR)])
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_confirm_keyboard(confirm_text: str):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=confirm_text)], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def get_edit_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_EDIT_CANCEL)]],
        resize_keyboard=True,
    )


def get_edit_inline_keyboard(prefill_text: str = ""):
    buttons = [
        [InlineKeyboardButton(text="✏️ Редагувати", callback_data="edit_caption")]
    ]
    if prefill_text and len(prefill_text) <= 250:
        buttons.append([
            InlineKeyboardButton(
                text="📝 Дописати у поле вводу",
                switch_inline_query_current_chat=prefill_text,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_nocode_inline_keyboard(count: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"📤 Надіслати пости без штрихкоду ({count} шт.)",
                callback_data="show_nocode_posts"
            )
        ]]
    )


# ---------------------------------------------------------------------------
# Надійна черга для масового збереження постів без втрат
# ---------------------------------------------------------------------------
_import_queues: dict[int, list] = {}
_import_tasks: dict[int, asyncio.Task] = {}


async def _process_queue(user_id: int, message: Message):
    await asyncio.sleep(1.5)
    queue = _import_queues.pop(user_id, None)
    _import_tasks.pop(user_id, None)

    if not queue:
        return

    added = 0
    updated_codes = []
    batch_dup_codes = []
    nocode_items = []

    seen_in_batch = set()

    for item in queue:
        caption = item["caption"]
        photo = item["photo"]
        code = extract_code_from_text(caption)

        if code:
            if code in seen_in_batch:
                batch_dup_codes.append(code)
                continue
            seen_in_batch.add(code)

            is_update = db.upsert_post(user_id, code, photo, caption)
            if is_update:
                updated_codes.append(code)
            else:
                added += 1
        else:
            nocode_items.append({"photo": photo, "caption": caption})

    total = get_user_total_count(user_id)
    lines = [
        "📊 <b>Результат масового імпорту:</b>",
        f"• Додано нових товарів: <b>{added}</b>",
    ]

    if batch_dup_codes:
        unique_batch_dups = list(dict.fromkeys(batch_dup_codes))
        codes_str = ", ".join(f"<code>{c}</code>" for c in unique_batch_dups)
        lines.append(
            f"• 🔄 Дублікати всередині пачки (пропущено <b>{len(batch_dup_codes)}</b> шт.):\n  {codes_str}"
        )

    if updated_codes:
        unique_updated = list(dict.fromkeys(updated_codes))
        codes_str = ", ".join(f"<code>{c}</code>" for c in unique_updated)
        lines.append(
            f"• ♻️ Вже були в базі (оновлено <b>{len(updated_codes)}</b> шт.):\n  {codes_str}"
        )

    if nocode_items:
        last_nocode_posts[user_id] = nocode_items
        lines.append(f"• ⚠️ Без штрихкоду (не додано): <b>{len(nocode_items)} шт.</b>")
    else:
        last_nocode_posts.pop(user_id, None)

    lines.append(f"\n📦 Всього у базі: <b>{total} шт.</b>")

    try:
        if nocode_items:
            await message.answer(
                "\n".join(lines),
                reply_markup=get_nocode_inline_keyboard(len(nocode_items)),
            )
        else:
            await message.answer("\n".join(lines), reply_markup=get_main_keyboard())
    except Exception:
        logging.exception("Помилка відправки звіту про імпорт")


@dp.callback_query(F.data == "show_nocode_posts")
async def on_show_nocode_posts(callback: CallbackQuery):
    uid = callback.from_user.id
    items = last_nocode_posts.pop(uid, None)
    if not items:
        await callback.answer("Немає збережених постів без штрихкоду.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    await callback.message.answer(f"📤 Надсилаю пости без штрихкоду ({len(items)} шт.):")
    for item in items:
        raw_cap = item["caption"]
        caption = esc(raw_cap) if raw_cap else "<i>(Без підпису)</i>"
        await send_post(uid, item["photo"], caption, raw_prefill=raw_cap)
        await asyncio.sleep(0.3)

    await callback.message.answer(
        "✅ Усі пости без штрихкоду надіслано вище.\n"
        "Натисніть «✏️ Редагувати» під постом — бот надішле готовий текст, який можна скопіювати одним дотиком і дописати код (або відразу впишіть рядок <code>Код 123456...</code>, щоб бот сам додав його в опис і зберіг у базу!).",
        reply_markup=get_main_keyboard(),
    )


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
    cleaned = raw.replace("\u00a0", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    return float(cleaned)


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


async def send_post(chat_id: int, photo: str, caption: str, raw_prefill: str = ""):
    kb = get_edit_inline_keyboard(raw_prefill)
    for _ in range(3):
        try:
            if len(caption) <= CAPTION_LIMIT:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=kb,
                )
            else:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    reply_markup=kb,
                )
                try:
                    await bot.send_message(
                        chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML
                    )
                except TelegramAPIError:
                    await bot.send_message(
                        chat_id=chat_id, text=caption[:4096], parse_mode=None
                    )
            return msg.message_id
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control: retry after {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramAPIError as e:
            logging.error(f"Ошибка отправки поста: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Разбор таблицы 1С и Google Таблиц
# ---------------------------------------------------------------------------
def as_digits(cell):
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, int):
        return str(cell)
    if isinstance(cell, float):
        s = repr(cell)
        if re.fullmatch(r"\d+[eE]\+?\d+", s) or re.fullmatch(r"\d+\.0", s):
            return str(int(cell))
        return None
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
        try:
            n = parse_price_number(str(cell))
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
        if n is not None and n > 0:
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


def build_google_sheet_csv_url(raw_url: str) -> str:
    """Превращает любую ссылку на Google Таблицу (включая gid конкретного листа) в прямую CSV-ссылку."""
    url = raw_url.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        if "output=csv" in url or "format=csv" in url:
            return url
        raise ValueError("Невірне посилання. Скопіюйте посилання з адресного рядка Google Таблиці.")

    sheet_id = m.group(1)
    gid_match = re.search(r"[#?&]gid=(\d+)", url)
    gid_part = f"&gid={gid_match.group(1)}" if gid_match else ""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{gid_part}"


def fetch_google_sheet_prices(raw_url: str) -> dict:
    csv_url = build_google_sheet_csv_url(raw_url)
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        content_type = resp.headers.get("Content-Type", "")
        raw_bytes = resp.read()

    if "text/html" in content_type.lower():
        raise ValueError(
            "Таблиця закрита для доступу! Відкрийте в Google Таблиці «Налаштування доступу» → «Усі, хто має посилання» (Читач)."
        )

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    prices = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        cells = [c for c in row if c is not None and str(c).strip() != ""]
        parsed = parse_row(cells)
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
    promo_batches.pop(uid, None)
    s = get_user_settings(uid)
    sheet_status = "✅ підключена" if s.get("sheet_url") else "❌ не підключена"
    await message.answer(
        "Привіт! 👋 Я ваш персональний помічник для роботи з Вітриною.\n\n"
        f"📋 Ваші поточні налаштування:\n"
        f"• Акція: {esc(s['promo'])}\n"
        f"• Знижка: {s['discount']:g}%\n"
        f"• Google Таблиця: {sheet_status}\n\n"
        "• Пересилайте звичайні пости сюди — база захищена від дублікатів.\n"
        "• Натискайте «🔥 Акція», щоб переслати товари і на лету отримати розрахунок знижки!\n"
        "• Під кожним відправленим постом є кнопка «✏️ Редагувати».",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text.in_({BTN_BACK, BTN_EDIT_CANCEL}))
async def cancel_to_main_menu(message: Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    promo_batches.pop(uid, None)
    await message.answer("🏠 Ви повернулися до головного меню.", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_promo_photos, F.photo)
async def collect_promo_photos(message: Message, state: FSMContext):
    uid = message.from_user.id
    batch = promo_batches.setdefault(uid, [])
    batch.append({"photo": message.photo[-1].file_id, "caption": message.caption or ""})
    count = len(batch)
    await message.answer(
        f"✅ Додано товарів для акції: <b>{count}</b>. Надсилайте ще або натисніть «Готово».",
        reply_markup=get_promo_keyboard(),
    )


@dp.message(F.photo)
async def auto_collect_posts(message: Message, state: FSMContext):
    if await state.get_state() in NO_AUTOCOLLECT_STATES:
        return

    uid_int = message.from_user.id
    caption = message.caption or ""
    photo = message.photo[-1].file_id

    if uid_int not in _import_queues:
        _import_queues[uid_int] = []
    _import_queues[uid_int].append({"photo": photo, "caption": caption})

    if uid_int in _import_tasks:
        _import_tasks[uid_int].cancel()

    _import_tasks[uid_int] = asyncio.create_task(_process_queue(uid_int, message))


@dp.message(F.text.in_(MENU_TEXTS))
async def handle_menu_buttons(message: Message, state: FSMContext):
    uid_int = message.from_user.id
    text = message.text
    await state.clear()
    promo_batches.pop(uid_int, None)

    settings = get_user_settings(uid_int)
    total = get_user_total_count(uid_int)
    has_sheet = bool(settings.get("sheet_url"))

    if text == BTN_SYNC:
        if total == 0:
            await message.answer(
                "⚠️ Ваша база даних порожня! Перешліть пости з групи сюди.",
                reply_markup=get_main_keyboard(),
            )
            return
        await state.set_state(AdminStates.waiting_for_group_table)
        hint_sheet = (
            "\n\n☁️ Або натисніть кнопку нижче, щоб завантажити дані з вашої прив'язаної Google Таблиці!"
            if has_sheet
            else "\n\n💡 <i>Порада: ви можете один раз прив'язати свою Google Таблицю через кнопку «🔗 Підключити Google Таблицю», щоб не скидати файл щоразу.</i>"
        )
        await message.answer(
            f"📥 У вашій базі даних збережено виробів: <b>{total} шт.</b>\n\n"
            "Надішліть файл Excel (<code>.xlsx</code>) або скопіюйте текст із таблиці ціноутворення 1С."
            f"{hint_sheet}\n"
            "<i>(Після підтвердження все, чого немає в цій таблиці, буде видалено як продане)</i>",
            reply_markup=get_sync_source_keyboard(has_sheet),
        )

    elif text == BTN_SHEET_SETUP:
        await state.set_state(AdminStates.waiting_for_sheet_url)
        current_info = (
            f"🔗 Зараз підключена таблиця:\n<code>{esc(settings['sheet_url'])}</code>\n\n"
            "Щоб замінити її, надішліть нове посилання на Google Таблицю (або відв'яжіть поточну кнопкою нижче):"
            if has_sheet
            else "🔗 <b>Підключення особистої Google Таблиці</b>\n\n"
                 "1. Відкрийте вашу Google Таблицю з кодами та цінами.\n"
                 "2. Натисніть «Поділитися» → «Усі, хто має посилання» (роль: <b>Читач</b>).\n"
                 "3. Скопіюйте посилання з браузера та надішліть його сюди.\n\n"
                 "<i>Посилання закріпиться лише за вашим обліковим записом у базі даних.</i>"
        )
        await message.answer(
            current_info,
            reply_markup=get_sheet_setup_keyboard(has_sheet),
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
        sheet_label = "✅ підключена" if has_sheet else "❌ не підключена"
        await message.answer(
            f"📊 Статистика:\n"
            f"• Загалом збережених товарів у вашій базі: <b>{total} шт.</b>\n"
            f"• Ваша поточна акція: {esc(settings['promo'])}\n"
            f"• Ваша поточна знижка: {settings['discount']:g}%\n"
            f"• Google Таблиця: {sheet_label}",
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
        sheet_line = (
            f"\n• Google Таблиця: <code>{esc(settings['sheet_url'])}</code>"
            if has_sheet
            else "\n• Google Таблиця: не підключена"
        )
        await message.answer(
            f"📋 Ваші поточні налаштування:\n"
            f"• Акція: {esc(settings['promo'])}\n"
            f"• Знижка: {settings['discount']:g}%"
            f"{sheet_line}",
            reply_markup=get_main_keyboard(),
        )


@dp.message(AdminStates.waiting_for_sheet_url, F.text == BTN_SHEET_CLEAR)
async def clear_user_sheet_url(message: Message, state: FSMContext):
    uid = message.from_user.id
    s = get_user_settings(uid)
    db.set_settings(uid, s["promo"], s["discount"], sheet_url="")
    await state.clear()
    await message.answer("🗑 Посилання на Google Таблицю видалено з ваших налаштувань.", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_sheet_url, F.text)
async def save_user_sheet_url(message: Message, state: FSMContext):
    uid = message.from_user.id
    raw_url = message.text.strip()
    has_sheet = bool(get_user_settings(uid).get("sheet_url"))

    await message.answer("⏳ Перевіряю доступ до Google Таблиці...")
    try:
        prices = await asyncio.to_thread(fetch_google_sheet_prices, raw_url)
    except Exception as e:
        await message.answer(
            f"❌ Не вдалося прочитати таблицю: {esc(e)}\n\nПеревірте посилання та права доступу й спробуйте ще раз:",
            reply_markup=get_sheet_setup_keyboard(has_sheet),
        )
        return

    if not prices:
        await message.answer(
            "⚠️ Таблиця відкрилася, але в ній не знайдено жодного штрихкоду та ціни.\n"
            "Перевірте, що в таблиці є стовпці зі штрихкодом та ціною, і надішліть посилання ще раз:",
            reply_markup=get_sheet_setup_keyboard(has_sheet),
        )
        return

    s = get_user_settings(uid)
    db.set_settings(uid, s["promo"], s["discount"], sheet_url=raw_url)
    await state.clear()
    await message.answer(
        f"✅ <b>Google Таблицю успішно підключено до вашого профілю!</b>\n"
        f"• Розпізнано позицій у таблиці: <b>{len(prices)} шт.</b>\n\n"
        "Тепер при натисканні «🔄 Отримати оновлені пости за таблицею» ви зможете оновлювати базу в один клік напряму з неї!",
        reply_markup=get_main_keyboard(),
    )


@dp.message(AdminStates.waiting_for_clear_confirm, F.text == BTN_CLEAR_CONFIRM)
async def confirm_clear(message: Message, state: FSMContext):
    uid = message.from_user.id
    db.clear_posts(uid)
    await state.clear()
    await message.answer("🗑 Ваша база даних повністю очищена.", reply_markup=get_main_keyboard())


@dp.message(AdminStates.waiting_for_promo_photos, F.text == BTN_PROMO_DONE)
async def finish_promo_batch(message: Message, state: FSMContext):
    uid = message.from_user.id
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
        msg_id = await send_post(uid, item["photo"], caption)
        if msg_id is not None:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.3)

    report = (
        f"✅ Готово! Надіслано акційних постів: <b>{sent}</b>\n"
        "Усі ціни зі знижкою, штрихкоди приховані — можете пересилати у групу!\n"
        "Під кожним постом є кнопка «✏️ Редагувати»."
    )
    if failed:
        report += f"\n⚠️ Не вдалося надіслати: <b>{failed}</b> (деталі в логах)"
    await message.answer(report, reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "edit_caption")
async def on_edit_caption(callback: CallbackQuery, state: FSMContext):
    msg = callback.message
    if msg is None or not msg.photo:
        await callback.answer("Це повідомлення не можна редагувати.", show_alert=True)
        return

    old_caption = msg.caption or ""
    if old_caption == "(Без підпису)":
        old_caption = ""
    photo_id = msg.photo[-1].file_id

    await state.set_state(AdminStates.waiting_for_edit_caption)
    await state.update_data(
        edit_chat_id=msg.chat.id,
        edit_message_id=msg.message_id,
        edit_old_caption=old_caption,
        edit_photo_id=photo_id,
    )
    await callback.answer()

    if old_caption:
        await callback.message.answer(
            "✏️ <b>Поточний текст поста</b> (натисніть на блок нижче, щоб скопіювати, вставте в поле вводу й допишіть код):\n\n"
            f"<pre>{esc(old_caption)}</pre>\n"
            "💡 <b>Швидкий спосіб:</b> якщо потрібно лише додати код до цього опису — просто надішліть <code>Код 1000287002666</code> (або самі цифри коду), і я автоматично вставлю його в текст і збережу пост в базу!",
            reply_markup=get_edit_cancel_keyboard(),
        )
    else:
        await callback.message.answer(
            "✏️ У цього поста поки немає підпису. Надішліть новий текст для нього (наприклад, з рядком <code>Код 1000287002666</code>):",
            reply_markup=get_edit_cancel_keyboard(),
        )


def _insert_code_into_caption(old_caption: str, code_input: str) -> str:
    digits = normalize_code(code_input)
    code_line = f"Код {digits}"
    if not old_caption:
        return code_line

    lines = old_caption.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"ціна", line, re.IGNORECASE):
            lines.insert(i, code_line)
            return "\n".join(lines)

    return f"{old_caption.rstrip()}\n{code_line}"


@dp.message(AdminStates.waiting_for_edit_caption, F.text)
async def save_edited_caption(message: Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    chat_id = data.get("edit_chat_id")
    message_id = data.get("edit_message_id")
    old_caption = data.get("edit_old_caption") or ""
    photo_id = data.get("edit_photo_id")

    raw_input = re.sub(r"^@\w+\s*", "", message.text).strip()
    await state.clear()

    if not chat_id or not message_id:
        await message.answer("❌ Помилка сесії редагування. Спробуйте ще раз.", reply_markup=get_main_keyboard())
        return

    is_only_code = bool(
        re.fullmatch(r"(?:(?:штрихкод|код)[ \t]*[:\-]?[ \t]*)?\d[\d ]*", raw_input, re.IGNORECASE)
        and len(normalize_code(raw_input)) >= MIN_CODE_LEN
    )

    if is_only_code and old_caption:
        new_caption = _insert_code_into_caption(old_caption, raw_input)
    else:
        new_caption = raw_input

    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=new_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=get_edit_inline_keyboard(),
        )
    except TelegramAPIError as e:
        logging.error(f"Помилка редагування caption: {e}")
        await message.answer(
            f"❌ Не вдалося оновити пост у Telegram: {esc(e)}\n"
            "Можливо, текст занадто довгий або містить невалідні HTML-теги.",
            reply_markup=get_main_keyboard(),
        )
        return

    plain_caption = re.sub(r"<[^>]+>", "", new_caption)
    code = extract_code_from_text(plain_caption)
    if code and photo_id:
        is_update = db.upsert_post(uid, code, photo_id, plain_caption)
        total = get_user_total_count(uid)
        action_word = "оновлено в базі" if is_update else "додано в базу"
        await message.answer(
            f"✅ Підпис оновлено, а товар з кодом <code>{code}</code> автоматично {action_word}!\n"
            f"📦 Всього у базі: <b>{total} шт.</b>",
            reply_markup=get_main_keyboard(),
        )
    else:
        await message.answer("✅ Підпис до поста успішно оновлено!", reply_markup=get_main_keyboard())


async def _prepare_sync_confirmation(message: Message, state: FSMContext, uid: int, price_database: dict):
    flat_posts = db.get_posts(uid)
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


@dp.message(AdminStates.waiting_for_group_table, F.text == BTN_SYNC_FROM_SHEET)
async def process_google_sheet_sync(message: Message, state: FSMContext):
    uid = message.from_user.id
    settings = get_user_settings(uid)
    sheet_url = settings.get("sheet_url") or ""

    if not sheet_url:
        await message.answer(
            "❌ У вас ще не підключена Google Таблиця. Натисніть «🔗 Підключити Google Таблицю» в головному меню.",
            reply_markup=get_cancel_keyboard(),
        )
        return

    await message.answer("☁️ Завантажую свіжі дані з вашої Google Таблиці...")
    try:
        price_database = await asyncio.to_thread(fetch_google_sheet_prices, sheet_url)
    except Exception as e:
        await message.answer(
            f"❌ Помилка завантаження Google Таблиці: {esc(e)}\n"
            "Ви можете перевірити доступ до таблиці або надіслати файл <code>.xlsx</code> вручну.",
            reply_markup=get_sync_source_keyboard(True),
        )
        return

    if not price_database:
        await message.answer(
            "❌ У Google Таблиці не вдалося розпізнати штрихкоди та ціни. Перевірте таблицю або надішліть <code>.xlsx</code>.",
            reply_markup=get_sync_source_keyboard(True),
        )
        return

    await _prepare_sync_confirmation(message, state, uid, price_database)


@dp.message(
    AdminStates.waiting_for_group_table,
    F.document | (F.text & ~F.text.in_(MENU_TEXTS))
)
async def process_table(message: Message, state: FSMContext):
    uid = message.from_user.id
    settings = get_user_settings(uid)
    has_sheet = bool(settings.get("sheet_url"))
    price_database = {}

    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith(".xlsx"):
            await message.answer(
                "❌ Підтримується лише формат <code>.xlsx</code> (або надішліть текст таблиці).",
                reply_markup=get_sync_source_keyboard(has_sheet),
            )
            return
        try:
            file_info = await bot.get_file(message.document.file_id)
            buf = await bot.download_file(file_info.file_path)
            price_database = await asyncio.to_thread(parse_excel, buf.read())
        except Exception as e:
            await message.answer(
                f"❌ Помилка читання файлу: {esc(e)}",
                reply_markup=get_sync_source_keyboard(has_sheet),
            )
            return
    elif message.text:
        price_database = parse_text_table(message.text)

    if not price_database:
        await message.answer(
            "❌ Не вдалося розпізнати штрихкоди та ціни. Перевірте таблицю й спробуйте ще раз.",
            reply_markup=get_sync_source_keyboard(has_sheet),
        )
        return

    await _prepare_sync_confirmation(message, state, uid, price_database)


@dp.message(AdminStates.waiting_for_sync_confirm, F.text == BTN_SYNC_CONFIRM)
async def confirm_sync(message: Message, state: FSMContext):
    uid_int = message.from_user.id
    data = await state.get_data()
    price_database = data.get("prices") or {}
    await state.clear()

    if not price_database:
        await message.answer("❌ Дані таблиці втрачено. Почніть спочатку.", reply_markup=get_main_keyboard())
        return

    await message.answer("🔄 Оновлюю ціни та синхронізую базу з 1С...")

    flat_posts = db.get_posts(uid_int)
    new_user_db = {}
    removed = 0
    sent, failed = 0, 0

    for code, post in flat_posts.items():
        if code in price_database:
            new_user_db[code] = post
            caption = update_price_and_clean_caption(post["caption"], price_database[code])
            msg_id = await send_post(uid_int, post["photo"], caption)
            if msg_id is not None:
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.3)
        else:
            removed += 1

    not_found = [c for c in price_database if c not in flat_posts]

    db.replace_posts(uid_int, new_user_db)

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
    s = get_user_settings(message.from_user.id)
    db.set_settings(message.from_user.id, message.text.strip(), s["discount"])
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
    s = get_user_settings(message.from_user.id)
    db.set_settings(message.from_user.id, s["promo"], val)
    await state.clear()
    await message.answer(f"✅ Знижку змінено на: {val:g}% для вашого аккаунта!", reply_markup=get_main_keyboard())


async def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("PORT"):
        threading.Thread(target=run_dummy_web_server, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущено з PostgreSQL...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
