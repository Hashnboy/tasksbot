# tasks_bot.py
import os
import sys
import logging
import re
import threading
import schedule
import time
from datetime import datetime, timedelta
from flask import Flask, request
import pytz
import telebot
from telebot import types
import gspread

# -------------------------
# ========== SETTINGS ======
# -------------------------
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"  # secret file path on Render
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://tasksbot-hy3t.onrender.com")
WEBHOOK_PATH = f"/{API_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}"

# timezone (Moscow). Change if needed.
TZ = pytz.timezone("Europe/Moscow")

# -------------------------
# ========== LOGGING ======
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasks_bot")

# -------------------------
# ========== TELEGRAM =====
# -------------------------
bot = telebot.TeleBot(API_TOKEN)

# quick token check
try:
    me = bot.get_me()
    log.info(f"Telegram bot OK. @{me.username} (id={me.id})")
except Exception as e:
    log.fatal(f"Telegram token check failed: {e}")
    sys.exit(1)

# -------------------------
# ========== GOOGLE SHEETS =
# -------------------------
try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)
    log.info("Connected to Google Sheets.")
except Exception as e:
    log.fatal(f"Google Sheets auth/open failed: {e}")
    sys.exit(1)

# worksheets
try:
    tasks_ws = sh.worksheet("Задачи")
except Exception:
    log.fatal("Worksheet 'Задачи' not found. Create it and add header row.")
    sys.exit(1)

try:
    users_ws = sh.worksheet("Пользователи")
except Exception:
    users_ws = None
    log.warning("Worksheet 'Пользователи' not found (optional).")

# -------------------------
# ========== HELPERS ======
# -------------------------
def now_dt():
    return datetime.now(TZ)

def date_str(dt=None):
    dt = dt or now_dt()
    return dt.strftime("%d.%m.%Y")

def safe(x):
    return "" if x is None else str(x)

def send_markdown(chat_id, text, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"send_markdown failed to {chat_id}: {e}")

# -------------------------
# ========== SHEET OPS =====
# -------------------------
def get_all_tasks_rows():
    try:
        return tasks_ws.get_all_records()
    except Exception as e:
        log.error(f"Error reading tasks sheet: {e}")
        return []

def get_users_list():
    if not users_ws:
        return []
    try:
        rows = users_ws.get_all_records()
    except Exception as e:
        log.error(f"Error reading users sheet: {e}")
        return []
    users = []
    for r in rows:
        tid = r.get("Telegram ID") or r.get("User ID") or r.get("TelegramID")
        if tid:
            cats_raw = r.get("Категории задач") or ""
            cats = [c.strip() for c in cats_raw.split(",") if c.strip()] if cats_raw else []
            users.append({"name": r.get("Имя", ""), "id": str(tid), "categories": cats})
    return users

def add_task_to_sheet(date, category, subcategory, description, deadline, user_id, status="", repeat=""):
    """Append a standard row. Standard order:
       Дата,Категория,Подкатегория,Задача,Дедлайн,Статус,Повторяемость,User ID
    """
    row = [date, category, subcategory, description, deadline, status, repeat, str(user_id)]
    try:
        tasks_ws.append_row(row, value_input_option="USER_ENTERED")
        log.info(f"Task added for {user_id}: {description} ({date} {deadline})")
    except Exception as e:
        log.error(f"Failed to append task row: {e}")

# -------------------------
# ========== FORMATTING ====
# -------------------------
def format_tasks_grouped(tasks, title_date):
    """
    Group by Category -> Subcategory and pretty print.
    tasks: list of dict rows from sheet
    """
    # build nested dict: cat -> subcat -> [tasks]
    groups = {}
    for t in tasks:
        cat = t.get("Категория") or "Без категории"
        sub = t.get("Подкатегория") or ""
        groups.setdefault(cat, {}).setdefault(sub, []).append(t)

    lines = []
    lines.append(f"📅 *Задачи на {title_date}*")
    lines.append("—" * 28)
    for cat, subdict in groups.items():
        lines.append(f"\n*📌 {cat}*")
        for sub, items in subdict.items():
            if sub:
                lines.append(f"_{sub}_:")
            for i, it in enumerate(items, start=1):
                desc = safe(it.get("Задача"))
                dl = safe(it.get("Дедлайн"))
                dl_str = f" (до {dl})" if dl else ""
                lines.append(f"{i}. {desc}{dl_str}")
    # footer
    lines.append("\n✨ Желаешь изменить задачу? Отправь /cancel и добавь заново.")
    return "\n".join(lines)

# -------------------------
# ========== TASK FILTERS ==
# -------------------------
def tasks_for_date_and_user(user_id, date_string):
    rows = get_all_tasks_rows()
    uid = str(user_id)
    return [r for r in rows if safe(r.get("Дата")) == date_string and safe(r.get("User ID")) == uid]

def tasks_for_week_and_user(user_id, start_dt=None):
    start = start_dt or now_dt()
    dates = [(start + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    uid = str(user_id)
    rows = get_all_tasks_rows()
    return [r for r in rows if safe(r.get("Дата")) in dates and safe(r.get("User ID")) == uid]

# -------------------------
# ========== KEYBOARDS =====
# -------------------------
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя", "➕ Добавить задачу")
    return kb

def week_days_kb(anchor_dt=None):
    anchor = anchor_dt or now_dt()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    days = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    for i in range(7):
        d = anchor + timedelta(days=i)
        btn = f"{days[d.weekday()]} ({d.strftime('%d.%m.%Y')})"
        kb.add(btn)
    kb.add("⬅ Назад")
    return kb

# -------------------------
# ========== ADD TASK FLOW =
# -------------------------
user_state = {}       # chat_id -> step
user_tmp = {}         # chat_id -> dict
cleanup_timers = {}   # chat_id -> timer
CLEANUP_SECONDS = 5 * 60

date_regex = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
time_regex = re.compile(r"^\d{2}:\d{2}$")

def schedule_cleanup(chat_id):
    # cancel existing
    t = cleanup_timers.get(chat_id)
    if t:
        try:
            t.cancel()
        except:
            pass
    timer = threading.Timer(CLEANUP_SECONDS, cleanup_timeout, args=(chat_id,))
    timer.daemon = True
    cleanup_timers[chat_id] = timer
    timer.start()

def cleanup_timeout(chat_id):
    user_state.pop(chat_id, None)
    user_tmp.pop(chat_id, None)
    cleanup_timers.pop(chat_id, None)
    try:
        send_markdown(chat_id, "⏳ Ввод прерван (таймаут). Начните добавление заново при необходимости.", reply_markup=main_menu_kb())
    except:
        pass

@bot.message_handler(commands=["cancel"])
def handle_cancel(m):
    chat_id = m.chat.id
    user_state.pop(chat_id, None)
    user_tmp.pop(chat_id, None)
    t = cleanup_timers.pop(chat_id, None)
    if t:
        try:
            t.cancel()
        except:
            pass
    send_markdown(chat_id, "Операция отменена ✅", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить задачу")
def start_add_task(m):
    chat_id = m.chat.id
    user_state[chat_id] = "date"
    user_tmp[chat_id] = {}
    schedule_cleanup(chat_id)
    send_markdown(chat_id, "🗓 Введи дату задачи в формате *ДД.ММ.ГГГГ* (например: 13.08.2025).", reply_markup=None)

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id) == "date")
def step_date(m):
    chat_id = m.chat.id
    text = (m.text or "").strip()
    if not date_regex.match(text):
        send_markdown(chat_id, "❌ Неверный формат даты. Повторите ввод в формате *ДД.ММ.ГГГГ*.")
        schedule_cleanup(chat_id)
        return
    try:
        datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        send_markdown(chat_id, "❌ Некорректная дата. Попробуй ещё раз.")
        schedule_cleanup(chat_id)
        return
    user_tmp[chat_id]["date"] = text
    user_state[chat_id] = "category"
    schedule_cleanup(chat_id)
    send_markdown(chat_id, "🔖 Введи *категорию* (например: Табачка / Кофейня / Личное).")

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id) == "category")
def step_category(m):
    chat_id = m.chat.id
    user_tmp[chat_id]["category"] = (m.text or "").strip()
    user_state[chat_id] = "subcategory"
    schedule_cleanup(chat_id)
    send_markdown(chat_id, "📂 Введи *подкатегорию* (например: Центр / Полет / Заказы).")

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id) == "subcategory")
def step_subcategory(m):
    chat_id = m.chat.id
    user_tmp[chat_id]["subcategory"] = (m.text or "").strip()
    user_state[chat_id] = "task"
    schedule_cleanup(chat_id)
    send_markdown(chat_id, "✍️ Введи краткое *описание* задачи.")

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id) == "task")
def step_task(m):
    chat_id = m.chat.id
    user_tmp[chat_id]["task"] = (m.text or "").strip()
    user_state[chat_id] = "deadline"
    schedule_cleanup(chat_id)
    send_markdown(chat_id, "⏱ Введи *дедлайн* в формате ЧЧ:ММ или `-` если нет дедлайна (пример: 14:30).")

@bot.message_handler(func=lambda msg: user_state.get(msg.chat.id) == "deadline")
def step_deadline(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()
    if txt != "-" and not time_regex.match(txt):
        send_markdown(chat_id, "❌ Неверный формат времени. Введите *ЧЧ:ММ* или `-` если дедлайна нет.")
        schedule_cleanup(chat_id)
        return
    if txt != "-":
        try:
            datetime.strptime(txt, "%H:%M")
        except ValueError:
            send_markdown(chat_id, "❌ Некорректное время. Попробуй ещё раз.")
            schedule_cleanup(chat_id)
            return
        deadline = txt
    else:
        deadline = ""

    data = user_tmp.get(chat_id, {})
    add_task_to_sheet(
        date=data.get("date", date_str(None)),
        category=data.get("category", ""),
        subcategory=data.get("subcategory", ""),
        description=data.get("task", ""),
        deadline=deadline,
        user_id=chat_id
    )

    # cleanup
    user_state.pop(chat_id, None)
    user_tmp.pop(chat_id, None)
    t = cleanup_timers.pop(chat_id, None)
    if t:
        try:
            t.cancel()
        except:
            pass

    send_markdown(chat_id, "✅ Задача добавлена успешно!", reply_markup=main_menu_kb())

# -------------------------
# ========== VIEW TASKS ====
# -------------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    chat_id = m.chat.id
    send_markdown(chat_id, f"Привет! 👋\nТвой Telegram ID: `{chat_id}`\nВыбери действие:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def cmd_today(m):
    chat_id = m.chat.id
    today = date_str()
    tasks = tasks_for_date_and_user(chat_id, today)
    if tasks:
        text = format_tasks_grouped(tasks, today)
        send_markdown(chat_id, text, reply_markup=main_menu_kb())
    else:
        send_markdown(chat_id, f"✅ На *{today}* задач нет. Отдыхай или добавь новую задачу.", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def cmd_week_all(m):
    chat_id = m.chat.id
    tasks = tasks_for_week_and_user(chat_id)
    if tasks:
        text = format_tasks_grouped(tasks, f"неделю от {date_str()}")
        send_markdown(chat_id, text, reply_markup=main_menu_kb())
    else:
        send_markdown(chat_id, "✅ На ближайшую неделю задач нет.", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: "(" in (msg.text or "") and ")" in (msg.text or ""))
def cmd_day_button(m):
    chat_id = m.chat.id
    try:
        date_text = m.text.split("(")[1].split(")")[0].strip()
    except Exception:
        send_markdown(chat_id, "Неверный формат кнопки.", reply_markup=main_menu_kb())
        return
    tasks = tasks_for_date_and_user(chat_id, date_text)
    if tasks:
        text = format_tasks_grouped(tasks, date_text)
        send_markdown(chat_id, text, reply_markup=main_menu_kb())
    else:
        send_markdown(chat_id, f"✅ На {date_text} задач нет.", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def cmd_week_menu(m):
    bot.send_message(m.chat.id, "Выберите день недели:", reply_markup=week_days_kb())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def cmd_back(m):
    send_markdown(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

# -------------------------
# ========== SCHEDULER =====
# -------------------------
def send_daily_plans():
    today = date_str()
    users = get_users_list()
    for u in users:
        try:
            tasks = tasks_for_date_and_user(u["id"], today)
            if tasks:
                text = format_tasks_grouped(tasks, today)
                send_markdown(int(u["id"]), text)
        except Exception as e:
            log.warning(f"Failed to send daily plan to {u.get('id')}: {e}")

def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plans)
    while True:
        schedule.run_pending()
        time.sleep(1)

# -------------------------
# ========== WEBHOOK =======
# -------------------------
app = Flask(__name__)

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_receiver():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.error(f"Webhook handling error: {e}")
        return "ERR", 400
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Tasks bot (webhook) — alive", 200

# -------------------------
# ========== STARTUP =======
# -------------------------
if __name__ == "__main__":
    # remove old webhook if exists and set new
    try:
        bot.remove_webhook()
        log.info("Old webhook removed if present.")
    except Exception as e:
        log.warning(f"remove_webhook warning: {e}")

    try:
        bot.set_webhook(url=WEBHOOK_URL)
        log.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        log.fatal(f"Failed to set webhook: {e}")

    # start scheduler thread
    thr = threading.Thread(target=run_scheduler, daemon=True)
    thr.start()

    # run flask app
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
