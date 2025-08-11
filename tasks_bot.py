import telebot
import gspread
import schedule
import time
import threading
import re
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import types
import pytz

# ====== НАСТРОЙКИ ======
API_TOKEN = "ТОКЕН_БОТА"  # Замени на свой токен
TABLE_URL = "ССЫЛКА_НА_GOOGLE_SHEETS"  # Замени на свою
CREDENTIALS_FILE = "/etc/secrets/credentials.json"
WEBHOOK_URL = "https://tasksbot-hy3t.onrender.com/" + API_TOKEN
TIMEZONE = pytz.timezone("Europe/Moscow")

bot = telebot.TeleBot(API_TOKEN)

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)
tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")
repeat_ws = sh.worksheet("Повторяющиеся задачи")

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======
def get_users():
    return [
        {
            "name": row.get("Имя", ""),
            "id": str(row.get("Telegram ID")),
            "categories": [c.strip() for c in (row.get("Категории задач") or "").split(",") if c.strip()]
        }
        for row in users_ws.get_all_records()
        if row.get("Telegram ID")
    ]

def get_tasks_for_date(user_id, date_str):
    """Возвращает обычные задачи и повторяющиеся"""
    tasks = []
    # Обычные задачи
    for row in tasks_ws.get_all_records():
        if row.get("Дата") == date_str and str(row.get("User ID")) == str(user_id):
            tasks.append({**row, "repeat": False})
    # Повторяющиеся задачи
    weekday = datetime.strptime(date_str, "%d.%m.%Y").strftime("%A")
    for row in repeat_ws.get_all_records():
        if weekday.lower() in (row.get("Дни повторения") or "").lower() and str(row.get("User ID")) == str(user_id):
            tasks.append({**row, "repeat": True})
    return tasks

def get_tasks_for_week(user_id):
    today = datetime.now(TIMEZONE)
    week_dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    tasks_by_day = {}
    for date_str in week_dates:
        tasks_by_day[date_str] = get_tasks_for_date(user_id, date_str)
    return tasks_by_day

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, user_id])

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

# ====== ДОБАВЛЕНИЕ ЗАДАЧ ======
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
    bot.send_message(message.chat.id, "Введите дедлайн в формате ЧЧ:ММ:")

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

# ====== ОБРАБОТЧИКИ КНОПОК ======
@bot.message_handler(commands=["start"])
def start_cmd(message):
    bot.send_message(message.chat.id, "👋 Добро пожаловать! Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today_tasks(message):
    user_id = message.chat.id
    today_str = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    tasks = get_tasks_for_date(user_id, today_str)
    send_tasks_formatted(user_id, today_str, tasks)

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week_menu(message):
    bot.send_message(message.chat.id, "Выберите день недели:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def all_week_tasks(message):
    user_id = message.chat.id
    tasks_by_day = get_tasks_for_week(user_id)
    days_map = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    text = "🗓 Задачи на неделю:\n\n"
    for date_str, tasks in tasks_by_day.items():
        weekday = days_map[datetime.strptime(date_str, "%d.%m.%Y").weekday()]
        text += f"📅 {weekday} {date_str}\n"
        if tasks:
            for t in tasks:
                repeat_mark = " 🔁" if t.get("repeat") else ""
                text += f"- {t.get('Задача','')} (до {t.get('Дедлайн','')}){repeat_mark}\n\n"
        else:
            text += "Нет задач\n\n"
    bot.send_message(user_id, text)

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text)
def day_tasks(message):
    user_id = message.chat.id
    date_str = message.text.split("(")[1].strip(")")
    tasks = get_tasks_for_date(user_id, date_str)
    send_tasks_formatted(user_id, date_str, tasks)

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=main_menu())

def send_tasks_formatted(user_id, date_str, tasks):
    days_map = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    weekday = days_map[datetime.strptime(date_str, "%d.%m.%Y").weekday()]
    if tasks:
        text = f"📅 {weekday} {date_str}\n\n"
        for t in tasks:
            repeat_mark = " 🔁" if t.get("repeat") else ""
            text += f"- {t.get('Задача','')} (до {t.get('Дедлайн','')}){repeat_mark}\n\n"
        bot.send_message(user_id, text)
    else:
        bot.send_message(user_id, f"📅 {weekday} {date_str}\nНет задач.")

# ====== ПЛАНИРОВЩИК ======
def send_daily_plan():
    today_str = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    for user in get_users():
        tasks = get_tasks_for_date(user["id"], today_str)
        send_tasks_formatted(user["id"], today_str, tasks)

def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ====== WEBHOOK ======
app = Flask(__name__)

@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
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
