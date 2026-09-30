import asyncio
import os
import re
import pymysql
from pymysql.cursors import DictCursor
from datetime import datetime, date, timedelta
from urllib.parse import urlparse, urlencode
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import hashlib
import json

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter, Forbidden, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ============================================================
# НАСТРОЙКИ
# ============================================================

# После отзыва старого токена в BotFather положи новый токен в Termux:
# export BOT_TOKEN="НОВЫЙ_ТОКЕН"
TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {8731772900, 6541647643}
DB_HOST = os.getenv("DB_HOST", "").strip()
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "").strip()
DB_USER = os.getenv("DB_USER", "root").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "").strip()
DEFAULT_REMINDER_TIME = "18:00"
REMINDER_CHECK_INTERVAL = 20
ARCOTEL_GROUP_ID = "57001"
ARCOTEL_GROUP_NAME = "ОИБ-61"
ARCOTEL_BASE_URL = "https://www.arcotel.ru/studentam/raspisanie-i-grafiki/raspisanie-zanyatiy-studentov-ochnoy-i-vecherney-form-obucheniya"
ARCOTEL_POLL_SECONDS = 10
ARCOTEL_WEEKS_AHEAD = 13
ARCOTEL_FUTURE_REFRESH_SECONDS = 300
ARCOTEL_TIMEOUT = 15

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class MySQLCompatConnection:
    """Небольшая совместимая обёртка: остальной код бота может использовать conn.execute()."""
    def __init__(self):
        self.raw = pymysql.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
            database=DB_NAME, charset="utf8mb4", cursorclass=DictCursor,
            autocommit=False, connect_timeout=10, read_timeout=30, write_timeout=30
        )

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        cur = self.raw.cursor()
        cur.execute(sql, params or ())
        return cur

    def cursor(self):
        return self.raw.cursor(DictCursor)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def db():
    if not DB_HOST or not DB_NAME or not DB_PASSWORD:
        raise RuntimeError("Не настроена MySQL: нужны DB_HOST, DB_NAME и DB_PASSWORD в переменных окружения FadeHost.")
    return MySQLCompatConnection()


def init_db():
    conn = db()
    cur = conn.cursor()
    tables = [
        """CREATE TABLE IF NOT EXISTS users(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            telegram_id BIGINT NOT NULL UNIQUE,
            full_name VARCHAR(255) NOT NULL,
            created_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS settings(
            user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            homework_reminders TINYINT NOT NULL DEFAULT 1,
            new_homework_notifications TINYINT NOT NULL DEFAULT 1,
            debt_reminders TINYINT NOT NULL DEFAULT 1,
            CONSTRAINT fk_settings_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS homework(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            homework_date VARCHAR(10) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            text TEXT NOT NULL,
            attachment_type VARCHAR(32),
            attachment TEXT,
            created_at VARCHAR(32) NOT NULL,
            INDEX idx_homework_date(homework_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS debts(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            subject VARCHAR(255) NOT NULL,
            text TEXT NOT NULL,
            attachment_type VARCHAR(32),
            attachment TEXT,
            created_at VARCHAR(32) NOT NULL,
            INDEX idx_debts_user(user_id),
            CONSTRAINT fk_debts_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS schedule_days(
            weekday INT PRIMARY KEY,
            no_school TINYINT NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS schedule(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            weekday INT NOT NULL,
            pair_number INT NOT NULL,
            subject VARCHAR(255) NOT NULL,
            teacher VARCHAR(255) NOT NULL,
            start_time VARCHAR(5) NOT NULL,
            end_time VARCHAR(5) NOT NULL,
            UNIQUE KEY uq_schedule_weekday_pair(weekday,pair_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS attendance(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            attendance_date VARCHAR(10) NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            pair_number INT NULL,
            status VARCHAR(20) NOT NULL,
            comment TEXT NULL,
            updated_at VARCHAR(32) NOT NULL,
            INDEX idx_att_date_user(attendance_date,user_id),
            INDEX idx_att_date_pair(attendance_date,pair_number),
            UNIQUE KEY uq_attendance(attendance_date,user_id,pair_number),
            CONSTRAINT fk_attendance_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS schedule_entries(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            lesson_date VARCHAR(10) NOT NULL,
            pair_number INT NOT NULL,
            subject VARCHAR(255) NOT NULL,
            teacher VARCHAR(255) NOT NULL DEFAULT '',
            room VARCHAR(255) NOT NULL DEFAULT '',
            start_time VARCHAR(5) NOT NULL DEFAULT '',
            end_time VARCHAR(5) NOT NULL DEFAULT '',
            lessons_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_schedule_entry(lesson_date,pair_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS schedule_sync(
            lesson_date VARCHAR(10) PRIMARY KEY,
            content_hash VARCHAR(64) NOT NULL,
            last_success VARCHAR(32) NOT NULL,
            no_school TINYINT NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS manual_schedule_days(
            lesson_date VARCHAR(10) PRIMARY KEY,
            no_school TINYINT NOT NULL DEFAULT 0,
            updated_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS manual_schedule_entries(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            lesson_date VARCHAR(10) NOT NULL,
            pair_number INT NOT NULL,
            subject VARCHAR(255) NOT NULL,
            teacher VARCHAR(255) NOT NULL DEFAULT '',
            room VARCHAR(255) NOT NULL DEFAULT '',
            start_time VARCHAR(5) NOT NULL DEFAULT '',
            end_time VARCHAR(5) NOT NULL DEFAULT '',
            lesson_type VARCHAR(255) NOT NULL DEFAULT '',
            lessons_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_manual_entry(lesson_date,pair_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS broadcasts(
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            text TEXT,
            attachment_type VARCHAR(32),
            attachment TEXT,
            created_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS global_settings(
            `key` VARCHAR(100) PRIMARY KEY,
            value TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ]
    try:
        for sql in tables:
            cur.execute(sql)
        for i in range(7):
            cur.execute("INSERT IGNORE INTO schedule_days(weekday,no_school) VALUES(%s,0)", (i,))
        cur.execute("INSERT IGNORE INTO global_settings(`key`,value) VALUES('reminder_time',%s)", (DEFAULT_REMINDER_TIME,))
        cur.execute("INSERT IGNORE INTO settings(user_id) SELECT id FROM users")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_user(tg_id):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
    if user:
        conn.execute("INSERT IGNORE INTO settings(user_id) VALUES(?)", (user["id"],))
        conn.commit()
    conn.close()
    return user


def create_user(tg_id, full_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT IGNORE INTO users(telegram_id,full_name,created_at) VALUES(?,?,?)",
                (tg_id, full_name, now_iso()))
    user = cur.execute("SELECT id FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
    cur.execute("INSERT IGNORE INTO settings(user_id) VALUES(?)", (user["id"],))
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute("""INSERT INTO global_settings(`key`,value) VALUES(?,?)
                    ON DUPLICATE KEY UPDATE value=VALUES(value)""", (key, value))
    conn.commit()
    conn.close()


def today_str():
    return date.today().isoformat()


def parse_date(value):
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    return None


def format_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return value


def valid_time(value):
    try:
        datetime.strptime(value.strip(), "%H:%M")
        return True
    except ValueError:
        return False


def valid_url(value):
    try:
        p = urlparse(value.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def clean_name(value):
    return " ".join(value.split())


def main_keyboard(tg_id):
    rows = [
        ["📚 Домашнее задание"],
        ["📖 Уроки"],
        ["👥 Посещаемость"],
        ["⚙️ Настройки"],
    ]
    if is_admin(tg_id):
        rows.append(["🔐 Админ-панель"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def simple_keyboard(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def split_text(text, limit=3900):
    """Безопасно делит длинный текст для Telegram, стараясь резать по строкам."""
    text=str(text or '')
    while len(text) > limit:
        cut=text.rfind("\n", 0, limit)
        if cut < 1: cut=limit
        yield text[:cut]
        text=text[cut:].lstrip("\n")
    if text:
        yield text


async def main_menu(update, text="🏠 Главное меню"):
    await update.message.reply_text(text, reply_markup=main_keyboard(update.effective_user.id))


# ============================================================
# РЕГИСТРАЦИЯ
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = get_user(update.effective_user.id)
    if not user:
        context.user_data["state"] = "registration"
        await update.message.reply_text("👋 Добро пожаловать!\n\nДля начала напишите ваше ФИО.")
        return
    await update.message.reply_text(
        f"👋 С возвращением, {user['full_name']}!\n\n🏠 Главное меню",
        reply_markup=main_keyboard(update.effective_user.id),
    )


# ============================================================
# ДЗ
# ============================================================

def homework_for_date(date_str):
    conn = db()
    rows = conn.execute("SELECT * FROM homework WHERE homework_date=? ORDER BY id", (date_str,)).fetchall()
    conn.close()
    return rows


async def homework_menu(update, context):
    context.user_data.clear()
    context.user_data["screen"] = "homework"
    rows = [["📅 Сегодня", "📆 Завтра"], ["🗓 По дате"], ["⚠️ Долги"], ["🗄 Архив"]]
    if is_admin(update.effective_user.id):
        rows.append(["🔐 Управление ДЗ"])
    rows.append(["🔙 Назад"])
    await update.message.reply_text("📚 Домашнее задание", reply_markup=simple_keyboard(rows))


async def send_homework(update, date_str, title):
    rows = homework_for_date(date_str)
    if not rows:
        await update.message.reply_text(f"{title}\n\n✅ Домашнего задания нет.")
        return
    text = title + "\n\n"
    for i, row in enumerate(rows, 1):
        text += f"{i}️⃣ {row['subject']}\n📝 {row['text']}\n"
        if row["attachment_type"] == "link":
            text += f"🔗 {row['attachment']}\n"
        text += "\n"
    await update.message.reply_text(text)
    for row in rows:
        if row["attachment_type"] == "photo":
            try:
                await update.message.reply_photo(row["attachment"], caption=f"📚 {row['subject']}")
            except Exception:
                pass
        elif row["attachment_type"] == "document":
            try:
                await update.message.reply_document(row["attachment"], caption=f"📚 {row['subject']}")
            except Exception:
                pass


async def homework_by_date_start(update, context):
    context.user_data["state"] = "homework_date"
    await update.message.reply_text("🗓 Введите дату в формате ДД.ММ.ГГГГ")


async def archive_menu(update, context):
    conn = db()
    rows = conn.execute("SELECT DISTINCT homework_date FROM homework WHERE homework_date<? ORDER BY homework_date DESC",
                        (today_str(),)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🗄 Архив пуст.")
        return
    buttons = [[InlineKeyboardButton(format_date(r["homework_date"]), callback_data=f"archive:{r['homework_date']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="archive_back")])
    await update.message.reply_text("🗄 Архив\n\nВыберите дату:", reply_markup=InlineKeyboardMarkup(buttons))


async def homework_admin_menu(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📚 Управление ДЗ", reply_markup=simple_keyboard([
        ["➕ Добавить ДЗ"], ["✏️ Изменить ДЗ"], ["🗑 Удалить ДЗ"], ["🔙 Назад"]
    ]))


async def add_homework_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data.update(state="hw_date")
    await update.message.reply_text("➕ Добавление ДЗ\n\n1️⃣ Введите дату:\nДД.ММ.ГГГГ")


async def save_homework(update, context, attachment_type=None, attachment=None):
    d = context.user_data["hw_date"]
    subject = context.user_data["hw_subject"]
    hw_text = context.user_data["hw_text"]
    conn = db()
    cur = conn.execute("""INSERT INTO homework(homework_date,subject,text,attachment_type,attachment,created_at)
                          VALUES(?,?,?,?,?,?)""",
                       (d, subject, hw_text, attachment_type, attachment, now_iso()))
    hw_id = cur.lastrowid
    conn.commit()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(f"✅ ДЗ добавлено!\n\n📅 {format_date(d)}\n📚 {subject}")
    await notify_new_homework(context, d, subject, hw_id)
    await update.message.reply_text("Что дальше?", reply_markup=simple_keyboard([
        ["➕ Добавить ещё ДЗ"], ["📚 Управление ДЗ"], ["🏠 Главное меню"]
    ]))


async def process_add_homework(update, context):
    state = context.user_data.get("state")
    text = update.message.text.strip()
    if state == "hw_date":
        d = parse_date(text)
        if not d:
            await update.message.reply_text("❌ Неверная дата. Например: 30.09.2026")
            return True
        context.user_data.update(hw_date=d.isoformat(), state="hw_subject")
        await update.message.reply_text("2️⃣ Введите название предмета:")
        return True
    if state == "hw_subject":
        if not text:
            await update.message.reply_text("❌ Название предмета не может быть пустым.")
            return True
        context.user_data.update(hw_subject=text, state="hw_text")
        await update.message.reply_text("3️⃣ Введите текст домашнего задания:")
        return True
    if state == "hw_text":
        if not text:
            await update.message.reply_text("❌ Текст ДЗ не может быть пустым.")
            return True
        context.user_data.update(hw_text=text, state="hw_attachment")
        await update.message.reply_text("4️⃣ Отправьте фото, файл или ссылку.\n\nЕсли вложения нет — напишите «нет».")
        return True
    if state == "hw_attachment":
        if text.lower() in {"нет", "нету", "без", "-"}:
            await save_homework(update, context)
            return True
        if valid_url(text):
            await save_homework(update, context, "link", text)
            return True
        await update.message.reply_text("❌ Отправьте фото, файл, ссылку или напишите «нет».")
        return True
    return False


async def edit_homework_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["state"] = "edit_hw_date"
    await update.message.reply_text("✏️ Введите дату ДЗ:\nДД.ММ.ГГГГ")


async def delete_homework_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["state"] = "delete_hw_date"
    await update.message.reply_text("🗑 Введите дату ДЗ:\nДД.ММ.ГГГГ")


async def hw_picker(update, date_str, action):
    rows = homework_for_date(date_str)
    if not rows:
        await update.message.reply_text("❌ На эту дату ДЗ нет.")
        return
    prefix = "edithw" if action == "edit" else "delhw"
    buttons = [[InlineKeyboardButton(f"{r['subject']}: {r['text'][:35]}", callback_data=f"{prefix}:{r['id']}")] for r in rows]
    await update.message.reply_text("✏️ Выберите ДЗ:" if action == "edit" else "🗑 Выберите ДЗ:",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def process_edit_hw_value(update, context):
    if context.user_data.get("state") != "edit_hw_value":
        return False
    value = update.message.text.strip()
    hw_id = context.user_data.get("edit_hw_id")
    field = context.user_data.get("edit_field")
    if not hw_id or field not in {"date", "subject", "text"}:
        context.user_data.clear()
        await update.message.reply_text("❌ Сессия редактирования устарела.")
        return True
    if field == "date":
        d = parse_date(value)
        if not d:
            await update.message.reply_text("❌ Неверная дата. Используйте ДД.ММ.ГГГГ.")
            return True
        value = d.isoformat()
    elif not value:
        await update.message.reply_text("❌ Значение не может быть пустым.")
        return True
    conn = db()
    conn.execute(f"UPDATE homework SET homework_date=? WHERE id=?" if field == "date" else f"UPDATE homework SET {field}=? WHERE id=?",
                 (value, hw_id))
    conn.commit()
    row = conn.execute("SELECT * FROM homework WHERE id=?", (hw_id,)).fetchone()
    conn.close()
    context.user_data.clear()
    await update.message.reply_text(f"✅ ДЗ изменено.\n\n📅 {format_date(row['homework_date'])}\n📚 {row['subject']}\n📝 {row['text']}")
    return True


# ============================================================
# ДОЛГИ
# ============================================================

async def debts_menu(update, context):
    user = get_user(update.effective_user.id)
    conn = db()
    rows = conn.execute("SELECT * FROM debts WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("⚠️ Мои долги\n\n✅ У вас нет долгов.")
        return
    text = "⚠️ Мои долги\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}️⃣ {r['subject']}\n📝 {r['text']}\n\n"
    await update.message.reply_text(text)
    for r in rows:
        if r["attachment_type"] == "photo":
            try: await update.message.reply_photo(r["attachment"], caption=f"⚠️ {r['subject']}")
            except Exception: pass
        elif r["attachment_type"] == "document":
            try: await update.message.reply_document(r["attachment"], caption=f"⚠️ {r['subject']}")
            except Exception: pass
        elif r["attachment_type"] == "link":
            await update.message.reply_text(f"🔗 {r['attachment']}")


async def debt_admin_menu(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⚠️ Управление долгами", reply_markup=simple_keyboard([
        ["➕ Добавить долг"], ["🗑 Удалить долг"], ["🔙 Назад"]
    ]))


async def find_users_by_name(search):
    conn = db()
    rows = conn.execute("SELECT * FROM users WHERE LOWER(full_name) LIKE LOWER(?) ORDER BY full_name", (f"%{search}%",)).fetchall()
    conn.close()
    return rows


async def add_debt_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data.clear()
    context.user_data["state"] = "debt_user"
    await update.message.reply_text("➕ Добавление долга\n\nВведите фамилию или ФИО ученика:")


async def delete_debt_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data.clear()
    context.user_data["state"] = "delete_debt_user"
    await update.message.reply_text("🗑 Удаление долга\n\nВведите фамилию или ФИО ученика:")


async def process_debt_state(update, context):
    state = context.user_data.get("state")
    text = update.message.text.strip()
    if state == "debt_user":
        users = await find_users_by_name(text)
        if not users:
            await update.message.reply_text("❌ Пользователь не найден. Попробуйте другую фамилию.")
            return True
        buttons = [[InlineKeyboardButton(u["full_name"], callback_data=f"debtuser:{u['id']}")] for u in users]
        await update.message.reply_text("👤 Выберите пользователя:", reply_markup=InlineKeyboardMarkup(buttons))
        return True
    if state == "debt_subject":
        context.user_data.update(debt_subject=text, state="debt_text")
        await update.message.reply_text("📝 Что должен ученик?")
        return True
    if state == "debt_text":
        context.user_data.update(debt_text=text, state="debt_attachment")
        await update.message.reply_text("📎 Отправьте фото/файл.\n\nЕсли не нужно — напишите «нет».")
        return True
    if state == "debt_attachment":
        if text.lower() in {"нет", "нету", "без", "-"}:
            await save_debt(update, context)
            return True
        await update.message.reply_text("❌ Отправьте фото/файл или напишите «нет».")
        return True
    if state == "delete_debt_user":
        users = await find_users_by_name(text)
        if not users:
            await update.message.reply_text("❌ Пользователь не найден.")
            return True
        buttons = [[InlineKeyboardButton(u["full_name"], callback_data=f"deldebtuser:{u['id']}")] for u in users]
        await update.message.reply_text("👤 Выберите пользователя:", reply_markup=InlineKeyboardMarkup(buttons))
        context.user_data["state"] = None
        return True
    return False


async def save_debt(update, context, attachment_type=None, attachment=None):
    uid = context.user_data["debt_user_id"]
    conn = db()
    conn.execute("""INSERT INTO debts(user_id,subject,text,attachment_type,attachment,created_at)
                    VALUES(?,?,?,?,?,?)""",
                 (uid, context.user_data["debt_subject"], context.user_data["debt_text"], attachment_type, attachment, now_iso()))
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.commit(); conn.close()
    context.user_data.clear()
    await update.message.reply_text(f"✅ Долг добавлен.\n\n👤 {user['full_name']}")


# ============================================================
# РАСПИСАНИЕ
# ============================================================

async def lessons_menu(update, context):
    context.user_data.clear()
    context.user_data["screen"] = "lessons"
    rows = [["📅 Сегодня", "📆 Завтра"], ["🗓 По дате"]]
    if is_admin(update.effective_user.id): rows.append(["🔐 Управление расписанием"])
    rows.append(["🔙 Назад"])
    await update.message.reply_text("📖 Уроки", reply_markup=simple_keyboard(rows))


def week_monday(target_date):
    return target_date - timedelta(days=target_date.weekday())


def schedule_for_date(target_date):
    date_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    conn = db()
    manual = conn.execute("SELECT * FROM manual_schedule_days WHERE lesson_date=?", (date_str,)).fetchone()
    if manual:
        rows = conn.execute("""SELECT id, lesson_date, pair_number, subject, teacher, room,
                                     start_time, end_time, lessons_json
                              FROM manual_schedule_entries
                              WHERE lesson_date=? ORDER BY pair_number, id""", (date_str,)).fetchall()
        meta = {"lesson_date": date_str, "no_school": int(manual["no_school"]), "manual": 1,
                "last_success": manual["updated_at"], "content_hash": "manual"}
        conn.close()
        return meta, rows
    meta = conn.execute("SELECT * FROM schedule_sync WHERE lesson_date=?", (date_str,)).fetchone()
    rows = conn.execute("SELECT * FROM schedule_entries WHERE lesson_date=? ORDER BY pair_number", (date_str,)).fetchall()
    conn.close()
    return meta, rows


def manual_day_exists(date_str):
    conn = db(); row = conn.execute("SELECT 1 FROM manual_schedule_days WHERE lesson_date=?", (date_str,)).fetchone(); conn.close(); return bool(row)


def set_manual_day(date_str, no_school=0):
    conn=db()
    conn.execute("INSERT INTO manual_schedule_days(lesson_date,no_school,updated_at) VALUES(?,?,?) ON DUPLICATE KEY UPDATE no_school=VALUES(no_school),updated_at=VALUES(updated_at)", (date_str, int(no_school), now_iso()))
    conn.commit(); conn.close()


def manual_rows(date_str):
    conn=db(); rows=conn.execute("SELECT * FROM manual_schedule_entries WHERE lesson_date=? ORDER BY pair_number,id", (date_str,)).fetchall(); conn.close(); return rows


def manual_upsert_entry(date_str, pair_number, subject, teacher, room, start_time, end_time, lesson_type=''):
    variants=[{"subject":subject,"teacher":teacher,"room":room,"lesson_type":lesson_type}]
    conn=db()
    conn.execute("""INSERT INTO manual_schedule_entries
        (lesson_date,pair_number,subject,teacher,room,start_time,end_time,lesson_type,lessons_json)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE
        subject=VALUES(subject),teacher=VALUES(teacher),room=VALUES(room),
        start_time=VALUES(start_time),end_time=VALUES(end_time),
        lesson_type=VALUES(lesson_type),lessons_json=VALUES(lessons_json)""",
        (date_str,pair_number,subject,teacher,room,start_time,end_time,lesson_type,json.dumps(variants,ensure_ascii=False)))
    conn.execute("INSERT INTO manual_schedule_days(lesson_date,no_school,updated_at) VALUES(?,0,?) ON DUPLICATE KEY UPDATE no_school=0,updated_at=VALUES(updated_at)", (date_str,now_iso()))
    conn.commit(); conn.close()


def manual_delete_entry(date_str, entry_id):
    conn=db(); conn.execute("DELETE FROM manual_schedule_entries WHERE id=? AND lesson_date=?", (entry_id,date_str)); conn.execute("UPDATE manual_schedule_days SET updated_at=? WHERE lesson_date=?", (now_iso(),date_str)); conn.commit(); conn.close()


def manual_clear_day(date_str):
    conn=db(); conn.execute("DELETE FROM manual_schedule_entries WHERE lesson_date=?",(date_str,)); conn.execute("INSERT INTO manual_schedule_days(lesson_date,no_school,updated_at) VALUES(?,1,?) ON DUPLICATE KEY UPDATE no_school=1,updated_at=VALUES(updated_at)",(date_str,now_iso())); conn.commit(); conn.close()


def manual_restore_arcotel(date_str):
    conn=db(); conn.execute("DELETE FROM manual_schedule_entries WHERE lesson_date=?",(date_str,)); conn.execute("DELETE FROM manual_schedule_days WHERE lesson_date=?",(date_str,)); conn.commit(); conn.close()


def row_variants(row):
    try:
        variants=json.loads(row['lessons_json'] or '[]')
        if variants:
            return variants
    except Exception:
        pass
    return [{
        'subject': row['subject'], 'teacher': row['teacher'], 'room': row['room'], 'lesson_type': ''
    }]


def schedule_for_weekday(weekday):
    # Совместимость со старой схемой: если где-то ещё вызывается этот
    # метод, возвращаем старое недельное расписание.
    conn = db()
    day = conn.execute("SELECT * FROM schedule_days WHERE weekday=?", (weekday,)).fetchone()
    rows = conn.execute("SELECT * FROM schedule WHERE weekday=? ORDER BY pair_number", (weekday,)).fetchall()
    conn.close()
    return day, rows


class ArcotelParser(HTMLParser):
    """Оставлен для совместимости со старыми версиями файла."""
    pass


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '').replace('\xa0', ' ')).strip()


def _class_block(html, class_name, start_pos=0):
    """Возвращает следующий div с нужным классом и его содержимое."""
    pat = re.compile(r'<div\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\b' + re.escape(class_name) + r'\b[^\"\']*[\"\'][^>]*>', re.I)
    m = pat.search(html, start_pos)
    if not m:
        return None, None, None
    pos = m.end()
    depth = 1
    tag_re = re.compile(r'<(/?)div\b[^>]*>', re.I)
    for tm in tag_re.finditer(html, pos):
        if tm.group(1):
            depth -= 1
            if depth == 0:
                return m, html[pos:tm.start()], tm.end()
        else:
            depth += 1
    return None, None, None


def _extract_inner_text(html_fragment):
    text = re.sub(r'<br\s*/?>', '\n', html_fragment, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    return _norm(re.sub(r'\s*\n\s*', ' ', text))


def _extract_field(block, class_name):
    m, inner, _ = _class_block(block, class_name)
    if m:
        return _extract_inner_text(inner)
    # Некоторые версии Arcotel используют span вместо div для преподавателя.
    pat=re.compile(r'<(div|span)\b[^>]*\bclass\s*=\s*["\'][^"\']*\b'+re.escape(class_name)+r'\b[^"\']*["\'][^>]*>(.*?)</\1>',re.I|re.S)
    m=pat.search(block)
    if not m:
        return ''
    return _extract_inner_text(m.group(2))


def _extract_variants(day_block):
    variants=[]
    pos=0
    while True:
        m, inner, endpos = _class_block(day_block, 'vt258', pos)
        if not m:
            break
        subject=_extract_field(inner, 'vt240')
        if not subject or subject.strip() == '-':
            pos=endpos
            continue
        teacher=_extract_field(inner, 'vt241')
        room=_extract_field(inner, 'vt242')
        lesson_type=_extract_field(inner, 'vt243')
        variants.append({
            'subject': subject,
            'teacher': teacher,
            'room': room,
            'lesson_type': lesson_type,
        })
        pos=endpos
    return variants


def _extract_date_headers(html, requested_monday):
    """Устойчиво извлекает даты из vt237 независимо от порядка атрибутов."""
    result={}
    pat=re.compile(
        r'<div\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bvt237\b[^"\']*["\'])'
        r'(?=[^>]*\bdata-i\s*=\s*["\'](\d+)["\'])[^>]*>'
        r'\s*(\d{1,2})\.(\d{1,2})', re.I|re.S)
    for m in pat.finditer(html):
        idx=int(m.group(1))
        if not 1 <= idx <= 6: continue
        day,month=int(m.group(2)),int(m.group(3))
        candidates=[]
        for year in (requested_monday.year-1, requested_monday.year, requested_monday.year+1):
            try: candidates.append(date(year,month,day))
            except ValueError: pass
        if not candidates: continue
        d=min(candidates,key=lambda x:abs((x-requested_monday).days))
        if requested_monday <= d <= requested_monday+timedelta(days=6): result[idx]=d
    return result


def _extract_day_blocks(html):
    """Возвращает все rasp-dayN блоки верхнего уровня строки пары."""
    result=[]
    pat=re.compile(r'<div\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\bras[p]?[-]?day\b[^\"\']*[\"\'][^>]*>',re.I)
    # Реальная разметка: class='vt239 rasp-day rasp-day1'. Ищем именно rasp-dayN.
    pat=re.compile(r'<div\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\brasp-day(\d+)\b[^\"\']*[\"\'][^>]*>',re.I)
    for m in pat.finditer(html):
        n=int(m.group(1))
        if not 1 <= n <= 6:
            continue
        pos=m.end(); depth=1
        tag_re=re.compile(r'<(/?)div\b[^>]*>',re.I)
        for tm in tag_re.finditer(html,pos):
            if tm.group(1):
                depth-=1
                if depth==0:
                    result.append((n,m.start(),html[pos:tm.start()]))
                    break
            else:
                depth+=1
    return result


def _extract_pair_rows(html):
    """Разбирает реальные vt244 строки Arcotel."""
    rows=[]
    pos=0
    while True:
        m, inner, endpos=_class_block(html,'vt244',pos)
        if not m:
            break
        time_match=re.search(r'(?<!\d)(\d{1,2}:\d{2})\s*<br\s*/?>\s*(\d{1,2}:\d{2})',inner,re.I)
        if not time_match:
            # HTML может содержать пробелы/другую форму br.
            txt=re.sub(r'<br\s*/?>','\n',inner,flags=re.I)
            tm=re.search(r'(\d{1,2}:\d{2}).*?(\d{1,2}:\d{2})',txt,re.S)
            if not tm:
                pos=endpos; continue
            start_time,end_time=tm.group(1),tm.group(2)
        else:
            start_time,end_time=time_match.group(1),time_match.group(2)
        num_m=re.search(r'<div\b[^>]*\bclass\s*=\s*[\"\'][^\"\']*\bvt283\b[^\"\']*[\"\'][^>]*>(.*?)</div>',inner,re.I|re.S)
        raw_num=_extract_inner_text(num_m.group(1)) if num_m else '-'
        pair=int(raw_num) if raw_num.isdigit() else 0
        for day_idx,_,day_inner in _extract_day_blocks(inner):
            variants=_extract_variants(day_inner)
            rows.append((day_idx,pair,start_time,end_time,variants))
        pos=endpos
    return rows


def parse_arcotel_html(html, requested_monday):
    """Парсер именно текущей разметки Arcotel: vt237/vt244/rasp-dayN/vt258."""
    if not html or 'vt237' not in html or 'vt244' not in html or 'rasp-day1' not in html:
        return []
    headers=_extract_date_headers(html,requested_monday)
    if len(headers) < 6:
        return []
    result={}
    for day_idx,pair,start_time,end_time,variants in _extract_pair_rows(html):
        d=headers.get(day_idx)
        if not d or not variants:
            continue
        key=(d.isoformat(),pair)
        # Если на странице несколько vt244 с одним номером — объединяем варианты.
        existing=result.get(key)
        if existing:
            existing['lessons'].extend(v for v in variants if v not in existing['lessons'])
        else:
            result[key]={
                'lesson_date':d.isoformat(),
                'pair_number':pair,
                'subject':variants[0]['subject'],
                'teacher':variants[0].get('teacher',''),
                'room':variants[0].get('room',''),
                'start_time':start_time,
                'end_time':end_time,
                'lessons':variants,
            }
    return list(result.values())

def _hash_schedule(rows):
    normalized=[]
    for r in rows:
        normalized.append({
            'lesson_date':r.get('lesson_date',''), 'pair_number':r.get('pair_number',0),
            'start_time':r.get('start_time',''), 'end_time':r.get('end_time',''),
            'lessons':r.get('lessons',[]),
        })
    raw=json.dumps(sorted(normalized,key=lambda x:(x['lesson_date'],x['pair_number'],x['start_time'])),ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def arcotel_url(monday):
    return ARCOTEL_BASE_URL + '?' + urlencode({'group':ARCOTEL_GROUP_ID,'date':monday.isoformat()})


def fetch_url_sync(url):
    import urllib.request
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Android; TelegramClassBot) Accept-Language: ru-RU,ru;q=0.9'})
    with urllib.request.urlopen(req,timeout=ARCOTEL_TIMEOUT) as r:
        data=r.read()
        charset='utf-8'
        ct=r.headers.get_content_charset()
        if ct: charset=ct
        return data.decode(charset,errors='replace')


def replace_week_schedule(monday, rows):
    dates={(monday+timedelta(days=i)).isoformat() for i in range(7)}
    conn=db(); cur=conn.cursor()
    try:
        cur.execute('BEGIN')
        manual_dates={r[0] for r in cur.execute("SELECT lesson_date FROM manual_schedule_days WHERE lesson_date IN (%s)" % ",".join("?"*len(dates)), tuple(sorted(dates))).fetchall()} if dates else set()
        for ds in dates:
            if ds in manual_dates:
                continue
            cur.execute('DELETE FROM schedule_entries WHERE lesson_date=?',(ds,))
            cur.execute('DELETE FROM schedule_sync WHERE lesson_date=?',(ds,))
        grouped={d:[] for d in dates}
        for r in rows:
            grouped.setdefault(r['lesson_date'],[]).append(r)
        for ds in sorted(dates):
            if ds in manual_dates:
                continue
            dayrows=grouped.get(ds,[])
            h=_hash_schedule(dayrows)
            for r in dayrows:
                variants=r.get('lessons') or [{'subject':r.get('subject',''),'teacher':r.get('teacher',''),'room':r.get('room',''),'lesson_type':''}]
                first=variants[0]
                cur.execute('''INSERT INTO schedule_entries(lesson_date,pair_number,subject,teacher,room,start_time,end_time,lessons_json)
                               VALUES(?,?,?,?,?,?,?,?)''',
                            (r['lesson_date'],r['pair_number'],first.get('subject',''),first.get('teacher',''),first.get('room',''),r.get('start_time',''),r.get('end_time',''),json.dumps(variants,ensure_ascii=False)))
            cur.execute('INSERT INTO schedule_sync(lesson_date,content_hash,last_success,no_school) VALUES(?,?,?,?)',
                        (ds,h,now_iso(),1 if not dayrows else 0))

            # Если пользователь указал общий статус до загрузки расписания,
            # переносим его на реальные пары после появления расписания.
            pending = cur.execute(
                'SELECT * FROM attendance WHERE attendance_date=? AND pair_number=-1', (ds,)
            ).fetchall()
            for mark in pending:
                for lesson in dayrows:
                    existing = cur.execute(
                        'SELECT id FROM attendance WHERE attendance_date=? AND user_id=? AND pair_number=?',
                        (ds, mark['user_id'], lesson['pair_number'])
                    ).fetchone()
                    if existing:
                        cur.execute(
                            'UPDATE attendance SET status=?,comment=?,updated_at=? WHERE id=?',
                            (mark['status'], mark['comment'], now_iso(), existing['id'])
                        )
                    else:
                        cur.execute(
                            'INSERT INTO attendance(attendance_date,user_id,pair_number,status,comment,updated_at) VALUES(?,?,?,?,?,?)',
                            (ds, mark['user_id'], lesson['pair_number'], mark['status'], mark['comment'], now_iso())
                        )
                cur.execute('DELETE FROM attendance WHERE id=?', (mark['id'],))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()

def update_week_if_changed(monday, rows):
    dates={(monday+timedelta(days=i)).isoformat() for i in range(7)}
    conn=db(); old={}
    for ds in dates:
        r=conn.execute('SELECT content_hash FROM schedule_sync WHERE lesson_date=?',(ds,)).fetchone()
        old[ds]=r['content_hash'] if r else None
    conn.close()
    new={ds:_hash_schedule([r for r in rows if r['lesson_date']==ds]) for ds in dates}
    if all(old.get(ds)==new[ds] for ds in dates):
        return False
    replace_week_schedule(monday,rows)
    return True


def arcotel_page_is_valid(html):
    return bool(html and 'vt237' in html and 'vt244' in html and len(_extract_date_headers(html, date.today()-timedelta(days=date.today().weekday()))) >= 6)

async def sync_arcotel_once(include_future=True):
    today=date.today()
    monday=week_monday(today)
    changed=False; loaded=[]
    weeks=ARCOTEL_WEEKS_AHEAD if include_future else 0
    for w in range(weeks+1):
        wm=monday+timedelta(days=7*w)
        try:
            html=await asyncio.to_thread(fetch_url_sync,arcotel_url(wm))
            rows=parse_arcotel_html(html,wm)
            # Обновляем БД только если страница действительно является расписанием Arcotel.
            # Пустая, но валидная неделя означает «занятий нет», а не ошибку парсера.
            if 'vt237' in html and 'vt244' in html and len(_extract_date_headers(html,wm)) >= 6:
                if update_week_if_changed(wm,rows):
                    changed=True
                loaded.append((wm,len(rows)))
            else:
                print(f'[Arcotel] {wm}: страница не похожа на таблицу расписания — старые данные сохранены')
        except Exception as e:
            print(f'[Arcotel] {wm}: {type(e).__name__}: {e}')
    if loaded:
        set_setting('arcotel_last_success',now_iso())
        set_setting('arcotel_last_loaded',','.join(f'{d.isoformat()}={n}' for d,n in loaded))
    return changed


async def arcotel_sync_loop():
    last_future=0.0
    loop=asyncio.get_running_loop()
    while True:
        try:
            # Текущая неделя проверяется каждые 10 секунд.
            await sync_arcotel_once(include_future=False)
            now=loop.time()
            # Будущие 6 месяцев обновляем периодически, не создавая десятки HTTP-запросов каждые 10 секунд.
            if now-last_future >= ARCOTEL_FUTURE_REFRESH_SECONDS:
                await sync_arcotel_once(include_future=True)
                last_future=loop.time()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f'[Arcotel] sync error: {type(e).__name__}: {e}')
        await asyncio.sleep(ARCOTEL_POLL_SECONDS)

async def show_schedule(update, target_date):
    meta, rows = schedule_for_date(target_date)
    title=f"📖 Расписание на {target_date.strftime('%d.%m.%Y')}"
    if meta and meta['no_school']:
        await update.message.reply_text(title+'\n\n💤 Сегодня не учимся.')
        return
    if not meta:
        await update.message.reply_text(title+'\n\n📅 Расписание на эту дату ещё не готово.')
        return
    if not rows:
        await update.message.reply_text(title+'\n\n💤 Сегодня не учимся.')
        return
    text=title+'\n\n'
    for r in rows:
        label='⏰' if int(r['pair_number'])==0 else f"{r['pair_number']}️⃣"
        text+=f"{label} {r['start_time']}–{r['end_time']}\n"
        for v in row_variants(r):
            text+=f"📚 {v.get('subject','')}\n"
            if v.get('teacher'): text+=f"👨‍🏫 {v['teacher']}\n"
            if v.get('room'): text+=f"🚪 {v['room']}\n"
            if v.get('lesson_type'): text+=f"📝 {v['lesson_type']}\n"
        text+='\n'
    last=get_setting('arcotel_last_success')
    if last: text+=f"🔄 Обновлено: {last.replace('T',' ')}"
    for chunk in split_text(text):
        await update.message.reply_text(chunk)

async def schedule_admin_menu(update, context):
    if not is_admin(update.effective_user.id): return
    last=get_setting('arcotel_last_success','ещё не было')
    loaded=get_setting('arcotel_last_loaded','нет данных')
    await update.message.reply_text(
        f"🔐 Управление расписанием\n\n👥 Группа: {ARCOTEL_GROUP_NAME}\n📦 Горизонт Arcotel: 3 месяца\n⏱ Проверка текущей недели: каждые {ARCOTEL_POLL_SECONDS} сек.\n\nПоследняя успешная синхронизация: {last}\n\n✏️ Ручное расписание имеет приоритет над Arcotel и не будет перезаписано.",
        reply_markup=simple_keyboard([["📅 Выбрать дату"],["🔄 Синхронизировать сейчас"],["🔙 Назад"]]))


async def manual_day_menu(update, context, date_str, edit=False):
    if not is_admin(update.effective_user.id): return
    try: dt=datetime.strptime(date_str,'%Y-%m-%d').date()
    except ValueError: await update.message.reply_text('❌ Неверная дата.'); return
    meta, rows=schedule_for_date(dt)
    text=f"🛠 Расписание на {dt.strftime('%d.%m.%Y')}\n\n"
    if rows:
        for r in rows:
            label='⏰' if int(r['pair_number'])==0 else f"{r['pair_number']}️⃣"
            text += f"{label} {r['start_time']}–{r['end_time']} — {r['subject']}\n"
    else:
        text += '💤 Пар нет.\n'
    text += '\n' + ('🔒 Режим: ручной' if meta and meta.get('manual') else '🌐 Источник: Arcotel')
    buttons=[
        [InlineKeyboardButton('➕ Добавить пару',callback_data=f'madd:{date_str}')],
        [InlineKeyboardButton('✏️ Изменить пару',callback_data=f'medit:{date_str}')],
        [InlineKeyboardButton('🗑 Удалить пару',callback_data=f'mdel:{date_str}')],
        [InlineKeyboardButton('💤 Сделать день без занятий',callback_data=f'mclear:{date_str}')],
        [InlineKeyboardButton('🔄 Вернуть Arcotel',callback_data=f'mrestore:{date_str}')],
        [InlineKeyboardButton('🔙 Назад',callback_data='msched:back')]
    ]
    if edit: await update.callback_query.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons))
    else: await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons))


def manual_entries_for_date(date_str):
    return manual_rows(date_str)

# ============================================================
# ПОСЕЩАЕМОСТЬ
# ============================================================

async def attendance_menu(update, context):
    context.user_data.clear(); context.user_data['screen']='attendance'
    rows=[["📅 Сегодня","📆 Завтра"],["📋 Моя посещаемость"],["🗓 По дате"]]
    if is_admin(update.effective_user.id): rows.append(["📊 Статистика"])
    rows.append(["🔙 Назад"])
    await update.message.reply_text("👥 Посещаемость",reply_markup=simple_keyboard(rows))


def allowed_attendance_date(target_date):
    return target_date in {date.today(), date.today()+timedelta(days=1)}


def save_attendance(date_str,user_id,pair,status,comment=None):
    conn=db()
    q=conn.execute("SELECT id FROM attendance WHERE attendance_date=? AND user_id=? AND pair_number=?",(date_str,user_id,pair)).fetchone()
    if q:
        conn.execute("UPDATE attendance SET status=?,comment=?,updated_at=? WHERE id=?",(status,comment,now_iso(),q['id']))
    else:
        conn.execute("INSERT INTO attendance(attendance_date,user_id,pair_number,status,comment,updated_at) VALUES(?,?,?,?,?,?)",(date_str,user_id,pair,status,comment,now_iso()))
    conn.commit(); conn.close()


def set_all_pairs_attendance(date_str,user_id,status,comment):
    try: target=datetime.strptime(date_str,'%Y-%m-%d').date()
    except ValueError: return 0
    _,lessons=schedule_for_date(target)
    count=0
    for lesson in lessons:
        save_attendance(date_str,user_id,lesson['pair_number'],status,comment)
        count+=1
    return count


def get_user_attendance(date_str,user_id):
    conn=db(); rows=conn.execute("SELECT * FROM attendance WHERE attendance_date=? AND user_id=? AND pair_number IS NOT NULL ORDER BY pair_number",(date_str,user_id)).fetchall(); conn.close(); return rows


async def attendance_for_date(update,context,target_date):
    if not allowed_attendance_date(target_date):
        await update.message.reply_text("📅 Посещаемость можно указывать только на сегодня или завтра.")
        return
    meta,lessons=schedule_for_date(target_date)
    ds=target_date.isoformat()
    if not meta:
        # Даже если расписание завтра ещё не загрузилось, разрешаем общий статус.
        buttons=[[InlineKeyboardButton("🙋 Буду — весь день",callback_data=f"attall:{ds}:present")],
                 [InlineKeyboardButton("❌ Не буду — весь день",callback_data=f"attall:{ds}:absent")],
                 [InlineKeyboardButton("🔙 Назад",callback_data="attmenu")]]
        await update.message.reply_text(f"👥 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n📅 Расписание на эту дату ещё не готово.\n\nМожно пока указать только общий статус на весь день.",reply_markup=InlineKeyboardMarkup(buttons))
        return
    if meta and meta['no_school']:
        await update.message.reply_text(f"👥 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n💤 Учёбы нет."); return
    if not lessons:
        await update.message.reply_text(f"👥 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n💤 Сегодня не учимся."); return
    user=get_user(update.effective_user.id); marks={r['pair_number']:r for r in get_user_attendance(ds,user['id'])}
    text=f"👥 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n"
    for l in lessons:
        label='🙋 Буду' if marks.get(l['pair_number']) and marks[l['pair_number']]['status']=='present' else '❌ Не буду' if marks.get(l['pair_number']) else '❔ Не указано'
        text+=f"{'⏰' if l['pair_number']==0 else str(l['pair_number'])+'️⃣'} {l['subject']} — {label}"
        if marks.get(l['pair_number']) and marks[l['pair_number']]['comment']: text+=f" — {marks[l['pair_number']]['comment']}"
        text+='\n'
    buttons=[[InlineKeyboardButton("🙋 Буду — весь день",callback_data=f"attall:{ds}:present")],
             [InlineKeyboardButton("❌ Не буду — весь день",callback_data=f"attall:{ds}:absent")],
             [InlineKeyboardButton("⏰ Не буду на определённых парах",callback_data=f"attselect:{ds}")],
             [InlineKeyboardButton("🔄 Изменить",callback_data=f"attselect:{ds}")]]
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons))


async def attendance_select_pairs(query,date_str):
    try: target=datetime.strptime(date_str,'%Y-%m-%d').date()
    except ValueError: return
    if not allowed_attendance_date(target):
        await query.answer("Можно отмечать только сегодня или завтра.",show_alert=True); return
    meta,lessons=schedule_for_date(target)
    if not meta or not lessons:
        await query.answer("Расписание на эту дату ещё не готово.",show_alert=True); return
    user=get_user(query.from_user.id); marks={r['pair_number'] for r in get_user_attendance(date_str,user['id']) if r['status']=='absent'}
    selected=query.message and None
    buttons=[]
    chosen=context_selected=None
    # selected is stored in user_data by callback_handler; it is reflected there before this call.
    # The visual state is read from callback context indirectly via query._bot, so callback_handler passes no extra state.
    for l in lessons:
        mark='☑️' if l['pair_number'] in marks else '☐'
        label='⏰' if l['pair_number']==0 else f"{l['pair_number']}️⃣"
        buttons.append([InlineKeyboardButton(f"{mark} {label} {l['subject']}",callback_data=f"attpick:{date_str}:{l['pair_number']}")])
    buttons.append([InlineKeyboardButton("✅ Готово",callback_data=f"attdone:{date_str}")])
    buttons.append([InlineKeyboardButton("🔙 Назад",callback_data=f"attbacknew:{date_str}")])
    await query.edit_message_text("⏰ Выбери пары, на которых тебя не будет:\n\n☑️ — уже выбрано. Нажимай на пары, чтобы изменить выбор.",reply_markup=InlineKeyboardMarkup(buttons))


async def my_attendance(update,target_date=None):
    user=get_user(update.effective_user.id); target_date=target_date or date.today()
    meta,lessons=schedule_for_date(target_date); marks={r['pair_number']:r for r in get_user_attendance(target_date.isoformat(),user['id'])}
    text=f"📋 Моя посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n"
    if not meta: text+='📅 Расписание на эту дату ещё не готово.'
    elif meta['no_school'] or not lessons: text+='💤 Сегодня не учимся.'
    else:
        for l in lessons:
            m=marks.get(l['pair_number']); label='🙋 Буду' if m and m['status']=='present' else '❌ Не буду' if m and m['status']=='absent' else '❔ Не указано'
            label_pair='⏰' if l['pair_number']==0 else f"{l['pair_number']}️⃣"
            text+=f"{label_pair} {l['subject']} — {label}"+(f" — {m['comment']}" if m and m['comment'] else '')+'\n'
    buttons=[[InlineKeyboardButton("📅 Сегодня",callback_data="myatt:today"),InlineKeyboardButton("📆 Завтра",callback_data="myatt:tomorrow")],
             [InlineKeyboardButton("🗓 По дате",callback_data="myatt:date")],
             [InlineKeyboardButton("🔙 Назад",callback_data="attmenu")]]
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons))


async def attendance_statistics(update,target_date):
    if not is_admin(update.effective_user.id): return
    meta,lessons=schedule_for_date(target_date)
    if not meta:
        await update.message.reply_text(f"📊 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n📅 Расписание на эту дату ещё не готово."); return
    if meta['no_school'] or not lessons:
        await update.message.reply_text(f"📊 {target_date.strftime('%d.%m.%Y')}\n\n💤 Сегодня не учимся."); return
    conn=db(); users=conn.execute("SELECT * FROM users ORDER BY full_name").fetchall(); rows=conn.execute("SELECT attendance.*,users.full_name FROM attendance JOIN users ON users.id=attendance.user_id WHERE attendance_date=? AND pair_number IS NOT NULL",(target_date.isoformat(),)).fetchall(); conn.close()
    text=f"📊 Посещаемость — {target_date.strftime('%d.%m.%Y')}\n\n"
    for lesson in lessons:
        pr=[r for r in rows if r['pair_number']==lesson['pair_number']]
        present=[r for r in pr if r['status']=='present']; absent=[r for r in pr if r['status']=='absent']; responded={r['user_id'] for r in pr}; missing=[u for u in users if u['id'] not in responded]
        label='⏰' if lesson['pair_number']==0 else f"{lesson['pair_number']}️⃣"
        text+=f"━━━━━━━━━━━━━━\n{label} {lesson['subject']}\n🙋 Будут: {len(present)}\n"
        text+=''.join(f"• {r['full_name']}\n" for r in present)
        text+=f"❌ Не будут: {len(absent)}\n"+''.join(f"• {r['full_name']} — {r['comment'] or 'причина не указана'}\n" for r in absent)
        text+=f"❔ Не указали: {len(missing)}\n"+''.join(f"• {u['full_name']}\n" for u in missing)+'\n'
    for chunk in split_text(text): await update.message.reply_text(chunk)


async def stats_menu(update, context, target_date=None):
    target_date=target_date or date.today()
    await attendance_statistics(update,target_date)

# ============================================================
# НАСТРОЙКИ
# ============================================================

async def settings_menu(update, context):
    user = get_user(update.effective_user.id)
    conn = db(); s = conn.execute("SELECT * FROM settings WHERE user_id=?", (user["id"],)).fetchone(); conn.close()
    await update.message.reply_text("⚙️ Настройки\n\n🟢 — включено\n🔴 — выключено\n\nВремя напоминания устанавливает администратор.",
                                    reply_markup=simple_keyboard([
                                        [f"🔔 Напоминание о ДЗ {'🟢' if s['homework_reminders'] else '🔴'}"],
                                        [f"📢 Новое ДЗ {'🟢' if s['new_homework_notifications'] else '🔴'}"],
                                        [f"⚠️ Напоминание о долгах {'🟢' if s['debt_reminders'] else '🔴'}"],
                                        ["🔙 Назад"],
                                    ]))


async def toggle_setting(update, field):
    user = get_user(update.effective_user.id)
    conn = db(); row = conn.execute(f"SELECT {field} FROM settings WHERE user_id=?", (user["id"],)).fetchone()
    value = 0 if row[field] else 1
    conn.execute(f"UPDATE settings SET {field}=? WHERE user_id=?", (value, user["id"]))
    conn.commit(); conn.close()
    await settings_menu(update, None)


# ============================================================
# АДМИН / РАССЫЛКА
# ============================================================

async def admin_menu(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("🔐 Админ-панель", reply_markup=simple_keyboard([
        ["📚 Управление ДЗ"], ["⚠️ Управление долгами"], ["📢 Отправить сообщение всем"],
        ["📖 Управление расписанием"], ["👥 Статистика посещаемости"], ["🔔 Настройки уведомлений"], ["🔙 Назад"]
    ]))


async def broadcast_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data.clear(); context.user_data["state"] = "broadcast_text"
    await update.message.reply_text("📢 Отправка сообщения всем\n\nВведите текст сообщения:")


async def send_broadcast(update, context, attachment_type=None, attachment=None):
    text = context.user_data.get("broadcast_text", "")
    conn = db()
    conn.execute("INSERT INTO broadcasts(text,attachment_type,attachment,created_at) VALUES(?,?,?,?)", (text, attachment_type, attachment, now_iso()))
    users = conn.execute("SELECT telegram_id FROM users").fetchall()
    conn.commit(); conn.close()
    success = failed = 0
    for u in users:
        try:
            if attachment_type == "photo":
                await context.bot.send_photo(u["telegram_id"], attachment, caption=text[:1024] if text else None)
            elif attachment_type == "document":
                await context.bot.send_document(u["telegram_id"], attachment, caption=text[:1024] if text else None)
            else:
                for chunk in split_text(text):
                    await context.bot.send_message(u["telegram_id"], chunk)
            success += 1
            await asyncio.sleep(0.04)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.send_message(u["telegram_id"], text[:3900])
                success += 1
            except Exception:
                failed += 1
        except (Forbidden, Exception):
            failed += 1
    context.user_data.clear()
    await update.message.reply_text(f"📢 Рассылка завершена.\n\n✅ Отправлено: {success}\n❌ Не доставлено: {failed}")


# ============================================================
# УВЕДОМЛЕНИЯ
# ============================================================

async def notify_new_homework(context, date_str, subject, hw_id):
    conn = db()
    users = conn.execute("""SELECT users.telegram_id FROM users JOIN settings ON settings.user_id=users.id
                            WHERE settings.new_homework_notifications=1""").fetchall()
    conn.close()
    for u in users:
        try:
            await context.bot.send_message(u["telegram_id"], f"📢 Новое домашнее задание!\n\n📅 {format_date(date_str)}\n📚 {subject}")
        except Exception:
            pass


async def reminder_loop(app):
    last_sent = None
    while True:
        try:
            now = datetime.now()
            current = now.strftime("%H:%M")
            target = get_setting("reminder_time", DEFAULT_REMINDER_TIME)
            stamp = now.strftime("%Y-%m-%d %H:%M")
            if current == target and stamp != last_sent:
                last_sent = stamp
                conn = db()
                hw = conn.execute("SELECT * FROM homework WHERE homework_date=? ORDER BY id", (today_str(),)).fetchall()
                debt_users = conn.execute("""SELECT users.telegram_id FROM users JOIN settings ON settings.user_id=users.id
                                             WHERE settings.debt_reminders=1 AND EXISTS(SELECT 1 FROM debts WHERE debts.user_id=users.id)""").fetchall()
                hw_users = conn.execute("""SELECT users.telegram_id FROM users JOIN settings ON settings.user_id=users.id
                                          WHERE settings.homework_reminders=1""").fetchall()
                conn.close()
                if hw:
                    text = "🔔 Напоминание о ДЗ\n\n" + "".join(f"📚 {r['subject']}\n📝 {r['text']}\n\n" for r in hw)
                    for u in hw_users:
                        try: await app.bot.send_message(u["telegram_id"], text[:3900])
                        except Exception: pass
                if debt_users:
                    for u in debt_users:
                        try: await app.bot.send_message(u["telegram_id"], "⚠️ Напоминание о долгах\n\nУ вас есть незакрытые долги. Откройте раздел «⚠️ Долги».")
                        except Exception: pass
            await asyncio.sleep(REMINDER_CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(REMINDER_CHECK_INTERVAL)


async def post_init(app):
    app.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(app))
    app.bot_data["arcotel_task"] = asyncio.create_task(arcotel_sync_loop())


async def post_shutdown(app):
    for key in ("reminder_task", "arcotel_task"):
        task = app.bot_data.get(key)
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


async def admin_notification_settings(update, context):
    if not is_admin(update.effective_user.id): return
    t = get_setting("reminder_time", DEFAULT_REMINDER_TIME)
    await update.message.reply_text(f"🔔 Настройки уведомлений\n\n⏰ Время напоминания о ДЗ и долгах: {t}",
                                    reply_markup=simple_keyboard([["⏰ Изменить время"], ["🔙 Назад"]]))


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(update, context):
    q=update.callback_query; data=q.data or ''; user_id=q.from_user.id
    admin_prefixes=("edithw:","editfield:","delhw:","debtuser:","deldebtuser:","deldebt:")
    if data.startswith(admin_prefixes) and not is_admin(user_id):
        await q.answer("⛔ Недостаточно прав",show_alert=True); return
    await q.answer()

    if data.startswith('archive:'):
        d=data.split(':',1)[1]; rows=homework_for_date(d)
        text=f"🗄 Архив — {format_date(d)}\n\n"+''.join(f"{i}️⃣ {r['subject']}\n📝 {r['text']}\n\n" for i,r in enumerate(rows,1))
        await q.edit_message_text(text or "🗄 В этот день ДЗ нет."); return
    if data=='archive_back': await q.edit_message_text('🗄 Архив закрыт.'); return

    if data.startswith('edithw:'):
        hid=int(data.split(':')[1]); context.user_data.clear(); context.user_data['edit_hw_id']=hid
        buttons=[[InlineKeyboardButton('📅 Дата',callback_data=f'editfield:{hid}:date')],[InlineKeyboardButton('📚 Предмет',callback_data=f'editfield:{hid}:subject')],[InlineKeyboardButton('📝 Текст',callback_data=f'editfield:{hid}:text')],[InlineKeyboardButton('📎 Вложение',callback_data=f'editfield:{hid}:attachment')]]
        await q.edit_message_text('✏️ Что изменить?',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('editfield:'):
        _,hid,field=data.split(':')
        if field=='attachment':
            context.user_data.update(edit_hw_id=int(hid),edit_field=field,state='edit_hw_attachment'); await q.edit_message_text('📎 Отправьте новое фото/файл/ссылку или напишите «нет».'); return
        context.user_data.update(edit_hw_id=int(hid),edit_field=field,state='edit_hw_value')
        await q.edit_message_text({'date':'📅 Введите новую дату:','subject':'📚 Введите новый предмет:','text':'📝 Введите новый текст ДЗ:'}[field]); return
    if data.startswith('delhw:'):
        hid=int(data.split(':')[1]); conn=db(); conn.execute('DELETE FROM homework WHERE id=?',(hid,)); conn.commit(); conn.close(); await q.edit_message_text('✅ ДЗ удалено.'); return

    if data.startswith('debtuser:'):
        uid=int(data.split(':')[1]); context.user_data.update(debt_user_id=uid,state='debt_subject'); await q.edit_message_text('📚 Введите предмет:'); return
    if data.startswith('deldebtuser:'):
        uid=int(data.split(':')[1]); conn=db(); rows=conn.execute('SELECT * FROM debts WHERE user_id=? ORDER BY id',(uid,)).fetchall(); conn.close()
        if not rows: await q.edit_message_text('❌ У этого пользователя долгов нет.'); return
        buttons=[[InlineKeyboardButton(f"{r['subject']}: {r['text'][:35]}",callback_data=f"deldebt:{r['id']}")] for r in rows]
        await q.edit_message_text('🗑 Выберите долг:',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('deldebt:'):
        did=int(data.split(':')[1]); conn=db(); conn.execute('DELETE FROM debts WHERE id=?',(did,)); conn.commit(); conn.close(); await q.edit_message_text('✅ Долг удалён.'); return

    if data=='msched:back':
        await q.edit_message_text('🔐 Управление расписанием\n\nВыберите действие в меню бота.')
        return
    if data.startswith(('madd:','medit:','mdel:','mclear:','mrestore:')):
        if not is_admin(user_id):
            await q.answer('⛔ Недостаточно прав',show_alert=True); return
    if data.startswith('madd:'):
        d=data.split(':',1)[1]
        context.user_data.clear(); context.user_data.update(state='manual_pair_number',manual_date=d)
        await q.edit_message_text('➕ Добавление пары\n\n1️⃣ Введите номер пары. Для специального занятия можно ввести 0:')
        return
    if data.startswith('medit:'):
        d=data.split(':',1)[1]; rows=manual_entries_for_date(d)
        if not rows:
            await q.answer('На эту дату нет ручных пар. Сначала добавьте пару.',show_alert=True); return
        buttons=[[InlineKeyboardButton(f"{('⏰' if int(r['pair_number'])==0 else str(r['pair_number'])+'️⃣')} {r['subject']}",callback_data=f'meditone:{d}:{r["id"]}')] for r in rows]
        buttons.append([InlineKeyboardButton('🔙 Назад',callback_data=f'mday:{d}')])
        await q.edit_message_text('✏️ Выберите пару:',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('meditone:'):
        _,d,eid=data.split(':'); conn=db(); row=conn.execute('SELECT * FROM manual_schedule_entries WHERE id=? AND lesson_date=?',(int(eid),d)).fetchone(); conn.close()
        if not row: await q.answer('Пара не найдена.',show_alert=True); return
        buttons=[[InlineKeyboardButton('1️⃣ Номер пары',callback_data=f'mfield:{d}:{eid}:pair')],[InlineKeyboardButton('📚 Предмет',callback_data=f'mfield:{d}:{eid}:subject')],[InlineKeyboardButton('👨‍🏫 Преподаватель',callback_data=f'mfield:{d}:{eid}:teacher')],[InlineKeyboardButton('🚪 Кабинет',callback_data=f'mfield:{d}:{eid}:room')],[InlineKeyboardButton('🕐 Начало',callback_data=f'mfield:{d}:{eid}:start')],[InlineKeyboardButton('🕐 Окончание',callback_data=f'mfield:{d}:{eid}:end')],[InlineKeyboardButton('📝 Тип занятия',callback_data=f'mfield:{d}:{eid}:type')],[InlineKeyboardButton('🔙 Назад',callback_data=f'mday:{d}')]]
        await q.edit_message_text('✏️ Что изменить?',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('mfield:'):
        _,d,eid,field=data.split(':'); context.user_data.update(state='manual_edit_value',manual_date=d,manual_entry_id=int(eid),manual_field=field)
        prompts={'pair':'Введите новый номер пары (0 — специальное занятие):','subject':'Введите новый предмет:','teacher':'Введите преподавателя или «нет»:','room':'Введите кабинет или «нет»:','start':'Введите время начала, например 08:30:','end':'Введите время окончания, например 10:05:','type':'Введите тип занятия или «нет»:'}
        await q.edit_message_text('✏️ '+prompts[field]); return
    if data.startswith('mdel:'):
        d=data.split(':',1)[1]; rows=manual_entries_for_date(d)
        if not rows: await q.answer('На эту дату нет ручных пар.',show_alert=True); return
        buttons=[[InlineKeyboardButton(f"{('⏰' if int(r['pair_number'])==0 else str(r['pair_number'])+'️⃣')} {r['subject']}",callback_data=f'mdelone:{d}:{r["id"]}')] for r in rows]
        buttons.append([InlineKeyboardButton('🔙 Назад',callback_data=f'mday:{d}')])
        await q.edit_message_text('🗑 Выберите пару:',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('mdelone:'):
        _,d,eid=data.split(':'); manual_delete_entry(d,int(eid)); await q.edit_message_text('✅ Пара удалена.'); return
    if data.startswith('mclear:'):
        d=data.split(':',1)[1]; manual_clear_day(d); await q.edit_message_text(f'💤 {format_date(d)} теперь вручную отмечен как день без занятий.\n\nArcotel его не перезапишет.'); return
    if data.startswith('mrestore:'):
        d=data.split(':',1)[1]; manual_restore_arcotel(d); await q.edit_message_text(f'🔄 Ручное изменение для {format_date(d)} снято.\n\nТеперь снова используется Arcotel.'); return
    if data.startswith('mday:'):
        await manual_day_menu(update,context,data.split(':',1)[1],edit=True); return

    if data=='attmenu':
        context.user_data.clear(); context.user_data['screen']='attendance'
        await q.edit_message_text('👥 Посещаемость\n\nОткройте нужный пункт в клавиатуре бота.'); return
    if data.startswith('myatt:'):
        kind=data.split(':')[1]
        if kind=='today':
            # Callback messages cannot use update.message; build a small adapter through send_message.
            target=date.today()
        elif kind=='tomorrow': target=date.today()+timedelta(days=1)
        else:
            context.user_data['state']='my_attendance_date'; await q.edit_message_text('🗓 Введите дату в формате ДД.ММ.ГГГГ:'); return
        user=get_user(user_id); meta,lessons=schedule_for_date(target); marks={r['pair_number']:r for r in get_user_attendance(target.isoformat(),user['id'])}
        text=f"📋 Моя посещаемость — {target.strftime('%d.%m.%Y')}\n\n"
        if not meta: text+='📅 Расписание на эту дату ещё не готово.'
        elif meta['no_school'] or not lessons: text+='💤 Сегодня не учимся.'
        else:
            for l in lessons:
                m=marks.get(l['pair_number']); label='🙋 Буду' if m and m['status']=='present' else '❌ Не буду' if m and m['status']=='absent' else '❔ Не указано'; lp='⏰' if l['pair_number']==0 else f"{l['pair_number']}️⃣"; text+=f"{lp} {l['subject']} — {label}"+(f" — {m['comment']}" if m and m['comment'] else '')+'\n'
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📅 Сегодня',callback_data='myatt:today'),InlineKeyboardButton('📆 Завтра',callback_data='myatt:tomorrow')],[InlineKeyboardButton('🗓 По дате',callback_data='myatt:date')],[InlineKeyboardButton('🔙 Назад',callback_data='attmenu')]])); return

    if data.startswith('attstats:'):
        kind=data.split(':')[1]
        if kind=='today': target=date.today()
        elif kind=='tomorrow': target=date.today()+timedelta(days=1)
        else:
            context.user_data['state']='attendance_date'; await q.edit_message_text('📆 Введите дату для статистики:\nДД.ММ.ГГГГ'); return
        # Inline callback needs a message target; create text directly using helper data.
        if not is_admin(user_id): return
        meta,lessons=schedule_for_date(target)
        if not meta: await q.edit_message_text(f"📊 {target.strftime('%d.%m.%Y')}\n\n📅 Расписание ещё не готово."); return
        if meta['no_school'] or not lessons: await q.edit_message_text(f"📊 {target.strftime('%d.%m.%Y')}\n\n💤 Сегодня не учимся."); return
        conn=db(); users=conn.execute('SELECT * FROM users ORDER BY full_name').fetchall(); rows=conn.execute('SELECT attendance.*,users.full_name FROM attendance JOIN users ON users.id=attendance.user_id WHERE attendance_date=? AND pair_number IS NOT NULL',(target.isoformat(),)).fetchall(); conn.close()
        text=f"📊 Посещаемость — {target.strftime('%d.%m.%Y')}\n\n"
        for lesson in lessons:
            pr=[r for r in rows if r['pair_number']==lesson['pair_number']]; present=[r for r in pr if r['status']=='present']; absent=[r for r in pr if r['status']=='absent']; responded={r['user_id'] for r in pr}; missing=[u for u in users if u['id'] not in responded]; lp='⏰' if lesson['pair_number']==0 else f"{lesson['pair_number']}️⃣"
            text+=f"━━━━━━━━━━━━━━\n{lp} {lesson['subject']}\n🙋 Будут: {len(present)}\n"+''.join(f"• {r['full_name']}\n" for r in present)+f"❌ Не будут: {len(absent)}\n"+''.join(f"• {r['full_name']} — {r['comment'] or 'причина не указана'}\n" for r in absent)+f"❔ Не указали: {len(missing)}\n"+''.join(f"• {u['full_name']}\n" for u in missing)+'\n'
        # Telegram callback edit is limited to 4096; if too long, edit first chunk and send the rest.
        chunks=list(split_text(text))
        await q.edit_message_text(chunks[0],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📅 Сегодня',callback_data='attstats:today'),InlineKeyboardButton('📆 Завтра',callback_data='attstats:tomorrow')],[InlineKeyboardButton('🗓 По дате',callback_data='attstats:date')],[InlineKeyboardButton('🔙 Назад',callback_data='attmenu')]]))
        for chunk in chunks[1:]: await context.bot.send_message(user_id,chunk)
        return

    if data.startswith('attbacknew:'):
        d=data.split(':',1)[1]; dt=datetime.strptime(d,'%Y-%m-%d').date()
        # повторно показываем дату через callback-совместимую версию
        if not allowed_attendance_date(dt): await q.edit_message_text('📅 Эта дата больше недоступна для изменения.'); return
        user=get_user(user_id); meta,lessons=schedule_for_date(dt); marks={r['pair_number']:r for r in get_user_attendance(d,user['id'])}
        text=f"👥 Посещаемость — {dt.strftime('%d.%m.%Y')}\n\n"
        for l in lessons:
            m=marks.get(l['pair_number']); label='🙋 Буду' if m and m['status']=='present' else '❌ Не буду' if m and m['status']=='absent' else '❔ Не указано'; lp='⏰' if l['pair_number']==0 else f"{l['pair_number']}️⃣"; text+=f"{lp} {l['subject']} — {label}\n"
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🙋 Буду — весь день',callback_data=f'attall:{d}:present')],[InlineKeyboardButton('❌ Не буду — весь день',callback_data=f'attall:{d}:absent')],[InlineKeyboardButton('⏰ Не буду на определённых парах',callback_data=f'attselect:{d}')]])); return
    if data.startswith('attselect:'):
        d=data.split(':')[1]; target=datetime.strptime(d,'%Y-%m-%d').date()
        if not allowed_attendance_date(target): await q.answer('Можно отмечать только сегодня или завтра.',show_alert=True); return
        meta,lessons=schedule_for_date(target)
        if not meta or not lessons: await q.answer('Расписание на эту дату ещё не готово.',show_alert=True); return
        user=get_user(user_id); existing={r['pair_number'] for r in get_user_attendance(d,user['id']) if r['status']=='absent'}
        context.user_data.setdefault('attendance_selected',{})[d]=set(existing)
        await attendance_select_pairs(q,d); return
    if data.startswith('attpick:'):
        _,d,p=data.split(':'); p=int(p); target=datetime.strptime(d,'%Y-%m-%d').date()
        if not allowed_attendance_date(target): await q.answer('Можно отмечать только сегодня или завтра.',show_alert=True); return
        selected=context.user_data.setdefault('attendance_selected',{}); chosen=selected.setdefault(d,set())
        if p in chosen: chosen.remove(p)
        else: chosen.add(p)
        # Re-render with temporary selection, not only DB state.
        _,lessons=schedule_for_date(target); buttons=[]
        for l in lessons:
            mark='☑️' if l['pair_number'] in chosen else '☐'; lp='⏰' if l['pair_number']==0 else f"{l['pair_number']}️⃣"; buttons.append([InlineKeyboardButton(f"{mark} {lp} {l['subject']}",callback_data=f'attpick:{d}:{l["pair_number"]}')])
        buttons += [[InlineKeyboardButton('✅ Готово',callback_data=f'attdone:{d}')],[InlineKeyboardButton('🔙 Назад',callback_data=f'attbacknew:{d}')]]
        await q.edit_message_text('⏰ Выбери пары, на которых тебя не будет:',reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith('attdone:'):
        d=data.split(':',1)[1]; chosen=sorted(context.user_data.get('attendance_selected',{}).get(d,set()))
        if not chosen: await q.answer('Выберите хотя бы одну пару',show_alert=True); return
        context.user_data['attendance_pending_pairs']={'date':d,'pairs':chosen}; await q.edit_message_text('❌ Почему не будешь на выбранных парах?',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🤒 Болезнь',callback_data=f'attreason:{d}:ill')],[InlineKeyboardButton('✏️ Своя причина',callback_data=f'attreason:{d}:custom')],[InlineKeyboardButton('🔙 Назад',callback_data=f'attselect:{d}')]])); return
    if data.startswith('attreason:'):
        _,d,reason=data.split(':'); pairs=context.user_data.get('attendance_pending_pairs',{}).get('pairs',[])
        if reason=='ill':
            user=get_user(user_id); set_all_pairs_attendance(d,user['id'],'present',None)
            for pnum in pairs: save_attendance(d,user['id'],pnum,'absent','Болезнь')
            context.user_data.get('attendance_selected',{}).pop(d,None); context.user_data.pop('attendance_pending_pairs',None); await q.edit_message_text('🤒 Посещаемость сохранена.'); return
        context.user_data['attendance_comment_mode']={'date':d,'pairs':pairs}; context.user_data['state']='attendance_custom_reason'; await q.edit_message_text('✏️ Напишите свою причину:'); return
    if data.startswith('attall:'):
        _,d,status=data.split(':'); target=datetime.strptime(d,'%Y-%m-%d').date()
        if not allowed_attendance_date(target): await q.answer('Можно отмечать только сегодня или завтра.',show_alert=True); return
        user=get_user(user_id)
        # Если расписание ещё не готово, общий статус запоминается через специальную пару -1.
        if not schedule_for_date(target)[0]:
            if status=='present': save_attendance(d,user['id'],-1,'present',None); await q.edit_message_text('🙋 Общий статус сохранён. Расписание ещё не готово, после загрузки он будет применён к парам.'); return
            context.user_data['attendance_all_pending']=d; context.user_data['attendance_all_status']='absent'; context.user_data['state']='attendance_all_reason'; await q.edit_message_text('❌ Почему не будешь?',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🤒 Болезнь',callback_data=f'attallreason:{d}:ill')],[InlineKeyboardButton('✏️ Своя причина',callback_data=f'attallreason:{d}:custom')]])); return
        if status=='present':
            set_all_pairs_attendance(d,user['id'],'present',None); await q.edit_message_text('🙋 Готово! Ты отмечен как «Буду» на все пары.'); return
        context.user_data['attendance_all_pending']=d; context.user_data['state']='attendance_all_reason'; await q.edit_message_text('❌ Почему не будешь?',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🤒 Болезнь',callback_data=f'attallreason:{d}:ill')],[InlineKeyboardButton('✏️ Своя причина',callback_data=f'attallreason:{d}:custom')],[InlineKeyboardButton('🔙 Назад',callback_data=f'attbacknew:{d}')]])); return
    if data.startswith('attallreason:'):
        _,d,reason=data.split(':'); user=get_user(user_id); target=datetime.strptime(d,'%Y-%m-%d').date()
        if reason=='ill':
            if schedule_for_date(target)[0]: set_all_pairs_attendance(d,user['id'],'absent','Болезнь')
            else: save_attendance(d,user['id'],-1,'absent','Болезнь')
            context.user_data.pop('attendance_all_pending',None); context.user_data.pop('state',None); await q.edit_message_text('🤒 Готово! Ты отмечен как отсутствующий на весь день.'); return
        context.user_data['attendance_comment_mode']={'date':d,'all':True}; context.user_data['state']='attendance_custom_reason'; await q.edit_message_text('✏️ Напишите свою причину:'); return
    if data=='schedule_sync_now' and is_admin(user_id):
        changed=await sync_arcotel_once(include_future=True); await q.edit_message_text('🔄 Синхронизация завершена. '+('Расписание обновлено.' if changed else 'Изменений нет.')); return

# ============================================================
# МЕДИА / СОСТОЯНИЯ
# ============================================================

async def process_edit_hw_attachment(update, context):
    if context.user_data.get("state") != "edit_hw_attachment": return False
    hid=context.user_data["edit_hw_id"]
    if update.message.photo:
        value_type="photo"; value=update.message.photo[-1].file_id
    elif update.message.document:
        value_type="document"; value=update.message.document.file_id
    else: return False
    conn=db(); conn.execute("UPDATE homework SET attachment_type=?,attachment=? WHERE id=?",(value_type,value,hid)); conn.commit(); conn.close(); context.user_data.clear()
    await update.message.reply_text("✅ Вложение ДЗ изменено."); return True


async def process_state(update, context):
    text=update.message.text.strip(); state=context.user_data.get('state'); uid=update.effective_user.id
    if state=='registration':
        name=clean_name(text)
        if len(name)<3: await update.message.reply_text('❌ Напишите полное ФИО.'); return True
        create_user(uid,name); context.user_data.clear(); await update.message.reply_text(f'✅ Регистрация завершена!\n\n👤 {name}',reply_markup=main_keyboard(uid)); return True
    if state in {'hw_date','hw_subject','hw_text','hw_attachment'}: return await process_add_homework(update,context)
    if state=='homework_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data.clear(); await send_homework(update,d.isoformat(),f'🗓 ДЗ на {d.strftime("%d.%m.%Y")}'); return True
    if state=='edit_hw_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data['state']=None; await hw_picker(update,d.isoformat(),'edit'); return True
    if state=='delete_hw_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data['state']=None; await hw_picker(update,d.isoformat(),'delete'); return True
    if state=='edit_hw_value': return await process_edit_hw_value(update,context)
    if state=='edit_hw_attachment':
        if text.lower() in {'нет','нету','без','-'}:
            hid=context.user_data['edit_hw_id']; conn=db(); conn.execute('UPDATE homework SET attachment_type=NULL,attachment=NULL WHERE id=?',(hid,)); conn.commit(); conn.close(); context.user_data.clear(); await update.message.reply_text('✅ Вложение удалено.'); return True
        if valid_url(text):
            hid=context.user_data['edit_hw_id']; conn=db(); conn.execute("UPDATE homework SET attachment_type='link',attachment=? WHERE id=?",(text,hid)); conn.commit(); conn.close(); context.user_data.clear(); await update.message.reply_text('✅ Ссылка сохранена.'); return True
        await update.message.reply_text('❌ Отправьте фото/файл/ссылку или «нет».'); return True
    if state in {'debt_user','debt_subject','debt_text','debt_attachment','delete_debt_user'}: return await process_debt_state(update,context)
    if state=='broadcast_text':
        if not text: await update.message.reply_text('❌ Текст не может быть пустым.'); return True
        context.user_data.update(broadcast_text=text,state='broadcast_attachment'); await update.message.reply_text('📎 Отправьте фото/файл. Если вложение не нужно — напишите «нет».'); return True
    if state=='broadcast_attachment':
        if text.lower() in {'нет','нету','без','-'}: await send_broadcast(update,context); return True
        if valid_url(text): context.user_data['broadcast_text']+='\n\n🔗 '+text; await send_broadcast(update,context); return True
        await update.message.reply_text('❌ Отправьте фото/файл/ссылку или «нет».'); return True
    if state=='manual_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата. Например: 01.10.2026'); return True
        context.user_data.clear()
        await manual_day_menu(update,context,d.isoformat())
        return True
    if state=='manual_pair_number':
        if not text.isdigit() or not 0 <= int(text) <= 20:
            await update.message.reply_text('❌ Введите номер от 0 до 20.'); return True
        context.user_data['manual_pair_number']=int(text); context.user_data['state']='manual_subject'
        await update.message.reply_text('2️⃣ Введите предмет:'); return True
    if state=='manual_subject':
        if not text: await update.message.reply_text('❌ Предмет не может быть пустым.'); return True
        context.user_data['manual_subject']=text; context.user_data['state']='manual_teacher'; await update.message.reply_text('3️⃣ Введите преподавателя или «нет»:'); return True
    if state=='manual_teacher':
        context.user_data['manual_teacher']='' if text.lower() in {'нет','-','без'} else text; context.user_data['state']='manual_room'; await update.message.reply_text('4️⃣ Введите кабинет или «нет»:'); return True
    if state=='manual_room':
        context.user_data['manual_room']='' if text.lower() in {'нет','-','без'} else text; context.user_data['state']='manual_start'; await update.message.reply_text('5️⃣ Введите время начала, например 08:30:'); return True
    if state=='manual_start':
        if not valid_time(text): await update.message.reply_text('❌ Неверное время. Например 08:30'); return True
        context.user_data['manual_start']=text; context.user_data['state']='manual_end'; await update.message.reply_text('6️⃣ Введите время окончания, например 10:05:'); return True
    if state=='manual_end':
        if not valid_time(text): await update.message.reply_text('❌ Неверное время. Например 10:05'); return True
        context.user_data['manual_end']=text; context.user_data['state']='manual_type'; await update.message.reply_text('7️⃣ Введите тип занятия или «нет»:'); return True
    if state=='manual_type':
        d=context.user_data['manual_date']; lesson_type='' if text.lower() in {'нет','-','без'} else text
        manual_upsert_entry(d,context.user_data['manual_pair_number'],context.user_data['manual_subject'],context.user_data['manual_teacher'],context.user_data['manual_room'],context.user_data['manual_start'],context.user_data['manual_end'],lesson_type)
        context.user_data.clear(); await update.message.reply_text('✅ Пара сохранена.\n\nОна имеет приоритет над Arcotel для этой даты.')
        return True
    if state=='manual_edit_value':
        d=context.user_data['manual_date']; eid=context.user_data['manual_entry_id']; field=context.user_data['manual_field']; value=text
        conn=db(); row=conn.execute('SELECT * FROM manual_schedule_entries WHERE id=? AND lesson_date=?',(eid,d)).fetchone()
        if not row: conn.close(); context.user_data.clear(); await update.message.reply_text('❌ Пара не найдена.'); return True
        if field=='pair':
            if not value.isdigit() or not 0<=int(value)<=20: conn.close(); await update.message.reply_text('❌ Номер пары: от 0 до 20.'); return True
            try: conn.execute('UPDATE manual_schedule_entries SET pair_number=? WHERE id=? AND lesson_date=?',(int(value),eid,d))
            except pymysql.err.IntegrityError: conn.close(); await update.message.reply_text('❌ Такая пара уже существует на этой дате.'); return True
        elif field in {'start','end'}:
            if not valid_time(value): conn.close(); await update.message.reply_text('❌ Неверное время.'); return True
            col={'start':'start_time','end':'end_time'}[field]; conn.execute(f'UPDATE manual_schedule_entries SET {col}=? WHERE id=? AND lesson_date=?',(value,eid,d))
        elif field=='type':
            conn.execute('UPDATE manual_schedule_entries SET lesson_type=? WHERE id=? AND lesson_date=?',('' if value.lower() in {'нет','-','без'} else value,eid,d))
        else:
            col={'subject':'subject','teacher':'teacher','room':'room'}[field]; conn.execute(f'UPDATE manual_schedule_entries SET {col}=? WHERE id=? AND lesson_date=?',('' if field!='subject' and value.lower() in {'нет','-','без'} else value,eid,d))
        row2=conn.execute('SELECT subject,teacher,room,lesson_type FROM manual_schedule_entries WHERE id=?',(eid,)).fetchone(); variants=[dict(row2)]
        conn.execute('UPDATE manual_schedule_entries SET lessons_json=? WHERE id=?', (json.dumps(variants,ensure_ascii=False),eid)); conn.execute('UPDATE manual_schedule_days SET updated_at=? WHERE lesson_date=?',(now_iso(),d)); conn.commit(); conn.close()
        context.user_data.clear(); await update.message.reply_text('✅ Изменение сохранено.'); return True
    if state=='attendance_all_reason':
        d=context.user_data.get('attendance_all_pending'); status=context.user_data.get('attendance_all_status','absent')
        if d:
            user=get_user(uid); target=datetime.strptime(d,'%Y-%m-%d').date(); meta,_=schedule_for_date(target)
            if meta: set_all_pairs_attendance(d,user['id'],status,text)
            else: save_attendance(d,user['id'],-1,status,text)
        context.user_data.pop('attendance_all_pending',None); context.user_data.pop('attendance_all_status',None); context.user_data.pop('state',None); await update.message.reply_text('✅ Причина сохранена.'); return True
    if state=='attendance_custom_reason':
        info=context.user_data.pop('attendance_comment_mode',None); context.user_data.pop('state',None)
        if info:
            user=get_user(uid)
            if info.get('all'):
                target=datetime.strptime(info['date'],'%Y-%m-%d').date(); meta,_=schedule_for_date(target)
                if meta: set_all_pairs_attendance(info['date'],user['id'],'absent',text)
                else: save_attendance(info['date'],user['id'],-1,'absent',text)
            else:
                set_all_pairs_attendance(info['date'],user['id'],'present',None)
                for pnum in info.get('pairs',[]): save_attendance(info['date'],user['id'],pnum,'absent',text)
            context.user_data.get('attendance_selected',{}).pop(info['date'],None)
        await update.message.reply_text('✅ Посещаемость и причина сохранены.'); return True
    if state=='attendance_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data.clear(); await attendance_statistics(update,d); return True
    if state=='attendance_user_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data.clear(); await attendance_for_date(update,context,d); return True
    if state=='my_attendance_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data.clear(); await my_attendance(update,d); return True
    if state=='lesson_date':
        d=parse_date(text)
        if not d: await update.message.reply_text('❌ Неверная дата.'); return True
        context.user_data.clear(); await show_schedule(update,d); return True
    if state=='admin_reminder_time':
        if not valid_time(text): await update.message.reply_text('❌ Неверное время. Например 18:30'); return True
        set_setting('reminder_time',text); context.user_data.clear(); await update.message.reply_text(f'✅ Время напоминания изменено на {text}.'); return True
    return False

async def media_handler(update, context):
    state=context.user_data.get("state")
    if state == "hw_attachment":
        if update.message.photo: await save_homework(update,context,"photo",update.message.photo[-1].file_id); return
        if update.message.document: await save_homework(update,context,"document",update.message.document.file_id); return
    if state == "debt_attachment":
        if update.message.photo: await save_debt(update,context,"photo",update.message.photo[-1].file_id); return
        if update.message.document: await save_debt(update,context,"document",update.message.document.file_id); return
    if state == "broadcast_attachment":
        if update.message.photo: await send_broadcast(update,context,"photo",update.message.photo[-1].file_id); return
        if update.message.document: await send_broadcast(update,context,"document",update.message.document.file_id); return
    if state == "edit_hw_attachment":
        if await process_edit_hw_attachment(update,context): return
    await update.message.reply_text("📎 Сейчас бот не ожидает файл или фото.")


# ============================================================
# ТЕКСТОВЫЙ ОБРАБОТЧИК
# ============================================================

async def text_handler(update, context):
    if not update.message or not update.message.text: return
    text=update.message.text.strip(); uid=update.effective_user.id

    # Навигационные кнопки имеют приоритет над любым активным состоянием.
    # Например, если бот ждёт причину пропуска, нажатие «📚 Домашнее задание»
    # не должно записываться как причина — оно должно открыть новое меню.
    if text=='📚 Домашнее задание': await homework_menu(update,context); return
    if text=='📖 Уроки': await lessons_menu(update,context); return
    if text=='👥 Посещаемость': await attendance_menu(update,context); return
    if text=='⚙️ Настройки': await settings_menu(update,context); return
    if text=='🔐 Админ-панель' and is_admin(uid): await admin_menu(update,context); return
    if text in {'🏠 Главное меню','🔙 Назад'}:
        context.user_data.clear(); await main_menu(update); return

    state=context.user_data.get('state')
    if state and await process_state(update,context): return

    screen=context.user_data.get('screen')
    if text=='📅 Сегодня':
        if screen=='homework': await send_homework(update,today_str(),'📅 ДЗ на сегодня')
        elif screen=='lessons': await show_schedule(update,date.today())
        elif screen=='attendance': await attendance_for_date(update,context,date.today())
        return
    if text=='📆 Завтра':
        if screen=='homework': await send_homework(update,(date.today()+timedelta(days=1)).isoformat(),'📆 ДЗ на завтра')
        elif screen=='lessons': await show_schedule(update,date.today()+timedelta(days=1))
        elif screen=='attendance': await attendance_for_date(update,context,date.today()+timedelta(days=1))
        return
    if text=='🗓 По дате':
        if screen=='homework': await homework_by_date_start(update,context)
        elif screen=='lessons': context.user_data['state']='lesson_date'; await update.message.reply_text('🗓 Введите дату:\nДД.ММ.ГГГГ')
        elif screen=='attendance': context.user_data['state']='attendance_date'; await update.message.reply_text('🗓 Для посещаемости доступны только сегодня и завтра. Напишите нужную дату:\nДД.ММ.ГГГГ')
        return
    if text=='📚 Домашнее задание': return
    if text=='⚠️ Долги': await debts_menu(update,context); return
    if text=='🗄 Архив': await archive_menu(update,context); return
    if text=='🔐 Управление ДЗ': await homework_admin_menu(update,context); return
    if text in {'➕ Добавить ДЗ','➕ Добавить ещё ДЗ'}: await add_homework_start(update,context); return
    if text=='✏️ Изменить ДЗ': await edit_homework_start(update,context); return
    if text=='🗑 Удалить ДЗ': await delete_homework_start(update,context); return
    if text=='🔐 Управление расписанием': await schedule_admin_menu(update,context); return
    if text=='📋 Моя посещаемость': await my_attendance(update); return
    if text in {'📊 Статистика','👥 Статистика посещаемости','🔐 Статистика'} and is_admin(uid):
        await stats_menu(update,context); return
    if text=='📅 Выбрать дату' and is_admin(uid):
        context.user_data['state']='manual_date'
        await update.message.reply_text('📅 Введите дату для ручного управления:\nДД.ММ.ГГГГ')
        return
    if text=='🔄 Синхронизировать сейчас' and is_admin(uid):
        changed=await sync_arcotel_once(include_future=True); await update.message.reply_text('🔄 Синхронизация завершена. '+('Расписание обновлено.' if changed else 'Изменений нет.')); return
    if text=='⚠️ Управление долгами': await debt_admin_menu(update,context); return
    if text=='➕ Добавить долг': await add_debt_start(update,context); return
    if text=='🗑 Удалить долг': await delete_debt_start(update,context); return
    if text=='📢 Отправить сообщение всем': await broadcast_start(update,context); return
    if text.startswith('🔔 Напоминание о ДЗ'): await toggle_setting(update,'homework_reminders'); return
    if text.startswith('📢 Новое ДЗ'): await toggle_setting(update,'new_homework_notifications'); return
    if text.startswith('⚠️ Напоминание о долгах'): await toggle_setting(update,'debt_reminders'); return
    if text=='🔔 Настройки уведомлений': await admin_notification_settings(update,context); return
    if text=='⏰ Изменить время' and is_admin(uid): context.user_data['state']='admin_reminder_time'; await update.message.reply_text('⏰ Введите новое время, например 18:30'); return
    await update.message.reply_text('❓ Я не понял эту команду.\n\nИспользуйте кнопки меню.',reply_markup=main_keyboard(uid))

async def error_handler(update, context):
    err=context.error
    if isinstance(err, RetryAfter):
        print(f"[Telegram] Flood limit: повтор через {err.retry_after} сек.")
        return
    if isinstance(err, NetworkError):
        print(f"[Telegram] Временная сетевая ошибка: {err}. Бот продолжит попытки.")
        return
    if isinstance(err, TelegramError):
        print(f"[Telegram] Ошибка API: {type(err).__name__}: {err}")
        return
    if isinstance(err, pymysql.MySQLError):
        print(f"[MySQL] Ошибка базы: {type(err).__name__}: {err}")
        return
    print(f"[BOT] Необработанная ошибка: {type(err).__name__}: {err}")


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь BOT_TOKEN в переменные окружения FadeHost.")
    init_db()
    app=(Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL,media_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_error_handler(error_handler)
    print("================================")
    print("🤖 БОТ ЗАПУЩЕН")
    print("📚 Система класса готова")
    print("================================")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
