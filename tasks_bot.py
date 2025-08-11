
import telebot
import gspread
import schedule
import time
import threading
from datetime import datetime, timedelta
from flask import Flask

# ====== НАСТРОЙКИ ======
API_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # замените на свой токен
TABLE_URL = "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing"
CREDENTIALS_FILE = "credentials.json"

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
        if row["Telegram ID"]:
            users.append({
                "name": row["Имя"],
                "id": str(row["Telegram ID"]),
                "categories": [c.strip() for c in row["Категории задач"].split(",")] if row["Категории задач"] else []
            })
    return users

def get_tasks_for_date(user_id, date_str):
    tasks = []
    rows = tasks_ws.get_all_records()
    users = get_users()
    user_info = next((u for u in users if u["id"] == str(user_id)), None)
    for row in rows:
        if row["Дата"] == date_str:
            if not user_info or not user_info["categories"] or row["Категория"] in user_info["categories"]:
                tasks.append(row)
    return tasks

def add_task(date, category, subcategory, task, deadline, status="", repeat=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat])

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
    today_rus = weekday_map[today_weekday]

    existing_tasks_today = [t["Задача"] for t in tasks_ws.get_all_records() if t["Дата"] == today_str]

    for row in repeat_ws.get_all_records():
        if row["День недели"].strip().lower() == today_rus:
            if row["Задача"] not in existing_tasks_today:
                add_task(today_str, row["Категория"], row["Подкатегория"], row["Задача"], row["Время"], "", "повтор")

def schedule_next_repeat_tasks():
    weekday_to_int = {
        "понедельник": 0,
        "вторник": 1,
        "среда": 2,
        "четверг": 3,
        "пятница": 4,
        "суббота": 5,
        "воскресенье": 6
    }
    today = datetime.now()
    for row in repeat_ws.get_all_records():
        target_weekday = weekday_to_int[row["День недели"].strip().lower()]
        days_ahead = (target_weekday - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        task_date = (today + timedelta(days=days_ahead)).strftime("%d.%m.%Y")
        existing_tasks = [t["Задача"] for t in tasks_ws.get_all_records() if t["Дата"] == task_date]
        if row["Задача"] not in existing_tasks:
            add_task(task_date, row["Категория"], row["Подкатегория"], row["Задача"], row["Время"], "", "повтор")

def send_daily_plan():
    process_repeating_tasks()
    schedule_next_repeat_tasks()

    users = get_users()
    today = datetime.now().strftime("%d.%m.%Y")
    for user in users:
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            text = f"📅 План на {today}:\n\n"

"
            for i, t in enumerate(tasks, 1):
                status_icon = "✅" if t["Статус"].lower() == "выполнено" else "⬜"
                text += f"{status_icon} {i}. [{t['Категория']} - {t['Подкатегория']}] {t['Задача']} (до {t['Дедлайн']})
"
            bot.send_message(user["id"], text)

def send_reminders():
    now = datetime.now()
    users = get_users()
    for user in users:
        tasks = get_tasks_for_date(user["id"], datetime.now().strftime("%d.%m.%Y"))
        for t in tasks:
            if not t["Статус"] or t["Статус"].lower() != "выполнено":
                try:
                    deadline = datetime.strptime(t["Дедлайн"], "%H:%M")
                    deadline_today = now.replace(hour=deadline.hour, minute=deadline.minute, second=0, microsecond=0)
                    if 0 <= (deadline_today - now).total_seconds() <= 1800:
                        bot.send_message(user["id"], f"⚠️ Напоминание: {t['Задача']} (до {t['Дедлайн']})")
                except:
                    pass

def send_evening_report():
    users = get_users()
    today = datetime.now().strftime("%d.%m.%Y")
    for user in users:
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            done = [t for t in tasks if t["Статус"].lower() == "выполнено"]
            undone = [t for t in tasks if not t["Статус"] or t["Статус"].lower() != "выполнено"]
            text = f"🌙 Итог за {today}:

"
            for t in done:
                text += f"✅ {t['Задача']}
"
            for t in undone:
                text += f"🔄 Перенос: {t['Задача']}
"
                add_task((datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y"), t["Категория"], t["Подкатегория"], t["Задача"], t["Дедлайн"], "", t["Повторяемость"])
            bot.send_message(user["id"], text)

@bot.message_handler(commands=['done'])
def mark_done(message):
    try:
        task_num = int(message.text.split()[1]) - 1
        user_id = str(message.chat.id)
        tasks = get_tasks_for_date(user_id, datetime.now().strftime("%d.%m.%Y"))
        if 0 <= task_num < len(tasks):
            cell = tasks_ws.find(tasks[task_num]["Задача"])
            tasks_ws.update_cell(cell.row, 6, "выполнено")
            bot.send_message(user_id, f"✅ Задача '{tasks[task_num]['Задача']}' выполнена!")
        else:
            bot.send_message(user_id, "❌ Неверный номер задачи")
    except:
        bot.send_message(user_id, "Используй формат: /done 1")

# ====== ФУНКЦИИ ЗАПУСКА ======
def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    schedule.every(10).minutes.do(send_reminders)
    schedule.every().day.at("19:00").do(send_evening_report)
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_bot():
    bot.polling(none_stop=True)

# Flask веб-сервер для Render
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=run_scheduler).start()
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=5000)
