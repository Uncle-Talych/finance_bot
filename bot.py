# bot.py
import re
import sqlite3
from datetime import datetime, date, time as dtime
import asyncio

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

TOKEN = "8020835184:AAGjIFp9AockvRSnHnnP5mRGDWoNK_M0U2w"

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()

DB_PATH = "finance_bot.db"

# ------------- database helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        timezone TEXT DEFAULT 'UTC',
        daily_time TEXT DEFAULT '20:00'
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        category TEXT,
        note TEXT,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)
    conn.commit()
    conn.close()

def get_or_create_user(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, daily_time FROM users WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if row:
        uid, daily_time = row
    else:
        cur.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        uid = cur.lastrowid
        daily_time = "20:00"
    conn.close()
    return uid, daily_time

def set_user_daily_time(chat_id, hhmm):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    cur.execute("UPDATE users SET daily_time = ? WHERE chat_id = ?", (hhmm, chat_id))
    conn.commit()
    conn.close()

def add_expense_for_user(chat_id, amount, category, note):
    user_id, _ = get_or_create_user(chat_id)
    created_at = datetime.now().isoformat()  # <-- локальное время вместо utc
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO expenses (user_id, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, category, note, created_at))
    conn.commit()
    conn.close()

def get_expenses_for_user_date(chat_id, target_date: date):
    user_id, _ = get_or_create_user(chat_id)
    start = datetime.combine(target_date, dtime.min).isoformat()
    end = datetime.combine(target_date, dtime.max).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT amount, category, note, created_at FROM expenses WHERE user_id = ? AND created_at BETWEEN ? AND ? ORDER BY created_at ASC",
                (user_id, start, end))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_summary_for_date(chat_id, target_date: date):
    rows = get_expenses_for_user_date(chat_id, target_date)
    total = sum(r[0] for r in rows)
    by_cat = {}
    for amount, category, note, created_at in rows:
        cat = category or "Без категории"
        by_cat[cat] = by_cat.get(cat, 0) + amount
    return total, by_cat, rows

# ------------- parsing helpers ----------------
expense_re = re.compile(r'^\s*(?:/add\s+)?([0-9]+(?:[.,][0-9]+)?)\s*(?:([^\s]+))?\s*(.*)$', re.IGNORECASE)

def parse_expense(text: str):
    m = expense_re.match(text.strip())
    if not m:
        return None
    amount_s = m.group(1).replace(',', '.')
    try:
        amount = float(amount_s)
    except:
        return None
    category = m.group(2) if m.group(2) else "Разное"
    note = m.group(3).strip() if m.group(3) else ""
    return amount, category, note

# ------------- bot handlers ----------------
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    get_or_create_user(message.chat.id)
    await message.answer("Привет! 👋 Я буду помогать считать расходы.\n"
                         "Добавить расход: `500 такси` или `/add 500 groceries хлеб`.\n"
                         "Команды: /summary /settime HH:MM", parse_mode="Markdown")

@dp.message_handler(commands=['settime'])
async def cmd_settime(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /settime HH:MM (например /settime 20:00)")
        return
    hhmm = parts[1].strip()
    if not re.match(r'^\d{1,2}:\d{2}$', hhmm):
        await message.answer("Неправильный формат. Пример: 20:00")
        return
    set_user_daily_time(message.chat.id, hhmm)
    await message.answer(f"Время ежедневного отчёта установлено на {hhmm} (локальное время).")
    schedule_user_daily_summary(message.chat.id, hhmm)

@dp.message_handler(commands=['summary'])
async def cmd_summary(message: types.Message):
    target = date.today()  # локальная дата
    total, by_cat, rows = get_summary_for_date(message.chat.id, target)
    if total == 0:
        await message.answer("За сегодня трат нет.")
        return
    text = f"Отчёт за {target.isoformat()}:\nВсего: {total:.2f}\n\nПо категориям:\n"
    for c, s in by_cat.items():
        text += f"- {c}: {s:.2f}\n"
    await message.answer(text)

@dp.message_handler()
async def handle_message(message: types.Message):
    parsed = parse_expense(message.text)
    if parsed:
        amount, category, note = parsed
        add_expense_for_user(message.chat.id, amount, category, note)
        await message.answer(f"Добавлено: {amount:.2f}  ({category}) {note}")
        return
    await message.answer("Не понял. Чтобы добавить расход, напишите `500 такси` или `/add 500 groceries хлеб`.\nКоманды: /summary /settime HH:MM", parse_mode="Markdown")

# ------------- scheduling daily summaries ----------------
def send_daily_summary(chat_id):
    asyncio.create_task(async_send_summary(chat_id))

async def async_send_summary(chat_id):
    target = date.today()
    total, by_cat, rows = get_summary_for_date(chat_id, target)
    if total == 0:
        await bot.send_message(chat_id, f"Отчёт за {target.isoformat()}: трат нет ✅")
        return
    text = f"Ежедневный отчёт за {target.isoformat()}:\nВсего: {total:.2f}\n\nПо категориям:\n"
    for c, s in by_cat.items():
        text += f"- {c}: {s:.2f}\n"
    await bot.send_message(chat_id, text)

def schedule_all_users():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id, daily_time FROM users")
    for chat_id, daily_time in cur.fetchall():
        schedule_user_daily_summary(chat_id, daily_time)
    conn.close()

def schedule_user_daily_summary(chat_id, daily_time_hhmm):
    job_id = f"daily_{chat_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    try:
        hour, minute = map(int, daily_time_hhmm.split(':'))
    except:
        hour, minute = 20, 0
    trigger = CronTrigger(hour=hour, minute=minute)
    scheduler.add_job(send_daily_summary, trigger, args=(chat_id,), id=job_id, replace_existing=True)

# ------------- startup ----------------
async def on_startup(dp):
    init_db()
    scheduler.start()
    schedule_all_users()
    print("✅ Scheduler запущен и бот готов к работе")

if __name__ == "__main__":
    print("✅ Бот запускается...")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
