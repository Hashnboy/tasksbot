import os
import re
import json
import time
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import telebot
from telebot import types
from flask import Flask, request

import gspread
import schedule

# =========================
#        НАСТРОЙКИ
# =========================

# === Переменные окружения (рекомендуется на Render) ===
API_TOKEN = os.getenv("BOT_TOKEN", "7959600917:AAF7szpbvX8CoFObxjVb6y3aCiSceCi-Rt4")
TABLE_URL = os.getenv("TABLE_URL", "https://docs.google.com/spreadsheets/d/1lIV2kUx8sDHR1ynMB2di8j5n9rpj1ydhsmfjXJpRGeA/edit?usp=sharing")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "/etc/secrets/credentials.json")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "https://tasksbot-hy3t.onrender.com")
WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}"

# Часовой пояс для расписаний (без pytz)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Админы (через запятую список числовых Telegram ID)
ADMIN_IDS = {id_.strip() for id_ in os.getenv("ADMIN_IDS", "").split(",") if id_.strip()}

# Опционально: интеграция с OpenAI (ChatGPT)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# =========================
#         ЛОГИ
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("tasksbot")

# =========================
#     TELEGRAM & FLASK
# =========================

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# =========================
#   GOOGLE SHEETS CLIENT
# =========================

gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

def _safe_worksheet(sheet, title):
    try:
        return sheet.worksheet(title)
    except Exception as e:
        logger.warning("Лист '%s' не найден: %s", title, e)
        return None

tasks_ws = _safe_worksheet(sh, "Задачи")
users_ws = _safe_worksheet(sh, "Пользователи")
repeat_ws = _safe_worksheet(sh, "Повторяющиеся задачи")  # опционально

# Ожидаемая структура листа "Задачи":
# A: Дата (ДД.ММ.ГГГГ)
# B: Категория
# C: Подкатегория
# D: Задача
# E: Дедлайн (ЧЧ:ММ)
# F: Статус (выполнено / "")
# G: Повтор (например "повтор")
# H: User ID (число)

# =========================
#         КЕШ
# =========================

_cache = {
    "tasks": {"ts": 0, "data": []},
    "users": {"ts": 0, "data": []},
    "repeat": {"ts": 0, "data": []},
}
CACHE_TTL = 20  # секунд

def _cache_get(name):
    now = time.time()
    if now - _cache[name]["ts"] <= CACHE_TTL:
        return _cache[name]["data"]
    return None

def _cache_set(name, data):
    _cache[name]["ts"] = time.time()
    _cache[name]["data"] = data

def _invalidate_cache(*names):
    for n in names:
        if n in _cache:
            _cache[n]["ts"] = 0

# =========================
#   УТИЛИТЫ ДАТ/ВРЕМЕНИ
# =========================

RU_WEEKDAYS = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
RU_WEEKDAYS_L = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

def now_tz():
    return datetime.now(ZoneInfo(TIMEZONE))

def fmt_date(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")

def parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%d.%m.%Y")
    except Exception:
        return None

def day_name(date: datetime) -> str:
    return RU_WEEKDAYS[date.weekday()]

def next_7_dates(start: datetime) -> list[datetime]:
    return [start + timedelta(days=i) for i in range(7)]

# =========================
#     ДОСТУП К ДАННЫМ
# =========================

def get_users():
    if not users_ws:
        return []
    cached = _cache_get("users")
    if cached is not None:
        return cached
    rows = users_ws.get_all_records()
    users = []
    for r in rows:
        if r.get("Telegram ID"):
            cats = []
            raw = r.get("Категории задач") or ""
            if raw:
                cats = [c.strip() for c in raw.split(",") if c.strip()]
            users.append({
                "name": r.get("Имя", ""),
                "id": str(r.get("Telegram ID")),
                "categories": cats
            })
    _cache_set("users", users)
    return users

def get_all_tasks():
    if not tasks_ws:
        return []
    cached = _cache_get("tasks")
    if cached is not None:
        return cached
    data = tasks_ws.get_all_records()
    _cache_set("tasks", data)
    return data

def get_repeat_tasks():
    if not repeat_ws:
        return []
    cached = _cache_get("repeat")
    if cached is not None:
        return cached
    data = repeat_ws.get_all_records()
    _cache_set("repeat", data)
    return data

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    # При добавлении — сразу инвалидируем кеш
    if not tasks_ws:
        return
    tasks_ws.append_row([date, category, subcategory, task, deadline, status, repeat, str(user_id)])
    _invalidate_cache("tasks")

def mark_task_done_by_desc_for_user(user_id, date_str, index_in_list):
    # Находим задачу из "видимого" списка пользователя за день и помечаем как выполненную
    tasks = get_tasks_for_date(user_id, date_str)
    if not (0 <= index_in_list < len(tasks)):
        return False, "Неверный номер."
    desc = tasks[index_in_list].get("Задача", "")
    # Ищем первую подходящую ячейку с этим описанием + датой + user_id
    all_rows = tasks_ws.get_all_values()
    # Заголовки ожидаются в первой строке
    # Ищем по всем строкам
    for r_idx, row in enumerate(all_rows, start=1):
        if r_idx == 1:
            continue
        try:
            date_v = row[0].strip()
            desc_v = row[3].strip()
            user_v = row[7].strip() if len(row) >= 8 else ""
        except Exception:
            continue
        if date_v == date_str and desc_v == desc and user_v == str(user_id):
            # Статус — колонка F (6)
            tasks_ws.update_cell(r_idx, 6, "выполнено")
            _invalidate_cache("tasks")
            return True, desc
    return False, "Не нашёл задачу в таблице."

def get_tasks_for_date(user_id, date_str):
    result = []
    for r in get_all_tasks():
        if r.get("Дата") == date_str and str(r.get("User ID")) == str(user_id):
            result.append(r)
    return result

def get_tasks_for_week(user_id, start=None):
    if start is None:
        start = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    targets = {fmt_date(d) for d in next_7_dates(start)}
    out = []
    for r in get_all_tasks():
        if r.get("Дата") in targets and str(r.get("User ID")) == str(user_id):
            out.append(r)
    return out

# =========================
#   ПОВТОРЯЮЩИЕСЯ ЗАДАЧИ
# =========================

def process_repeating_for_date(date_dt: datetime):
    """Добавляет в 'Задачи' повторяющиеся задачи (если ещё нет) для указанной даты."""
    if not repeat_ws:
        return
    date_str = fmt_date(date_dt)
    weekday_rus = RU_WEEKDAYS_L[date_dt.weekday()]  # 'понедельник', ...
    today_existing = {(t.get("Задача",""), str(t.get("User ID",""))) for t in get_all_tasks() if t.get("Дата")==date_str}

    for row in get_repeat_tasks():
        # Ожидаемые поля: День недели, Категория, Подкатегория, Задача, Время, User ID (опционально — кому назначать)
        if (row.get("День недели") or "").strip().lower() != weekday_rus:
            continue
        task_desc = row.get("Задача", "").strip()
        if not task_desc:
            continue
        target_user = str(row.get("User ID") or "").strip()
        if not target_user:
            # если не указан — не создаём, чтобы не раскидывать всем
            continue
        # Защита от дублей
        if (task_desc, target_user) in today_existing:
            continue
        add_task(
            date_str,
            row.get("Категория", ""),
            row.get("Подкатегория", ""),
            task_desc,
            row.get("Время", ""),
            target_user,
            status="",
            repeat="повтор"
        )

def schedule_repeating_today_and_next():
    # На всякий случай прогоняем для сегодня
    today = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    process_repeating_for_date(today)

# =========================
#     ФОРМАТИРОВАНИЕ
# =========================

def pretty_day_block(user_id, date_dt):
    date_str = fmt_date(date_dt)
    tasks = get_tasks_for_date(user_id, date_str)
    if not tasks:
        return f"• <b>{day_name(date_dt)}</b> — {date_str}\n  <i>Нет задач</i>"

    # Группируем по категориям → подкатегориям
    grouped = defaultdict(lambda: defaultdict(list))
    for t in tasks:
        grouped[t.get("Категория","Без категории")][t.get("Подкатегория","Без подкатегории")].append(t)

    lines = [f"• <b>{day_name(date_dt)}</b> — {date_str}"]
    for cat in sorted(grouped.keys()):
        lines.append(f"\n<b>📂 {cat}</b>")
        for sub in sorted(grouped[cat].keys()):
            lines.append(f"  └ <u>{sub}</u>")
            for t in grouped[cat][sub]:
                status = (t.get("Статус") or "").lower()
                done = "✅ " if status == "выполнено" else "⬜ "
                pin = " 📌" if (t.get("Повтор") or "").strip() else ""
                deadline = t.get("Дедлайн","")
                title = t.get("Задача","")
                lines.append(f"    {done}{title}{pin}  <i>(до {deadline})</i>")
            lines.append("")  # пустая строка между группами
    return "\n".join(lines).strip()

def pretty_today(user_id):
    d = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    header = f"📅 <b>Задачи на {fmt_date(d)}</b>\n"
    body = pretty_day_block(user_id, d)
    return header + "\n" + body

def pretty_week(user_id):
    start = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    blocks = [pretty_day_block(user_id, d) for d in next_7_dates(start)]
    return "🗓 <b>Задачи на неделю</b>\n\n" + "\n\n".join(blocks)

# =========================
#          МЕНЮ
# =========================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя")
    kb.add("➕ Добавить задачу", "✅ Отметить выполнение")
    if OPENAI_API_KEY:
        kb.add("🤖 Помощь AI")
    return kb

def week_days_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    base = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    for d in next_7_dates(base):
        kb.add(f"{day_name(d)} ({fmt_date(d)})")
    kb.add("⬅ Назад")
    return kb

# =========================
#     ДОБАВЛЕНИЕ ЗАДАЧ
# =========================

user_steps = {}      # chat_id -> step
temp_task_data = {}  # chat_id -> dict

@bot.message_handler(func=lambda m: m.text == "➕ Добавить задачу")
def add_task_start(m):
    user_steps[m.chat.id] = "date"
    temp_task_data[m.chat.id] = {}
    bot.send_message(m.chat.id, "Введите дату в формате <b>ДД.ММ.ГГГГ</b>:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "date")
def add_task_date(m):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", m.text):
        bot.send_message(m.chat.id, "❌ Неверный формат. Пример: 12.08.2025")
        return
    temp_task_data[m.chat.id]["date"] = m.text
    user_steps[m.chat.id] = "category"
    bot.send_message(m.chat.id, "Категория:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "category")
def add_task_category(m):
    temp_task_data[m.chat.id]["category"] = m.text.strip() or "Без категории"
    user_steps[m.chat.id] = "subcategory"
    bot.send_message(m.chat.id, "Подкатегория:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "subcategory")
def add_task_subcategory(m):
    temp_task_data[m.chat.id]["subcategory"] = m.text.strip() or "Общее"
    user_steps[m.chat.id] = "title"
    bot.send_message(m.chat.id, "Описание задачи:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "title")
def add_task_title(m):
    temp_task_data[m.chat.id]["title"] = m.text.strip()
    user_steps[m.chat.id] = "deadline"
    bot.send_message(m.chat.id, "Дедлайн в формате <b>ЧЧ:ММ</b>:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "deadline")
def add_task_deadline(m):
    if not re.match(r"^\d{2}:\d{2}$", m.text.strip()):
        bot.send_message(m.chat.id, "❌ Неверный формат. Пример: 14:30")
        return
    data = temp_task_data[m.chat.id]
    data["deadline"] = m.text.strip()
    # Сохранение
    add_task(
        data["date"],
        data["category"],
        data["subcategory"],
        data["title"],
        data["deadline"],
        m.chat.id
    )
    bot.send_message(m.chat.id, "✅ Задача добавлена!", reply_markup=main_menu())
    user_steps.pop(m.chat.id, None)
    temp_task_data.pop(m.chat.id, None)

# =========================
#     ОТМЕТКА ВЫПОЛНЕНИЯ
# =========================

@bot.message_handler(func=lambda m: m.text == "✅ Отметить выполнение")
def ask_done_index(m):
    today = fmt_date(now_tz())
    tasks = get_tasks_for_date(m.chat.id, today)
    if not tasks:
        bot.send_message(m.chat.id, "Сегодня задач нет.")
        return
    # Покажем нумерованный список
    lines = [f"📋 Выберите номер задачи на {today}:"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t.get('Задача','')} (до {t.get('Дедлайн','')})")
    bot.send_message(m.chat.id, "\n".join(lines))
    user_steps[m.chat.id] = "done_wait_number"

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "done_wait_number")
def do_mark_done(m):
    try:
        idx = int(m.text.strip()) - 1
    except Exception:
        bot.send_message(m.chat.id, "Введите номер, например: 2")
        return
    today = fmt_date(now_tz())
    ok, info = mark_task_done_by_desc_for_user(m.chat.id, today, idx)
    if ok:
        bot.send_message(m.chat.id, f"✅ Задача «{info}» отмечена как выполненная!")
    else:
        bot.send_message(m.chat.id, f"❌ {info}")
    user_steps.pop(m.chat.id, None)

# =========================
#         КНОПКИ
# =========================

@bot.message_handler(commands=["start"])
def start_cmd(m):
    bot.send_message(m.chat.id, "Добро пожаловать! Выберите действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "📅 Сегодня")
def today_tasks(m):
    schedule_repeating_today_and_next()  # на всякий — подкинем повторяющиеся на сегодня
    bot.send_message(m.chat.id, pretty_today(m.chat.id))

@bot.message_handler(func=lambda m: m.text == "📆 Неделя")
def week_menu_handler(m):
    bot.send_message(m.chat.id, "Выберите день недели:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda m: m.text == "🗓 Вся неделя")
def all_week_tasks(m):
    schedule_repeating_today_and_next()
    bot.send_message(m.chat.id, pretty_week(m.chat.id))

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text and any(d in msg.text for d in RU_WEEKDAYS))
def day_tasks(m):
    try:
        date_str = m.text.split("(")[1].strip(")")
    except Exception:
        bot.send_message(m.chat.id, "Не удалось определить дату.")
        return
    if not parse_date(date_str):
        bot.send_message(m.chat.id, "Неверная дата.")
        return
    d = parse_date(date_str).replace(tzinfo=ZoneInfo(TIMEZONE))
    bot.send_message(m.chat.id, pretty_day_block(m.chat.id, d))

@bot.message_handler(func=lambda m: m.text == "⬅ Назад")
def back_to_main(m):
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# =========================
#    УТРЕННЯЯ РАССЫЛКА
# =========================

def send_daily_plan():
    try:
        # 1) Сначала создаём повторяющиеся задачи на сегодня
        schedule_repeating_today_and_next()

        today = fmt_date(now_tz())
        for u in get_users():
            uid = u["id"]
            tasks = get_tasks_for_date(uid, today)
            if not tasks:
                continue
            msg = pretty_today(uid)
            bot.send_message(uid, msg)
        logger.info("Утренняя рассылка отправлена")
    except Exception as e:
        logger.exception("Ошибка в send_daily_plan: %s", e)

def scheduler_loop():
    # 09:00 локального времени TIMEZONE
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        schedule.run_pending()
        time.sleep(1)

# =========================
#      ИНТЕГРАЦИЯ ChatGPT
# =========================

def ai_answer(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "AI недоступен: не задан OPENAI_API_KEY."
    try:
        # Используем официальную библиотеку openai>=1.0
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Ты помощник по личным задачам. Отвечай кратко и по делу."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("AI error: %s", e)
        return "Не удалось получить ответ от AI."

@bot.message_handler(commands=["ask"])
def ask_ai_cmd(m):
    q = m.text.replace("/ask", "", 1).strip()
    if not q:
        bot.send_message(m.chat.id, "Напишите вопрос после команды: <code>/ask Как распределить задачи?</code>")
        return
    bot.send_message(m.chat.id, "Думаю…")
    bot.send_message(m.chat.id, ai_answer(q))

@bot.message_handler(func=lambda m: m.text == "🤖 Помощь AI")
def ask_ai_button(m):
    bot.send_message(m.chat.id, "Отправьте ваш вопрос в следующем сообщении. Начните с <b>/ask</b> ...")

# =========================
#          WEBHOOK
# =========================

@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        logger.exception("Ошибка обработки вебхука: %s", e)
        return "ERR", 500
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!"

def ensure_webhook():
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        ok = bot.set_webhook(url=WEBHOOK_URL, allowed_updates=[
            "message", "callback_query"
        ])
        if ok:
            logger.info("Webhook установлен: %s", WEBHOOK_URL)
        else:
            logger.error("Не удалось установить webhook (без исключения).")
    except telebot.apihelper.ApiTelegramException as e:
        # Частая причина 401 — неправильный токен
        logger.exception("Ошибка Telegram set_webhook: %s", e)
    except Exception as e:
        logger.exception("Не удалось установить webhook: %s", e)

# =========================
#          MAIN
# =========================

if __name__ == "__main__":
    # Без polling — только webhook, чтобы исключить 409 конфликты
    ensure_webhook()

    # Параллельно поднимем планировщик
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # Flask-сервер для Telegram вебхука и Render health-check
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
