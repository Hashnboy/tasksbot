import telebot
import gspread
import schedule
import time
import threading
from datetime import datetime, timedelta
from flask import Flask
from telebot import types

# ====== НАСТРОЙКИ ======
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"

bot = telebot.TeleBot(API_TOKEN)
bot.remove_webhook()  # убираем webhook, чтобы избежать ошибки 409

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")
repeat_ws = sh.worksheet("Повторяющиеся задачи")

# ====== ВСПОМОГАТЕЛЬНЫЕ ======
def get_users():
    users = []
    for row in users_ws.get_all_records():
        if row.get("Telegram ID"):
            cats = [c.strip() for c in (row.get("Категории задач") or "").split(",") if c.strip()]
            users.append({
                "name": row.get("Имя", ""),
                "id": str(row.get("Telegram ID")),
                "categories": cats
            })
    return users

def get_tasks_for_date(user_id, date_str):
    tasks = []
    user_info = next((u for u in get_users() if u["id"] == str(user_id)), None)
    for row in tasks_ws.get_all_records():
        if row.get("Дата") == date_str and str(row.get("Пользователь ID", "")) == str(user_id):
            if not user_info or not user_info["categories"] or row.get("Категория") in user_info["categories"]:
                tasks.append(row)
    return tasks

def add_task(date, category, subcategory, task, deadline, status="", repeat="", user_id=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, user_id])

# ====== ОБРАБОТКА ПОВТОРЯЮЩИХСЯ ======
def process_repeating_tasks():
    today_str = datetime.now().strftime("%d.%m.%Y")
    today_rus = {
        "monday": "понедельник",
        "tuesday": "вторник",
        "wednesday": "среда",
        "thursday": "четверг",
        "friday": "пятница",
        "saturday": "суббота",
        "sunday": "воскресенье"
    }[datetime.now().strftime("%A").lower()]

    existing = [t.get("Задача") for t in tasks_ws.get_all_records() if t.get("Дата") == today_str]
    for row in repeat_ws.get_all_records():
        if (row.get("День недели") or "").strip().lower() == today_rus:
            if row.get("Задача") and row.get("Задача") not in existing:
                for user in get_users():
                    add_task(today_str, row.get("Категория", ""), row.get("Подкатегория", ""),
                             row.get("Задача", ""), row.get("Время", ""), "", "повтор", user["id"])

def schedule_next_repeat_tasks():
    wd = {"понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6}
    today = datetime.now()
    for row in repeat_ws.get_all_records():
        d = (row.get("День недели") or "").strip().lower()
        if d not in wd: continue
        days_ahead = (wd[d] - today.weekday() + 7) % 7 or 7
        task_date = (today + timedelta(days=days_ahead)).strftime("%d.%m.%Y")
        existing = [t.get("Задача") for t in tasks_ws.get_all_records() if t.get("Дата") == task_date]
        if row.get("Задача") and row.get("Задача") not in existing:
            for user in get_users():
                add_task(task_date, row.get("Категория", ""), row.get("Подкатегория", ""),
                         row.get("Задача", ""), row.get("Время", ""), "", "повтор", user["id"])

# ====== ОТПРАВКА ПЛАНА ======
def send_daily_plan():
    process_repeating_tasks()
    schedule_next_repeat_tasks()
    today = datetime.now().strftime("%d.%m.%Y")
    for user in get_users():
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            text = f"📅 План на {today}:\n\n"
            for i, t in enumerate(tasks, 1):
                status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
                text += f"{status_icon} {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
            bot.send_message(user["id"], text)

# ====== КНОПКИ ======
@bot.message_handler(commands=["start"])
def start_cmd(message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📅 Мои задачи на сегодня", "📆 Задачи на неделю")
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📅 Мои задачи на сегодня")
def today_tasks(message):
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = get_tasks_for_date(message.chat.id, today)
    if not tasks:
        bot.send_message(message.chat.id, "На сегодня задач нет ✅")
    else:
        text = f"📅 План на {today}:\n\n"
        for i, t in enumerate(tasks, 1):
            status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
            text += f"{status_icon} {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📆 Задачи на неделю")
def week_tasks(message):
    kb = types.InlineKeyboardMarkup()
    today = datetime.now()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    for i, d in enumerate(days):
        date_str = (today + timedelta(days=(i - today.weekday()) % 7)).strftime("%d.%m.%Y")
        kb.add(types.InlineKeyboardButton(f"{d.capitalize()} ({date_str})", callback_data=f"day_{date_str}"))
    kb.add(types.InlineKeyboardButton("📅 Вся неделя", callback_data="week_all"))
    bot.send_message(message.chat.id, "Выберите день:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("day_"))
def show_day_tasks(call):
    date_str = call.data.split("_")[1]
    tasks = get_tasks_for_date(call.message.chat.id, date_str)
    if not tasks:
        bot.send_message(call.message.chat.id, f"На {date_str} задач нет ✅")
    else:
        text = f"📅 Задачи на {date_str}:\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"⬜ {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(call.message.chat.id, text)

@bot.callback_query_handler(func=lambda call: call.data == "week_all")
def show_week_tasks(call):
    today = datetime.now()
    text = "📅 Задачи на неделю:\n\n"
    for i in range(7):
        date_str = (today + timedelta(days=i)).strftime("%d.%m.%Y")
        tasks = get_tasks_for_date(call.message.chat.id, date_str)
        if tasks:
            text += f"\n🗓 {date_str}:\n"
            for j, t in enumerate(tasks, 1):
                text += f"⬜ {j}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
    bot.send_message(call.message.chat.id, text)

# ====== СТАРТ ======
def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=lambda: bot.polling(none_stop=True), daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
