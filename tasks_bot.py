# tasks_bot.py
import os
import sys
import logging
import telebot
import gspread
import schedule
import time
import threading
import re
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import types

# ================== НАСТРОЙКИ (вставлены твои значения) ==================
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"   # secret file в Render
WEBHOOK_HOST = "https://tasksbot-hy3t.onrender.com"   # твой Render URL
WEBHOOK_PATH = f"/{API_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}"

# ================== ЛОГИ ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================
bot = telebot.TeleBot(API_TOKEN)

# проверка токена при старте
try:
    me = bot.get_me()
    log.info(f"Bot OK. @{me.username} id={me.id}")
except Exception as e:
    log.fatal(f"Telegram token invalid or network error: {e}")
    sys.exit(1)

# ================== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==================
try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)
    log.info("Connected to Google Sheets.")
except Exception as e:
    log.fatal(f"Cannot connect to Google Sheets: {e}")
    sys.exit(1)

# Листы (если листа "Логи" нет — создадим)
try:
    tasks_ws = sh.worksheet("Задачи")
except Exception:
    log.fatal("Worksheet 'Задачи' not found. Create sheet with name 'Задачи' and headers.")
    sys.exit(1)

try:
    users_ws = sh.worksheet("Пользователи")
except Exception:
    log.warning("Worksheet 'Пользователи' not found — commands that read users may fail.")
    users_ws = None

# Лист для логов — если нет, создадим
try:
    logs_ws = sh.worksheet("Логи")
except Exception:
    try:
        logs_ws = sh.add_worksheet(title="Логи", rows="1000", cols="5")
        logs_ws.append_row(["Время", "Уровень", "Сообщение"])
        log.info("Created 'Логи' worksheet.")
    except Exception as e:
        log.warning(f"Cannot create 'Логи' worksheet: {e}")
        logs_ws = None

def log_to_sheets(level, message):
    try:
        if logs_ws:
            logs_ws.append_row([datetime.now().strftime("%d.%m.%Y %H:%M:%S"), level, message])
    except Exception as e:
        log.error(f"Failed to write log to sheet: {e}")

# ================== УТИЛИТЫ ==================
def safe_str(x):
    return "" if x is None else str(x)

# ================== ФУНКЦИИ РАБОТЫ С ТАБЛИЦЕЙ ==================
def get_users():
    if not users_ws:
        return []
    try:
        rows = users_ws.get_all_records()
    except Exception as e:
        log.error(f"Error reading users sheet: {e}")
        log_to_sheets("ERROR", f"Error reading users sheet: {e}")
        return []
    users = []
    for row in rows:
        tid = row.get("Telegram ID") or row.get("TelegramID") or row.get("User ID")
        if tid:
            raw = row.get("Категории задач") or ""
            cats = [c.strip() for c in raw.split(",") if c.strip()] if raw else []
            users.append({"name": row.get("Имя", ""), "id": str(tid), "categories": cats})
    return users

def get_tasks_all_rows():
    try:
        return tasks_ws.get_all_records()
    except Exception as e:
        log.error(f"Error reading tasks sheet: {e}")
        log_to_sheets("ERROR", f"Error reading tasks sheet: {e}")
        return []

def get_tasks_for_date(user_id, date_str):
    uid = str(user_id)
    rows = get_tasks_all_rows()
    return [r for r in rows if safe_str(r.get("Дата")) == date_str and safe_str(r.get("User ID")) == uid]

def get_tasks_for_week(user_id):
    today = datetime.now()
    week_dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    uid = str(user_id)
    rows = get_tasks_all_rows()
    return [r for r in rows if safe_str(r.get("Дата")) in week_dates and safe_str(r.get("User ID")) == uid]

def append_row_safe(values):
    """
    append_row в порядке, ожидаемом (если заголовки есть — best-effort).
    Если структура таблицы нестандартная — append по обычному порядку.
    Ожидаемый стандартный порядок: Дата,Категория,Подкатегория,Задача,Дедлайн,Статус,Повторяемость,User ID
    """
    try:
        # попытка добавить в конец стандартной строкой
        standard = [
            values.get("Дата", ""),
            values.get("Категория", ""),
            values.get("Подкатегория", ""),
            values.get("Задача", ""),
            values.get("Дедлайн", ""),
            values.get("Статус", ""),
            values.get("Повторяемость", ""),
            values.get("User ID", "")
        ]
        tasks_ws.append_row(standard, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        log.error(f"append_row failed: {e}")
        log_to_sheets("ERROR", f"append_row failed: {e}")
        return False

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    values = {
        "Дата": date,
        "Категория": category,
        "Подкатегория": subcategory,
        "Задача": task,
        "Дедлайн": deadline,
        "Статус": status,
        "Повторяемость": repeat,
        "User ID": str(user_id)
    }
    ok = append_row_safe(values)
    if ok:
        log.info(f"Added task for {user_id}: {task} @ {date} {deadline}")
        log_to_sheets("INFO", f"Added task for {user_id}: {task} @ {date} {deadline}")
    else:
        log.error("Failed to add task (see logs)")

# ================== КНОПКИ / МЕНЮ ==================
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя", "➕ Добавить задачу")
    return markup

def week_days_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    today = datetime.now()
    days_map = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    for i in range(7):
        d = today + timedelta(days=i)
        btn = f"{days_map[d.weekday()]} ({d.strftime('%d.%m.%Y')})"
        markup.add(btn)
    markup.add("⬅ Назад")
    return markup

# ================== ДОБАВЛЕНИЕ ЗАДАЧ (Пошагово) ==================
user_steps = {}     # chat_id -> state
temp_task_data = {} # chat_id -> dict
cleanup_timers = {} # chat_id -> threading.Timer
CLEANUP_TIMEOUT = 5 * 60  # 5 minutes

date_re = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
time_re = re.compile(r"^\d{2}:\d{2}$")

def schedule_cleanup(chat_id):
    # cancel old
    t_prev = cleanup_timers.get(chat_id)
    if t_prev:
        try:
            t_prev.cancel()
        except:
            pass
    t = threading.Timer(CLEANUP_TIMEOUT, force_cleanup, args=(chat_id,))
    t.daemon = True
    cleanup_timers[chat_id] = t
    t.start()

def force_cleanup(chat_id):
    user_steps.pop(chat_id, None)
    temp_task_data.pop(chat_id, None)
    cleanup_timers.pop(chat_id, None)
    try:
        bot.send_message(chat_id, "⏳ Ввод прерван (таймаут 5 минут). Начните заново при необходимости.", reply_markup=main_menu())
    except Exception:
        pass
    log.info(f"Cleaned temp state for {chat_id} due to timeout")
    log_to_sheets("INFO", f"Cleaned temp state for {chat_id} due to timeout")

@bot.message_handler(func=lambda m: (m.text or "") == "➕ Добавить задачу")
def add_task_start(message):
    chat_id = message.chat.id
    user_steps[chat_id] = "date"
    temp_task_data[chat_id] = {}
    schedule_cleanup(chat_id)
    bot.send_message(chat_id, "Введите дату задачи в формате ДД.MM.ГГГГ (например 13.08.2025):")

@bot.message_handler(func=lambda m: user_steps.get(getattr(m, "chat", {}).id) == "date" if getattr(m, "chat", None) else False)
def add_date(message):
    chat_id = message.chat.id
    txt = (message.text or "").strip()
    if not date_re.match(txt):
        bot.send_message(chat_id, "❌ Неверный формат даты! Введите ДД.MM.ГГГГ:")
        schedule_cleanup(chat_id)
        return
    try:
        datetime.strptime(txt, "%d.%m.%Y")
    except ValueError:
        bot.send_message(chat_id, "❌ Некорректная дата. Попробуйте ещё раз:")
        schedule_cleanup(chat_id)
        return
    temp_task_data[chat_id]["date"] = txt
    user_steps[chat_id] = "category"
    schedule_cleanup(chat_id)
    bot.send_message(chat_id, "Введите категорию (например: Табачка / Кофейня / Личное):")

@bot.message_handler(func=lambda m: user_steps.get(getattr(m, "chat", {}).id) == "category" if getattr(m, "chat", None) else False)
def add_category(message):
    chat_id = message.chat.id
    temp_task_data[chat_id]["category"] = (message.text or "").strip()
    user_steps[chat_id] = "subcategory"
    schedule_cleanup(chat_id)
    bot.send_message(chat_id, "Введите подкатегорию (например: Центр / Полет / Заказы):")

@bot.message_handler(func=lambda m: user_steps.get(getattr(m, "chat", {}).id) == "subcategory" if getattr(m, "chat", None) else False)
def add_subcategory(message):
    chat_id = message.chat.id
    temp_task_data[chat_id]["subcategory"] = (message.text or "").strip()
    user_steps[chat_id] = "task"
    schedule_cleanup(chat_id)
    bot.send_message(chat_id, "Введите описание задачи:")

@bot.message_handler(func=lambda m: user_steps.get(getattr(m, "chat", {}).id) == "task" if getattr(m, "chat", None) else False)
def add_task_desc(message):
    chat_id = message.chat.id
    temp_task_data[chat_id]["task"] = (message.text or "").strip()
    user_steps[chat_id] = "deadline"
    schedule_cleanup(chat_id)
    bot.send_message(chat_id, "Введите дедлайн в формате ЧЧ:ММ или '-' если дедлайна нет:")

@bot.message_handler(func=lambda m: user_steps.get(getattr(m, "chat", {}).id) == "deadline" if getattr(m, "chat", None) else False)
def add_task_deadline(message):
    chat_id = message.chat.id
    txt = (message.text or "").strip()
    if txt != "-" and not time_re.match(txt):
        bot.send_message(chat_id, "❌ Неверный формат времени! Введите ЧЧ:ММ или '-' если дедлайна нет:")
        schedule_cleanup(chat_id)
        return
    if txt != "-":
        try:
            datetime.strptime(txt, "%H:%M")
            deadline = txt
        except ValueError:
            bot.send_message(chat_id, "❌ Некорректное время. Введите снова:")
            schedule_cleanup(chat_id)
            return
    else:
        deadline = ""

    data = temp_task_data.get(chat_id, {})
    add_task(
        date=data.get("date", datetime.now().strftime("%d.%m.%Y")),
        category=data.get("category", ""),
        subcategory=data.get("subcategory", ""),
        task=data.get("task", ""),
        deadline=deadline,
        user_id=chat_id,
        status="",
        repeat=""
    )

    # cleanup
    user_steps.pop(chat_id, None)
    temp_task_data.pop(chat_id, None)
    timer = cleanup_timers.pop(chat_id, None)
    if timer:
        try:
            timer.cancel()
        except:
            pass

    bot.send_message(chat_id, "✅ Задача добавлена!", reply_markup=main_menu())

# /cancel
@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    chat_id = message.chat.id
    user_steps.pop(chat_id, None)
    temp_task_data.pop(chat_id, None)
    timer = cleanup_timers.pop(chat_id, None)
    if timer:
        try:
            timer.cancel()
        except:
            pass
    bot.send_message(chat_id, "Операция отменена.", reply_markup=main_menu())

# ================== КОМАНДЫ ДЛЯ ВЫВОДА ЗАДАЧ ==================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, f"Добро пожаловать!\nВаш Telegram ID: `{chat_id}`", parse_mode="Markdown", reply_markup=main_menu())

@bot.message_handler(func=lambda m: (m.text or "") == "📅 Сегодня")
def today_cmd(message):
    chat_id = message.chat.id
    today = datetime.now().strftime("%d.%m.%Y")
    try:
        tasks = get_tasks_for_date(chat_id, today)
    except Exception as e:
        log.error(f"Error getting tasks: {e}")
        bot.send_message(chat_id, "Ошибка при чтении задач. Попробуйте позже.")
        return
    if tasks:
        text = f"📅 Задачи на {today}:\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"{i}. {safe_str(t.get('Задача'))} (до {safe_str(t.get('Дедлайн'))})\n"
        bot.send_message(chat_id, text)
    else:
        bot.send_message(chat_id, "Сегодня задач нет.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: (m.text or "") == "📆 Неделя")
def week_menu_cmd(message):
    bot.send_message(message.chat.id, "Выберите день недели:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda m: (m.text or "") == "🗓 Вся неделя")
def all_week_cmd(message):
    chat_id = message.chat.id
    try:
        tasks = get_tasks_for_week(chat_id)
    except Exception as e:
        log.error(f"Error getting week tasks: {e}")
        bot.send_message(chat_id, "Ошибка при чтении задач. Попробуйте позже.")
        return
    if tasks:
        text = "🗓 Задачи на неделю:\n\n"
        for t in tasks:
            text += f"{safe_str(t.get('Дата'))}: {safe_str(t.get('Задача'))} (до {safe_str(t.get('Дедлайн'))})\n"
        bot.send_message(chat_id, text)
    else:
        bot.send_message(chat_id, "На неделю задач нет.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: "(" in (m.text or "") and ")" in (m.text or ""))
def day_button_cmd(message):
    chat_id = message.chat.id
    try:
        date_str = message.text.split("(")[1].split(")")[0].strip()
    except Exception:
        bot.send_message(chat_id, "Неверный формат кнопки.", reply_markup=main_menu())
        return
    tasks = get_tasks_for_date(chat_id, date_str)
    if tasks:
        text = f"📅 Задачи на {date_str}:\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"{i}. {safe_str(t.get('Задача'))} (до {safe_str(t.get('Дедлайн'))})\n"
        bot.send_message(chat_id, text)
    else:
        bot.send_message(chat_id, "В этот день задач нет.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: (m.text or "") == "⬅ Назад")
def back_cmd(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=main_menu())

# ================== ПЛАНИРОВЩИК ==================
def send_daily_plan():
    today = datetime.now().strftime("%d.%m.%Y")
    users = get_users()
    for u in users:
        try:
            tasks = get_tasks_for_date(u["id"], today)
            if tasks:
                text = f"📅 План на {today}:\n\n"
                for i, t in enumerate(tasks, 1):
                    text += f"{i}. {safe_str(t.get('Задача'))} (до {safe_str(t.get('Дедлайн'))})\n"
                bot.send_message(int(u["id"]), text)
        except Exception as e:
            log.warning(f"Failed to send daily plan to {u.get('id')}: {e}")
            log_to_sheets("WARN", f"Failed to send daily plan to {u.get('id')}: {e}")

def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ================== FLASK / WEBHOOK ==================
app = Flask(__name__)

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_receiver():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.error(f"Error processing webhook update: {e}")
        log_to_sheets("ERROR", f"Error processing webhook update: {e}")
        return "ERROR", 400
    return "OK", 200

@app.route("/", methods=["GET"])
def home():
    return "Bot is running (webhook).", 200

# ================== СТАРТ ==================
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        log.info("Old webhook removed (if any).")
    except Exception as e:
        log.warning(f"remove_webhook() warning: {e}")

    try:
        bot.set_webhook(url=WEBHOOK_URL)
        log.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        log.fatal(f"Failed to set webhook: {e}")
        log_to_sheets("FATAL", f"Failed to set webhook: {e}")
        # не выходим, но webhook не будет работать, посмотри логи

    # старт планировщика
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # запуск Flask (Render uses $PORT or default 5000)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
