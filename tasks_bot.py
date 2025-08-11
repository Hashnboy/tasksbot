import telebot
import gspread
import schedule
import time
import threading
from datetime import datetime, timedelta
from flask import Flask
from telebot import types

# ====== НАСТРОЙКИ ======
API_TOKEN = "ТВОЙ_ТОКЕН"
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"

bot = telebot.TeleBot(API_TOKEN)

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")
repeat_ws = sh.worksheet("Повторяющиеся задачи")

# ====== ФУНКЦИИ ======
def get_users():
    users = []
    rows = users_ws.get_all_records()
    for row in rows:
        if row.get("Telegram ID"):
            categories = []
            raw = row.get("Категории задач") or ""
            if raw:
                categories = [c.strip() for c in raw.split(",") if c.strip()]
            users.append({
                "name": row.get("Имя", ""),
                "id": str(row.get("Telegram ID")),
                "categories": categories
            })
    return users

def get_tasks_for_date(user_id, date_str):
    tasks = []
    rows = tasks_ws.get_all_records()
    for row in rows:
        if row.get("Дата") == date_str and str(row.get("User ID")) == str(user_id):
            tasks.append(row)
    return tasks

def get_tasks_for_week(user_id):
    today = datetime.now()
    week_dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    tasks = []
    rows = tasks_ws.get_all_records()
    for row in rows:
        if row.get("Дата") in week_dates and str(row.get("User ID")) == str(user_id):
            tasks.append(row)
    return tasks

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, user_id])

def process_repeating_tasks():
    today_str = datetime.now().strftime("%d.%m.%Y")
    today_weekday = datetime.now().strftime("%A").lower()
    weekday_map = {
        "monday": "понедельник",
        "tuesday": "вторник",
        "wednesday": "среда",
        "thursday": "четверг",
        "friday": "пятница",
        "saturday": "суббота",
        "sunday": "воскресенье"
    }
    today_rus = weekday_map.get(today_weekday, "")

    existing_tasks_today = [t.get("Задача") for t in tasks_ws.get_all_records() if t.get("Дата") == today_str]

    for row in repeat_ws.get_all_records():
        if (row.get("День недели") or "").strip().lower() == today_rus:
            if row.get("Задача") and row.get("Задача") not in existing_tasks_today:
                add_task(today_str,
                         row.get("Категория", ""),
                         row.get("Подкатегория", ""),
                         row.get("Задача", ""),
                         row.get("Время", ""),
                         "", "повтор")

# ====== КНОПКИ ======
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя")
    return markup

def week_days_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    today = datetime.now()
    days_map = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    for i in range(7):
        day_date = today + timedelta(days=i)
        btn_text = f"{days_map[day_date.weekday()]} ({day_date.strftime('%d.%m.%Y')})"
        markup.add(btn_text)
    markup.add("⬅ Назад")
    return markup

# ====== ОБРАБОТЧИКИ ======
@bot.message_handler(commands=["start"])
def start_cmd(message):
    bot.send_message(message.chat.id, "Добро пожаловать! Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today_tasks(message):
    user_id = message.chat.id
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = get_tasks_for_date(user_id, today)
    if tasks:
        text = f"📅 Задачи на {today}:\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"{i}. {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(user_id, text)
    else:
        bot.send_message(user_id, "Сегодня задач нет.")

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week_menu(message):
    bot.send_message(message.chat.id, "Выберите день недели:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def all_week_tasks(message):
    user_id = message.chat.id
    tasks = get_tasks_for_week(user_id)
    if tasks:
        text = "🗓 Все задачи на неделю:\n\n"
        for t in tasks:
            text += f"{t.get('Дата','')}: {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(user_id, text)
    else:
        bot.send_message(user_id, "На неделю задач нет.")

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text)
def day_tasks(message):
    user_id = message.chat.id
    date_str = message.text.split("(")[1].strip(")")
    tasks = get_tasks_for_date(user_id, date_str)
    if tasks:
        text = f"📅 Задачи на {date_str}:\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"{i}. {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(user_id, text)
    else:
        bot.send_message(user_id, "В этот день задач нет.")

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=main_menu())

# ====== ВЕЧЕРНИЙ ОТЧЕТ ======
def send_evening_report():
    users = get_users()
    today = datetime.now().strftime("%d.%m.%Y")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    for user in users:
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            done = [t for t in tasks if (t.get("Статус") or "").lower() == "выполнено"]
            undone = [t for t in tasks if not (t.get("Статус") or "").lower() == "выполнено"]
            text = f"🌙 Итог за {today}:\n\n"
            for t in done:
                text += f"✅ {t.get('Задача','')}\n"
            for t in undone:
                text += f"🔄 Перенос: {t.get('Задача','')}\n"
                add_task(
                    tomorrow,
                    t.get("Категория", ""),
                    t.get("Подкатегория", ""),
                    t.get("Задача", ""),
                    t.get("Дедлайн", ""),
                    user["id"],
                    "",  # сброс статуса
                    t.get("Повторяемость", "")
                )
            try:
                bot.send_message(user["id"], text)
            except Exception as e:
                print(f"Ошибка при отправке вечернего отчета пользователю {user['id']}: {e}")

# ====== ФУНКЦИИ ЗАПУСКА ======
def run_scheduler():
    schedule.every().day.at("09:00").do(lambda: print("Рассылка утренних задач"))
    schedule.every().day.at("19:00").do(send_evening_report)
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_bot():
    bot.polling(none_stop=True)

# Flask сервер для Render
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    run_bot()
