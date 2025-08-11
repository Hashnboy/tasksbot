import telebot
import gspread
import schedule
import time
import threading
import re
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import types
from zoneinfo import ZoneInfo  # встроенный модуль Python 3.9+

# ====== НАСТРОЙКИ ======
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"
WEBHOOK_URL = "https://tasksbot-hy3t.onrender.com/" + API_TOKEN
TIMEZONE = ZoneInfo("Europe/Moscow")

bot = telebot.TeleBot(API_TOKEN)

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")

# ====== ЭМОДЗИ ДЛЯ КАТЕГОРИЙ ======
CATEGORY_EMOJIS = {
    "заказ": "📦",
    "дом": "🏠",
    "работа": "💼",
    "встреча": "📅",
    "покупка": "🛒",
    "отдых": "🌴",
}

# ====== ФУНКЦИИ ======
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
    for row in tasks_ws.get_all_records():
        if row.get("Дата") == date_str and str(row.get("User ID")) == str(user_id):
            tasks.append(row)
    return tasks

def get_tasks_for_week(user_id):
    today = datetime.now(TIMEZONE)
    week_dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    return [row for row in tasks_ws.get_all_records()
            if row.get("Дата") in week_dates and str(row.get("User ID")) == str(user_id)]

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, user_id])

def format_tasks_by_category(tasks, date_str):
    if not tasks:
        return f"❌ На {date_str} задач нет."
    
    grouped = {}
    for t in tasks:
        cat = t.get("Категория", "Без категории")
        emoji = next((em for key, em in CATEGORY_EMOJIS.items() if key.lower() in cat.lower()), "📌")
        grouped.setdefault(f"{emoji} {cat}", []).append(t)

    text = f"📅 Задачи на {date_str}:\n\n"
    for cat, items in grouped.items():
        text += f"{cat}:\n"
        for i, t in enumerate(items, 1):
            text += f"  {i}. {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        text += "\n"
    return text.strip()

# ====== КНОПКИ ======
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя", "➕ Добавить задачу")
    return markup

def week_days_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    today = datetime.now(TIMEZONE)
    days_map = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    for i in range(7):
        day_date = today + timedelta(days=i)
        btn_text = f"{days_map[day_date.weekday()]} ({day_date.strftime('%d.%m.%Y')})"
        markup.add(btn_text)
    markup.add("⬅ Назад")
    return markup

# ====== СТАТУСЫ ДЛЯ ДОБАВЛЕНИЯ ЗАДАЧ ======
user_steps = {}
temp_task_data = {}

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить задачу")
def add_task_start(message):
    user_steps[message.chat.id] = 'date'
    temp_task_data[message.chat.id] = {}
    bot.send_message(message.chat.id, "Введите дату задачи в формате ДД.ММ.ГГГГ:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == 'date')
def get_task_date(message):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text):
        bot.send_message(message.chat.id, "❌ Неверный формат даты!")
        return
    temp_task_data[message.chat.id]['date'] = message.text
    user_steps[message.chat.id] = 'category'
    bot.send_message(message.chat.id, "Введите категорию:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == 'category')
def get_task_category(message):
    temp_task_data[message.chat.id]['category'] = message.text
    user_steps[message.chat.id] = 'subcategory'
    bot.send_message(message.chat.id, "Введите подкатегорию:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == 'subcategory')
def get_task_subcategory(message):
    temp_task_data[message.chat.id]['subcategory'] = message.text
    user_steps[message.chat.id] = 'task'
    bot.send_message(message.chat.id, "Введите описание задачи:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == 'task')
def get_task_desc(message):
    temp_task_data[message.chat.id]['task'] = message.text
    user_steps[message.chat.id] = 'deadline'
    bot.send_message(message.chat.id, "Введите дедлайн (ЧЧ:ММ):")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == 'deadline')
def get_task_deadline(message):
    if not re.match(r"^\d{2}:\d{2}$", message.text):
        bot.send_message(message.chat.id, "❌ Неверный формат времени!")
        return
    data = temp_task_data[message.chat.id]
    data['deadline'] = message.text
    add_task(data['date'], data['category'], data['subcategory'], data['task'], data['deadline'], message.chat.id)
    bot.send_message(message.chat.id, "✅ Задача добавлена!", reply_markup=main_menu())
    user_steps.pop(message.chat.id, None)
    temp_task_data.pop(message.chat.id, None)

# ====== ОБРАБОТЧИКИ ======
@bot.message_handler(commands=["start"])
def start_cmd(message):
    bot.send_message(message.chat.id, "👋 Добро пожаловать! Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today_tasks(message):
    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    tasks = get_tasks_for_date(message.chat.id, today)
    bot.send_message(message.chat.id, format_tasks_by_category(tasks, today))

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week_menu_cmd(message):
    bot.send_message(message.chat.id, "📆 Выберите день:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def all_week_tasks(message):
    tasks = get_tasks_for_week(message.chat.id)
    if tasks:
        today = datetime.now(TIMEZONE)
        text = "🗓 Все задачи на неделю:\n\n"
        for i in range(7):
            day_date = (today + timedelta(days=i)).strftime("%d.%m.%Y")
            day_tasks = [t for t in tasks if t["Дата"] == day_date]
            text += format_tasks_by_category(day_tasks, day_date) + "\n\n"
        bot.send_message(message.chat.id, text.strip())
    else:
        bot.send_message(message.chat.id, "❌ На неделю задач нет.")

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text)
def day_tasks_cmd(message):
    date_str = message.text.split("(")[1].strip(")")
    tasks = get_tasks_for_date(message.chat.id, date_str)
    bot.send_message(message.chat.id, format_tasks_by_category(tasks, date_str))

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "🔙 Главное меню:", reply_markup=main_menu())

# ====== ПЛАНИРОВЩИК ======
def send_daily_plan():
    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    for user in get_users():
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            bot.send_message(user["id"], format_tasks_by_category(tasks, today))

def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ====== WEBHOOK ======
app = Flask(__name__)

@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
