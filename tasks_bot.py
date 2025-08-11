import telebot
import gspread
import schedule
import time
import threading
from datetime import datetime, timedelta
from flask import Flask

# ====== НАСТРОЙКИ ======
API_TOKEN = "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4"  # замените на свой токен
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
    users = get_users()
    user_info = next((u for u in users if u["id"] == str(user_id)), None)
    for row in rows:
        if row.get("Дата") == date_str:
            if not user_info or not user_info["categories"] or row.get("Категория") in user_info["categories"]:
                tasks.append(row)
    return tasks

def add_task(date, category, subcategory, task, deadline, status="", repeat=""):
    # Добавляем новую строку в конец листа
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
        dow_raw = (row.get("День недели") or "").strip().lower()
        if dow_raw not in weekday_to_int:
            continue
        target_weekday = weekday_to_int[dow_raw]
        days_ahead = (target_weekday - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        task_date = (today + timedelta(days=days_ahead)).strftime("%d.%m.%Y")
        existing_tasks = [t.get("Задача") for t in tasks_ws.get_all_records() if t.get("Дата") == task_date]
        if row.get("Задача") and row.get("Задача") not in existing_tasks:
            add_task(task_date,
                     row.get("Категория", ""),
                     row.get("Подкатегория", ""),
                     row.get("Задача", ""),
                     row.get("Время", ""),
                     "", "повтор")

def send_daily_plan():
    # Сначала добавляем повторяющиеся задачи на сегодня и планируем следующие
    process_repeating_tasks()
    schedule_next_repeat_tasks()

    users = get_users()
    today = datetime.now().strftime("%d.%m.%Y")
    for user in users:
        tasks = get_tasks_for_date(user["id"], today)
        if tasks:
            text = f"📅 План на {today}:\n\n"
            for i, t in enumerate(tasks, 1):
                status = (t.get("Статус") or "").lower()
                status_icon = "✅" if status == "выполнено" else "⬜"
                cat = t.get("Категория", "")
                sub = t.get("Подкатегория", "")
                desc = t.get("Задача", "")
                deadline = t.get("Дедлайн", "")
                text += f"{status_icon} {i}. [{cat} - {sub}] {desc} (до {deadline})\n"
            try:
                bot.send_message(user["id"], text)
            except Exception as e:
                print(f"Ошибка при отправке плана пользователю {user['id']}: {e}")

def send_reminders():
    now = datetime.now()
    users = get_users()
    for user in users:
        tasks = get_tasks_for_date(user["id"], datetime.now().strftime("%d.%m.%Y"))
        for t in tasks:
            status = (t.get("Статус") or "").lower()
            if status != "выполнено":
                try:
                    dl = t.get("Дедлайн") or ""
                    if not dl:
                        continue
                    deadline = datetime.strptime(dl, "%H:%M")
                    deadline_today = now.replace(hour=deadline.hour, minute=deadline.minute, second=0, microsecond=0)
                    if 0 <= (deadline_today - now).total_seconds() <= 1800:
                        bot.send_message(user["id"], f"⚠️ Напоминание: {t.get('Задача','')} (до {dl})")
                except Exception:
                    # некорректный формат времени — пропускаем
                    continue

def send_evening_report():
    users = get_users()
    today = datetime.now().strftime("%d.%m.%Y")
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
                # переносим на завтра (сохраняем повторяемость)
                add_task((datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y"),
                         t.get("Категория", ""),
                         t.get("Подкатегория", ""),
                         t.get("Задача", ""),
                         t.get("Дедлайн", ""),
                         "", t.get("Повторяемость", ""))
            try:
                bot.send_message(user["id"], text)
            except Exception as e:
                print(f"Ошибка при отправке вечернего отчета пользователю {user['id']}: {e}")

# ====== КОМАНДЫ ======
@bot.message_handler(commands=['done'])
def mark_done(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.send_message(message.chat.id, "Используй формат: /done 1")
            return
        task_num = int(parts[1]) - 1
        user_id = str(message.chat.id)
        tasks = get_tasks_for_date(user_id, datetime.now().strftime("%d.%m.%Y"))
        if 0 <= task_num < len(tasks):
            # находим первую ячейку с таким описанием (может быть несколько — берём первую)
            task_desc = tasks[task_num].get("Задача")
            cell = tasks_ws.find(task_desc)
            if cell:
                # колонка Статус — 6 (считаем: Дата(1),Категория(2),Подкат(3),Задача(4),Дедлайн(5),Статус(6))
                tasks_ws.update_cell(cell.row, 6, "выполнено")
                bot.send_message(user_id, f"✅ Задача '{task_desc}' отмечена как выполненная!")
            else:
                bot.send_message(user_id, "Не удалось найти задачу в таблице.")
        else:
            bot.send_message(user_id, "❌ Неверный номер задачи")
    except Exception:
        bot.send_message(message.chat.id, "Используй формат: /done 1")

# ====== ФУНКЦИИ ЗАПУСКА ======
def run_scheduler():
    schedule.every().day.at("09:00").do(send_daily_plan)
    schedule.every(10).minutes.do(send_reminders)
    schedule.every().day.at("19:00").do(send_evening_report)
    while True:
        schedule.run_pending()
        time.sleep(1)

def run_bot():
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print(f"Ошибка polling: {e}")
        time.sleep(5)
        # попытка перезапуска в случае падения
        run_bot()

# Flask веб-сервер для Render
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # Запуск планировщика задач в отдельном потоке
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Запуск Telegram-бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Flask-сервер для Render
    app.run(host="0.0.0.0", port=5000)
