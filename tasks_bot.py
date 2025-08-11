#!/usr/bin/env python3
# coding: utf-8
"""
tasks_bot.py

Webhook-only Telegram tasks bot.
Storage layer is abstracted (SheetsStorage provided). Easy to replace with DB.
Supports: repeating tasks, grouped output by category/subcategory, add-task flow, scheduler.
"""

import os
import sys
import logging
import threading
import schedule
import time
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request
import telebot
from telebot import types

# Optional: gspread import is inside SheetsStorage to allow switching storage easily
import gspread

# ----------------------
# CONFIG / ENV
# ----------------------
# Prefer env vars, fallback to values you previously shared (for convenience).
API_TOKEN = os.getenv("API_TOKEN", "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4")
TABLE_URL = os.getenv("TABLE_URL", "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/etc/secrets/credentials.json")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://tasksbot-hy3t.onrender.com")
WEBHOOK_PATH = f"/{API_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}"

# Time zone
TZ = ZoneInfo("Europe/Moscow")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasks_bot")

# ----------------------
# UTILITIES
# ----------------------
def now_moscow():
    return datetime.now(TZ)

def date_str(dt=None):
    dt = dt or now_moscow()
    return dt.strftime("%d.%m.%Y")

def safe(x):
    return "" if x is None else str(x)

def weekday_rus(dt=None):
    dt = dt or now_moscow()
    # monday=0..sunday=6
    names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return names[dt.weekday()]

def weekday_short_rus(dt=None):
    dt = dt or now_moscow()
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

# ----------------------
# STORAGE LAYER (абстракция)
# ----------------------
class Storage:
    """Abstract storage interface. Implementations must provide below methods."""
    def get_users(self):
        raise NotImplementedError
    def get_tasks_all(self):
        raise NotImplementedError
    def append_task(self, row_dict):
        raise NotImplementedError
    def get_repeat_tasks_all(self):
        raise NotImplementedError
    def log_entry(self, level, msg):
        raise NotImplementedError

class SheetsStorage(Storage):
    """
    Google Sheets implementation.
    Expects sheets:
      - 'Задачи' (rows: Дата,Категория,Подкатегория,Задача,Дедлайн,Статус,Повторяемость,User ID)
      - 'Пользователи' (rows: Имя,Telegram ID,Категории задач)
      - 'Повторяющиеся задачи' (rows: Категория,Подкатегория,Задача,Дедлайн,Дни недели,User ID(optional))
      - 'Логи' (created automatically if missing)
    """
    def __init__(self, table_url, creds_file):
        try:
            self.gc = gspread.service_account(filename=creds_file)
            self.sh = self.gc.open_by_url(table_url)
        except Exception as e:
            log.fatal(f"Sheets auth/open failed: {e}")
            raise

        # load worksheets or create/log
        try:
            self.tasks_ws = self.sh.worksheet("Задачи")
        except Exception:
            log.fatal("Worksheet 'Задачи' not found. Please create sheet with name 'Задачи' and required columns.")
            raise

        try:
            self.users_ws = self.sh.worksheet("Пользователи")
        except Exception:
            self.users_ws = None
            log.warning("'Пользователи' sheet not found; get_users will return empty list.")

        try:
            self.repeat_ws = self.sh.worksheet("Повторяющиеся задачи")
        except Exception:
            self.repeat_ws = None
            log.warning("'Повторяющиеся задачи' sheet not found; repeating tasks disabled.")

        # logs sheet: try to open, else create
        try:
            self.logs_ws = self.sh.worksheet("Логи")
        except Exception:
            try:
                self.logs_ws = self.sh.add_worksheet(title="Логи", rows="1000", cols="4")
                self.logs_ws.append_row(["Время", "Уровень", "Сообщение"])
                log.info("Created 'Логи' worksheet.")
            except Exception as e:
                self.logs_ws = None
                log.warning(f"Could not create 'Логи' sheet: {e}")

    def get_users(self):
        if not self.users_ws:
            return []
        try:
            rows = self.users_ws.get_all_records()
            users = []
            for r in rows:
                tid = r.get("Telegram ID") or r.get("User ID") or r.get("TelegramID")
                if tid:
                    raw = r.get("Категории задач") or ""
                    cats = [c.strip() for c in raw.split(",") if c.strip()] if raw else []
                    users.append({"name": r.get("Имя", ""), "id": str(tid), "categories": cats})
            return users
        except Exception as e:
            log.error(f"Error reading users sheet: {e}")
            return []

    def get_tasks_all(self):
        try:
            return self.tasks_ws.get_all_records()
        except Exception as e:
            log.error(f"Error reading tasks sheet: {e}")
            return []

    def append_task(self, row_dict):
        # standard order:
        row = [
            row_dict.get("Дата",""),
            row_dict.get("Категория",""),
            row_dict.get("Подкатегория",""),
            row_dict.get("Задача",""),
            row_dict.get("Дедлайн",""),
            row_dict.get("Статус",""),
            row_dict.get("Повторяемость",""),
            str(row_dict.get("User ID",""))
        ]
        try:
            self.tasks_ws.append_row(row, value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            log.error(f"Failed to append task: {e}")
            self.log_entry("ERROR", f"Failed to append task: {e}")
            return False

    def get_repeat_tasks_all(self):
        if not self.repeat_ws:
            return []
        try:
            return self.repeat_ws.get_all_records()
        except Exception as e:
            log.error(f"Error reading repeat tasks sheet: {e}")
            return []

    def log_entry(self, level, msg):
        try:
            if self.logs_ws:
                self.logs_ws.append_row([datetime.now().strftime("%d.%m.%Y %H:%M:%S"), level, msg])
        except Exception as e:
            log.warning(f"Failed to write to logs sheet: {e}")

# ----------------------
# STORAGE INSTANTIATION
# ----------------------
try:
    storage = SheetsStorage(TABLE_URL, CREDENTIALS_FILE)
    log.info("Storage initialized (SheetsStorage).")
except Exception as e:
    log.fatal("Storage initialization failed. Exiting.")
    sys.exit(1)

# ----------------------
# TELEGRAM BOT
# ----------------------
bot = telebot.TeleBot(API_TOKEN)

# quick token validation
try:
    me = bot.get_me()
    log.info(f"Telegram bot ready. @{me.username} (id={me.id})")
except Exception as e:
    log.fatal(f"Telegram token invalid or network error: {e}")
    sys.exit(1)

# ----------------------
# CATEGORY EMOJI MAP (simple)
# ----------------------
CATEGORY_EMOJIS = {
    "табак": "🚬",
    "кофе": "☕",
    "кофейня": "☕",
    "личн": "🌟",
    "заказ": "📦",
    "склад": "📦",
    "доставка": "🚚",
    "звон": "📞",
    "федя": "🧾",
    "реал": "📊",
    "авант": "🥛",
    # add as needed
}

def emoji_for_category(cat):
    if not cat:
        return "📌"
    c = cat.lower()
    for key, em in CATEGORY_EMOJIS.items():
        if key in c:
            return em
    return "📌"

# ----------------------
# REPEATING TASKS HELPERS
# ----------------------
# Support various ways of storing weekdays in sheet: "понедельник", "Пн,Ср", "Mon,Wed" etc.
RUS_WEEK_MAP = {
    "mon": "понедельник", "monday": "понедельник", "пн": "понедельник", "понедельник": "понедельник",
    "tue": "вторник", "tuesday": "вторник", "вт": "вторник", "вторник": "вторник",
    "wed": "среда", "wednesday": "среда", "ср": "среда", "среда": "среда",
    "thu": "четверг", "thursday": "четверг", "чт": "четверг", "четверг": "четверг",
    "fri": "пятница", "friday": "пятница", "пт": "пятница", "пятница": "пятница",
    "sat": "суббота", "saturday": "суббота", "сб": "суббота", "суббота": "суббота",
    "sun": "воскресенье", "sunday": "воскресенье", "вс": "воскресенье", "воскресенье": "воскресенье"
}

def normalize_weekday_token(tok):
    tok = tok.strip().lower()
    return RUS_WEEK_MAP.get(tok, tok)

def repeat_tasks_for_date(user_id, date_obj):
    """
    Return list of repeat-task-like dicts with fields: Дата,Категория,Подкатегория,Задача,Дедлайн,Статус,Повторяемость,User ID
    date_obj: datetime
    """
    rows = storage.get_repeat_tasks_all()
    if not rows:
        return []
    today_week = weekday_rus(date_obj)  # e.g. 'понедельник'
    result = []
    for r in rows:
        days_field = safe(r.get("Дни недели") or r.get("День недели") or r.get("Дни") or "")
        if not days_field:
            continue
        # split by common separators
        parts = re.split(r"[;,/|]+|\s+", days_field)
        parts = [p for p in parts if p.strip()]
        normalized = [normalize_weekday_token(p) for p in parts]
        # if sheet contains "ежедневно" or "daily"
        if any(p in ("ежедневно","daily","everyday","every day") for p in parts):
            match = True
        else:
            match = any(p == today_week for p in normalized)
        if match:
            # optional per-user filtering: if repeat row has User ID non-empty, only that user
            rid = r.get("User ID") or r.get("Telegram ID") or ""
            if rid:
                if str(rid) != str(user_id):
                    continue
            # build row dict similar to tasks table
            row = {
                "Дата": date_obj.strftime("%d.%m.%Y"),
                "Категория": r.get("Категория") or "",
                "Подкатегория": r.get("Подкатегория") or "",
                "Задача": r.get("Задача") or "",
                "Дедлайн": r.get("Дедлайн") or "",
                "Статус": "",
                "Повторяемость": "повтор",
                "User ID": str(user_id)
            }
            result.append(row)
    return result

def repeat_tasks_for_week(user_id, start_date=None):
    start = start_date or now_moscow()
    result = []
    for i in range(7):
        d = start + timedelta(days=i)
        result.extend(repeat_tasks_for_date(user_id, d))
    return result

# ----------------------
# TASK FETCH & MERGE
# ----------------------
def tasks_for_user_on_date(user_id, date_obj):
    dstr = date_obj.strftime("%d.%m.%Y")
    all_tasks = storage.get_tasks_all()
    user_tasks = [r for r in all_tasks if safe(r.get("Дата")) == dstr and safe(r.get("User ID")) == str(user_id)]
    # add repeating tasks
    repeats = repeat_tasks_for_date(user_id, date_obj)
    # avoid duplicates by task text+deadline
    existing_keys = {(safe(t.get("Задача")), safe(t.get("Дедлайн"))) for t in user_tasks}
    for rp in repeats:
        key = (safe(rp.get("Задача")), safe(rp.get("Дедлайн")))
        if key not in existing_keys:
            user_tasks.append(rp)
    return user_tasks

def tasks_for_user_week(user_id, start_date=None):
    start = start_date or now_moscow()
    all_tasks = storage.get_tasks_all()
    dates = [(start + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    user_tasks = [r for r in all_tasks if safe(r.get("Дата")) in dates and safe(r.get("User ID")) == str(user_id)]
    # add repeating tasks for the week
    repeats = repeat_tasks_for_week(user_id, start_date=start)
    existing_keys = {(safe(t.get("Дата")), safe(t.get("Задача")), safe(t.get("Дедлайн"))) for t in user_tasks}
    for rp in repeats:
        key = (safe(rp.get("Дата")), safe(rp.get("Задача")), safe(rp.get("Дедлайн")))
        if key not in existing_keys:
            user_tasks.append(rp)
    return user_tasks

# ----------------------
# FORMATTING for output
# ----------------------
def format_grouped(tasks, title):
    if not tasks:
        return f"✅ На *{title}* задач нет."
    # group by category -> subcategory
    groups = {}
    for t in tasks:
        cat = safe(t.get("Категория")) or "Без категории"
        sub = safe(t.get("Подкатегория")) or ""
        groups.setdefault(cat, {}).setdefault(sub, []).append(t)

    lines = []
    lines.append(f"📅 *{title}*")
    lines.append("—" * 30)
    for cat, subdict in groups.items():
        em = emoji_for_category(cat)
        lines.append(f"\n*{em} {cat}*")
        for sub, items in subdict.items():
            if sub:
                lines.append(f"_{sub}_:")
            for idx, it in enumerate(items, 1):
                desc = safe(it.get("Задача"))
                dl = safe(it.get("Дедлайн"))
                dlpart = f" (до {dl})" if dl else ""
                lines.append(f"{idx}. {desc}{dlpart}")
    lines.append("\n🔔 Чтобы отменить операцию — /cancel")
    return "\n".join(lines)

# ----------------------
# ADD TASK FLOW (conversation)
# ----------------------
user_state = {}      # chat_id -> step
user_temp = {}       # chat_id -> dict
cleanup_timers = {}  # chat_id -> timer
CLEANUP_SECONDS = 5 * 60

date_regex = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
time_regex = re.compile(r"^\d{2}:\d{2}$")

def schedule_cleanup(chat_id):
    t_prev = cleanup_timers.get(chat_id)
    if t_prev:
        try:
            t_prev.cancel()
        except:
            pass
    t = threading.Timer(CLEANUP_SECONDS, cleanup_timeout, args=(chat_id,))
    t.daemon = True
    cleanup_timers[chat_id] = t
    t.start()

def cleanup_timeout(chat_id):
    user_state.pop(chat_id, None)
    user_temp.pop(chat_id, None)
    cleanup_timers.pop(chat_id, None)
    try:
        bot.send_message(chat_id, "⏳ Ввод прерван (таймаут). Начните заново при необходимости.")
    except:
        pass
    storage.log_entry("INFO", f"Cleanup timeout for {chat_id}")

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m):
    cid = m.chat.id
    user_state.pop(cid, None)
    user_temp.pop(cid, None)
    t = cleanup_timers.pop(cid, None)
    if t:
        try:
            t.cancel()
        except:
            pass
    bot.send_message(cid, "Операция отменена.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📅 Сегодня","📆 Неделя","🗓 Вся неделя","➕ Добавить задачу"))

@bot.message_handler(func=lambda m: (m.text or "").strip() == "➕ Добавить задачу")
def flow_start(m):
    cid = m.chat.id
    user_state[cid] = "date"
    user_temp[cid] = {}
    schedule_cleanup(cid)
    bot.send_message(cid, "🗓 Введите дату задачи (ДД.ММ.ГГГГ):")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "date")
def flow_date(m):
    cid = m.chat.id
    txt = (m.text or "").strip()
    if not date_regex.match(txt):
        bot.send_message(cid, "❌ Неверный формат даты. Используй ДД.MM.ГГГГ")
        schedule_cleanup(cid)
        return
    try:
        datetime.strptime(txt, "%d.%m.%Y")
    except ValueError:
        bot.send_message(cid, "❌ Некорректная дата.")
        schedule_cleanup(cid)
        return
    user_temp[cid]["Дата"] = txt
    user_state[cid] = "category"
    schedule_cleanup(cid)
    bot.send_message(cid, "🔖 Введите категорию (например: Табачка / Кофейня / Личное):")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "category")
def flow_category(m):
    cid = m.chat.id
    user_temp[cid]["Категория"] = (m.text or "").strip()
    user_state[cid] = "subcategory"
    schedule_cleanup(cid)
    bot.send_message(cid, "📂 Введите подкатегорию (например: Центр / Полет / Заказы):")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "subcategory")
def flow_subcategory(m):
    cid = m.chat.id
    user_temp[cid]["Подкатегория"] = (m.text or "").strip()
    user_state[cid] = "task"
    schedule_cleanup(cid)
    bot.send_message(cid, "✍️ Введите краткое описание задачи:")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "task")
def flow_task(m):
    cid = m.chat.id
    user_temp[cid]["Задача"] = (m.text or "").strip()
    user_state[cid] = "deadline"
    schedule_cleanup(cid)
    bot.send_message(cid, "⏱ Введите дедлайн ЧЧ:ММ или '-' если нет:")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "deadline")
def flow_deadline(m):
    cid = m.chat.id
    txt = (m.text or "").strip()
    if txt != "-" and not time_regex.match(txt):
        bot.send_message(cid, "❌ Неверный формат времени. Попробуйте ЧЧ:ММ или '-'")
        schedule_cleanup(cid)
        return
    deadline = "" if txt == "-" else txt
    data = user_temp.get(cid, {})
    # ensure User ID is set to the chat that added the task (even if admin adds)
    data["User ID"] = cid
    data.setdefault("Статус", "")
    data.setdefault("Повторяемость", "")
    data.setdefault("Дедлайн", deadline)
    success = storage.append_task({
        "Дата": data.get("Дата",""),
        "Категория": data.get("Категория",""),
        "Подкатегория": data.get("Подкатегория",""),
        "Задача": data.get("Задача",""),
        "Дедлайн": data.get("Дедлайн",""),
        "Статус": data.get("Статус",""),
        "Повторяемость": data.get("Повторяемость",""),
        "User ID": data.get("User ID","")
    })
    if success:
        bot.send_message(cid, "✅ Задача добавлена!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("📅 Сегодня","📆 Неделя","🗓 Вся неделя","➕ Добавить задачу"))
        storage.log_entry("INFO", f"Task added by {cid}: {data.get('Задача')}")
    else:
        bot.send_message(cid, "❌ Ошибка при добавлении задачи. Посмотри логи.")
    # cleanup
    user_state.pop(cid, None)
    user_temp.pop(cid, None)
    t = cleanup_timers.pop(cid, None)
    if t:
        try:
            t.cancel()
        except:
            pass

# ----------------------
# BUTTONS / VIEW HANDLERS
# ----------------------
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя", "➕ Добавить задачу")
    return kb

def week_kb(anchor=None):
    anchor = anchor or now_moscow()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    days = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    for i in range(7):
        d = anchor + timedelta(days=i)
        btn = f"{days[d.weekday()]} ({d.strftime('%d.%m.%Y')})"
        kb.add(btn)
    kb.add("⬅ Назад")
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(m):
    cid = m.chat.id
    bot.send_message(cid, f"Привет! 👋\nТвой Telegram ID: `{cid}`", parse_mode="Markdown", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: (m.text or "") == "📅 Сегодня")
def cmd_today(m):
    cid = m.chat.id
    d = now_moscow()
    tasks = tasks_for_user_on_date(cid, d)
    text = format_grouped(tasks, d.strftime("%d.%m.%Y"))
    bot.send_message(cid, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: (m.text or "") == "📆 Неделя")
def cmd_week_menu(m):
    bot.send_message(m.chat.id, "Выберите день:", reply_markup=week_kb())

@bot.message_handler(func=lambda m: (m.text or "") == "🗓 Вся неделя")
def cmd_week_all(m):
    cid = m.chat.id
    start = now_moscow()
    tasks = tasks_for_user_week(cid, start_date=start)
    if not tasks:
        bot.send_message(cid, "✅ На ближайшую неделю задач нет.", reply_markup=main_menu_kb())
        return
    # group by date then format per day
    days = {}
    for t in tasks:
        d = safe(t.get("Дата"))
        days.setdefault(d, []).append(t)
    # sort days by date ascending
    sorted_days = sorted(days.items(), key=lambda kv: datetime.strptime(kv[0], "%d.%m.%Y"))
    texts = []
    for dstr, items in sorted_days:
        texts.append(format_grouped(items, dstr))
    full = "\n\n".join(texts)
    bot.send_message(cid, full, parse_mode="Markdown")

@bot.message_handler(func=lambda m: "(" in (m.text or "") and ")" in (m.text or ""))
def cmd_day_from_kb(m):
    cid = m.chat.id
    try:
        dstr = m.text.split("(")[1].split(")")[0].strip()
        # validate
        datetime.strptime(dstr, "%d.%m.%Y")
    except Exception:
        bot.send_message(cid, "Неверный формат даты в кнопке.", reply_markup=main_menu_kb())
        return
    tasks = tasks_for_user_on_date(cid, datetime.strptime(dstr, "%d.%m.%Y").replace(tzinfo=TZ))
    text = format_grouped(tasks, dstr)
    bot.send_message(cid, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: (m.text or "") == "⬅ Назад")
def cmd_back(m):
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

# ----------------------
# SCHEDULER: daily plan send
# ----------------------
def send_daily_plans():
    d = now_moscow()
    dstr = d.strftime("%d.%m.%Y")
    users = storage.get_users()
    for u in users:
        uid = u.get("id")
        try:
            tasks = tasks_for_user_on_date(uid, d)
            if tasks:
                txt = format_grouped(tasks, dstr)
                bot.send_message(int(uid), txt, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Failed to send daily plan to {uid}: {e}")
            storage.log_entry("WARN", f"Failed to send daily plan to {uid}: {e}")

def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plans)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ----------------------
# FLASK / WEBHOOK
# ----------------------
app = Flask(__name__)

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_receiver():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.error(f"Error processing webhook update: {e}")
        storage.log_entry("ERROR", f"Webhook processing error: {e}")
        return "ERROR", 400
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Tasks bot (webhook) — alive", 200

# ----------------------
# STARTUP
# ----------------------
if __name__ == "__main__":
    # set webhook (remove previous first)
    try:
        bot.remove_webhook()
        log.info("Old webhook removed.")
    except Exception as e:
        log.warning(f"remove_webhook warning: {e}")
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        log.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        log.fatal(f"Failed to set webhook: {e}")
        storage.log_entry("FATAL", f"Failed to set webhook: {e}")
        # continue to run (webhook not set — but code will be running)
    # start scheduler thread
    sch = threading.Thread(target=run_scheduler, daemon=True)
    sch.start()
    # run flask app
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
