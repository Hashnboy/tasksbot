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

# ====== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ======
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")
repeat_ws = sh.worksheet("Повторяющиеся задачи")

# ====== ПОЛУЧЕНИЕ ПОЛЬЗОВАТЕЛЕЙ ======
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

# ====== ДОБАВЛЕНИЕ НОВОГО ПОЛЬЗОВАТЕЛЯ ======
def add_new_user(user_id, username):
    users = get_users()
    if not any(u["id"] == str(user_id) for u in users):
        users_ws.append_row([username, str(user_id), ""])  # Имя, ID, Категории
        print(f"Добавлен новый пользователь: {username} ({user_id})")

# ====== ПОЛУЧЕНИЕ ЗАДАЧ ======
def get_tasks_for_date(user_id, date_str):
    tasks = []
    rows = tasks_ws.get_all_records()
    for row in rows:
        if row.get("Дата") == date_str and str(row.get("Telegram ID")) == str(user_id):
            tasks.append(row)
    return tasks

# ====== ДОБАВЛЕНИЕ ЗАДАЧ ======
def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, str(user_id)])

# ====== ОБРАБОТКА ПОВТОРЯЮЩИХСЯ ЗАДАЧ ======
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

    for row in repeat_ws.get_all_records():
        if (row.get("День недели") or "").strip().lower() == today_rus:
            add_task(today_str,
                     row.get("Категория", ""),
                     row.get("Подкатегория", ""),
                     row.get("Задача", ""),
                     row.get("Время", ""),
                     row.get("Telegram ID", ""),  # теперь задачи привязаны к ID
                     "", "повтор")

# ====== ОТПРАВКА ПЛАНА ======
def send_daily_plan():
    process_repeating_tasks()
    today = datetime.now().strftime("%d.%m.%Y")
    for user in get_users():
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            text = f"📅 План на {today}:\n\n"
            for i, t in enumerate(tasks, 1):
                status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
                text += f"{status_icon} {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
            bot.send_message(user["id"], text)

# ====== КОМАНДА /today ======
@bot.message_handler(commands=['today'])
def today_tasks(message):
    user_id = message.chat.id
    add_new_user(user_id, message.from_user.first_name)
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = get_tasks_for_date(user_id, today)
    if not tasks:
        bot.send_message(user_id, "📭 На сегодня задач нет!")
        return
    text = f"📅 Ваши задачи на сегодня:\n\n"
    for i, t in enumerate(tasks, 1):
        status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
        text += f"{status_icon} {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
    bot.send_message(user_id, text)

# ====== КОМАНДА /week ======
@bot.message_handler(commands=['week'])
def week_menu(message):
    add_new_user(message.chat.id, message.from_user.first_name)
    today = datetime.now()
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    
    for i in range(7):
        day_date = today + timedelta(days=i)
        day_name_rus = day_date.strftime("%A")
        day_name_rus = {
            "Monday": "Понедельник",
            "Tuesday": "Вторник",
            "Wednesday": "Среда",
            "Thursday": "Четверг",
            "Friday": "Пятница",
            "Saturday": "Суббота",
            "Sunday": "Воскресенье"
        }[day_name_rus]
        day_str = day_date.strftime("%d.%m.%Y")
        btn = types.InlineKeyboardButton(f"{day_name_rus} ({day_str})", callback_data=f"day_{day_str}")
        keyboard.add(btn)
    
    keyboard.add(types.InlineKeyboardButton("📅 Вся неделя", callback_data="week_all"))
    bot.send_message(message.chat.id, "Выбери день или всю неделю:", reply_markup=keyboard)

# ====== ОБРАБОТКА КНОПОК ======
@bot.callback_query_handler(func=lambda call: call.data.startswith("day_") or call.data == "week_all")
def callback_week(call):
    user_id = str(call.message.chat.id)
    if call.data.startswith("day_"):
        date_str = call.data.replace("day_", "")
        tasks = get_tasks_for_date(user_id, date_str)
        if not tasks:
            bot.send_message(user_id, f"📭 На {date_str} задач нет!")
            return
        text = f"📅 Ваши задачи на {date_str}:\n\n"
        for i, t in enumerate(tasks, 1):
            status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
            text += f"{status_icon} {i}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        bot.send_message(user_id, text)
    elif call.data == "week_all":
        today = datetime.now()
        text = "📅 Ваши задачи на неделю:\n\n"
        has_tasks = False
        for i in range(7):
            date_str = (today + timedelta(days=i)).strftime("%d.%m.%Y")
            tasks = get_tasks_for_date(user_id, date_str)
            if tasks:
                has_tasks = True
                text += f"\n📆 {date_str}:\n"
                for j, t in enumerate(tasks, 1):
                    status_icon = "✅" if (t.get("Статус") or "").lower() == "выполнено" else "⬜"
                    text += f"{status_icon} {j}. [{t.get('Категория','')} - {t.get('Подкатегория','')}] {t.get('Задача','')} (до {t.get('Дедлайн','')})\n"
        if not has_tasks:
            text = "📭 На неделю задач нет!"
        bot.send_message(user_id, text)

# ====== СТАРТ ======
@bot.message_handler(commands=['start'])
def start(message):
    add_new_user(message.chat.id, message.from_user.first_name)
    bot.send_message(message.chat.id, "Привет! Я бот задач.\nКоманды:\n/today - задачи на сегодня\n/week - задачи на неделю")

# ====== ЗАПУСК ======
def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_bot():
    bot.remove_webhook()
    bot.polling(none_stop=True)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
