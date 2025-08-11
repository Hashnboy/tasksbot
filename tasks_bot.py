import telebot
import gspread
import schedule
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import types

# ====== НАСТРОЙКИ ======
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"
WEBHOOK_URL = "https://tasksbot-hy3t.onrender.com"  # твой Render URL
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "/etc/secrets/credentials.json"

bot = telebot.TeleBot(API_TOKEN)

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")

# ====== ФУНКЦИИ ======
def get_users():
    users = []
    for row in users_ws.get_all_records():
        if row.get("Telegram ID"):
            users.append({
                "name": row.get("Имя", ""),
                "id": str(row.get("Telegram ID"))
            })
    return users

def get_tasks_for_date(user_id, date_str):
    return [
        row for row in tasks_ws.get_all_records()
        if row.get("Дата") == date_str and str(row.get("User ID")) == str(user_id)
    ]

def get_tasks_for_week(user_id):
    today = datetime.now()
    week_dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    return [
        row for row in tasks_ws.get_all_records()
        if row.get("Дата") in week_dates and str(row.get("User ID")) == str(user_id)
    ]

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

# ====== РАССЫЛКА НАПОМИНАНИЙ ======
def send_reminders():
    now = datetime.now()
    for user in get_users():
        tasks = get_tasks_for_date(user["id"], now.strftime("%d.%m.%Y"))
        for t in tasks:
            deadline = t.get("Дедлайн", "")
            if not deadline:
                continue
            try:
                dl_time = datetime.strptime(deadline, "%H:%M").time()
                if 0 <= (datetime.combine(now.date(), dl_time) - now).total_seconds() <= 1800:
                    bot.send_message(user["id"], f"⚠️ Напоминание: {t.get('Задача','')} (до {deadline})")
            except:
                continue

# ====== SCHEDULER ======
def run_scheduler():
    schedule.every(10).minutes.do(send_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ====== FLASK ДЛЯ WEBHOOK ======
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "Bot is running!"

@app.route(f"/{API_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{API_TOKEN}")

    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
