import asyncio
import html
import http.server
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

import openpyxl
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

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")

MIN_CODE_LEN = 5
CAPTION_LIMIT = 1024
SUMMARY_DELAY = 1.5

DEFAULT_PROMO = "АКЦІЙНА ПРОПОЗИЦІЯ"
DEFAULT_DISCOUNT = 6.0

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def esc(text) -> str:
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Хранилище (SQLite, WAL)
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    user_id   INTEGER NOT NULL,
                    code      TEXT    NOT NULL,
                    photo     TEXT    NOT NULL,
                    caption   TEXT    NOT NULL DEFAULT '',
                    updated_at REAL   NOT NULL,
                    PRIMARY KEY (user_id, code)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    user_id INTEGER PRIMARY KEY,
                    promo   TEXT    NOT NULL,
                    discount REAL   NOT NULL
                );
            """)

    # -- посты --
    def upsert_post(self, user_id: int, code: str, photo: str, caption: str) -> bool:
        """Возвращает True, если пост уже существовал (обновление)."""
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT 1 FROM posts WHERE user_id=? AND code=?", (user_id, code)
            ).fetchone()
            self.conn.execute(
                "INSERT INTO posts(user_id, code, photo, caption, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id, code) DO UPDATE SET "
                "photo=excluded.photo, caption=excluded.caption, updated_at=excluded.updated_at",
                (user_id, code, photo, caption, time.time()),
            )
        return row is not None

    def count_posts(self, user_id: int) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM posts WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row["c"])

    def get_posts(self, user_id: int) -> dict:
        """dict: code -> {"photo": ..., "caption": ...}"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT code, photo, caption FROM posts WHERE user_id=?", (user_id,)
            ).fetchall()
        return {r["code"]: {"photo": r["photo"], "caption": r["caption"]} for r in rows}

    def replace_posts(self, user_id: int, posts: dict):
        """Полная замена набора постов пользователя (для синхронизации)."""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM posts WHERE user_id=?", (user_id,))
            self.conn.executemany(
                "INSERT INTO posts(user_id, code, photo, caption, updated_at) VALUES(?,?,?,?,?)",
                [
                    (user_id, code, post["photo"], post.get("caption", ""), time.time())
                    for code, post in posts.items()
                ],
            )

    def clear_posts(self, user_id: int):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM posts WHERE user_id=?", (user_id,))

    # -- настройки --
    def get_settings(self, user_id: int):
        with self.lock:
            row = self.conn.execute(
                "SELECT promo, discount FROM settings WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return {"promo": row["promo"], "discount": float(row["discount"])}

    def set_settings(self, user_id: int, promo: str, discount: float):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(user_id, promo, discount) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET promo=excluded.promo, discount=excluded.discount",
                (user_id, promo, discount),
            )

    def backup(self, dest_path: str):
        """Консистентная копия через SQLite backup API."""
        with self.lock:
            dst = sqlite3.connect(dest_path)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()


def load_json(filename, default_val):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения {filename}: {e}")
    return default_val


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


def migrate_from_json(db: Database):
    """Одноразовый перенос старых JSON-файлов в SQLite."""
    legacy_db = os.path.join(DATA_DIR, "group_posts_db.json")
    if os.path.exists(legacy_db):
        raw = load_json(legacy_db, {})
        users = raw.get("users", raw) if isinstance(raw, dict) else {}
        total = 0
        for uid, posts in users.items():
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            flat = flatten_db(posts)
            if flat:
                db.replace_posts(uid_int, flat)
                total += len(flat)
        os.replace(legacy_db, legacy_db + ".imported")
        logging.info(f"Перенесено {total} постов из group_posts_db.json в SQLite")

    legacy_settings = os.path.join(DATA_DIR, "user_settings.json")
    if os.path.exists(legacy_settings):
        users_settings = load_json(legacy_settings, {})
        for uid, s in users_settings.items():
            if not isinstance(s, dict):
                continue
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            db.set_settings(
                uid_int,
                str(s.get("promo", DEFAULT_PROMO)),
                float(s.get("discount", DEFAULT_DISCOUNT)),
            )
        os.replace(legacy_settings, legacy_settings + ".imported")
        logging.info("Перенесены настройки из user_settings.json в SQLite")


db = Database(DB_PATH)
migrate_from_json(db)

promo_batches: dict[int, list] = {}


def get_user_settings(user_id: int) -> dict:
    s = db.get_settings(user_id)
    if s is None:
        s = {"promo": DEFAULT_PROMO, "discount": DEFAULT_DISCOUNT}
        db.set_settings(user_id, s["promo"], s["discount"])
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
BTN_EDIT_CANCEL = "❌ Скасувати редагування"

MENU_TEXTS = {BTN_SYNC, BTN_PROMO, BTN_PROMO_TEXT, BTN_DISCOUNT, BTN_SETTINGS, BTN_STATS, BTN_CLEAR, BTN_BACK}

# Защита: в этих состояниях фоновый сбор фото заблокирован
NO_AUTOCOLLECT_STATES = {
    AdminStates.waiting_for_group_table.state,
    AdminStates.waiting_for_sync_confirm.state,
    AdminStates.waiting_for_promo_photos.state,
    AdminStates.waiting_for_clear_confirm.state,
    AdminStates.waiting_for_custom_promo.state,
    AdminStates.waiting_for_custom_discount.state,
    AdminStates.waiting_for_edit_caption.state,
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


def get_edit_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_EDIT_CANCEL)]],
        resize_keyboard=True,
    )


def get_edit_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Редагувати", callback_data="edit_caption")
        ]]
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


async def send_post(chat_id: int, photo: str, caption: str):
    for _ in range(3):
        try:
            if len(caption) <= CAPTION_LIMIT:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=get_edit_inline_keyboard(),
                )
            else:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    reply_markup=get_edit_inline_keyboard(),
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
# Разбор таблицы 1С
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


# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(me