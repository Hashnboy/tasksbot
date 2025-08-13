# tasks_bot.py
import os
import re
import json
import time
import pytz
import gspread
import logging
import schedule
import threading
from datetime import datetime, timedelta
from flask import Flask, request, abort
from telebot import TeleBot, types

# =========================
# НАСТРОЙКИ / ОКРУЖЕНИЕ
# =========================
API_TOKEN = os.getenv("API_TOKEN")  # TELEGRAM BOT TOKEN
TABLE_URL = os.getenv("TABLE_URL")  # GOOGLE SHEETS URL
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # OpenAI
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/etc/secrets/credentials.json")

# Публичный URL сервиса (Render → Settings → Environment → BASE_URL)
BASE_URL = os.getenv("BASE_URL", "https://tasksbot-hy3t.onrender.com")
WEBHOOK_URL = f"{BASE_URL}/{API_TOKEN}"

# Часовой пояс
TZ_NAME = os.getenv("BOT_TZ", "Europe/Moscow")
TZ = pytz.timezone(TZ_NAME)

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("tasksbot")

# =========================
# ПРОВЕРКИ СЕКРЕТОВ
# =========================
if not API_TOKEN:
    log.error("API_TOKEN отсутствует в переменных окружения")
    raise SystemExit(1)
if not TABLE_URL:
    log.error("TABLE_URL отсутствует в переменных окружения")
    raise SystemExit(1)

# =========================
# TELEGRAM BOT + FLASK
# =========================
bot = TeleBot(API_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# =========================
# GOOGLE SHEETS
# Задачи: Дата | Категория | Подкатегория | Задача | Дедлайн | Статус | Повтор | User ID
# Пользователи: Имя | Telegram ID | Категории задач
# Повторяющиеся задачи: Описание | Категория | Подкатегория | Время | Правило | User ID
# =========================
try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)
    tasks_ws = sh.worksheet("Задачи")
    users_ws = sh.worksheet("Пользователи")
    try:
        repeat_ws = sh.worksheet("Повторяющиеся задачи")
    except Exception:
        repeat_ws = None
        log.warning("Лист 'Повторяющиеся задачи' не найден — повторяемость отключена.")
except Exception as e:
    log.exception("Ошибка подключения к Google Sheets")
    raise

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================
def today_str():
    return datetime.now(TZ).strftime("%d.%m.%Y")

def week_dates(start: datetime = None):
    base = start or datetime.now(TZ)
    return [(base + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]

def to_weekday_ru(dt: datetime):
    wd = dt.weekday()
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[wd]

def get_users():
    users = []
    for row in users_ws.get_all_records():
        tid = str(row.get("Telegram ID") or "").strip()
        if not tid:
            continue
        raw = str(row.get("Категории задач") or "").strip()
        cats = [c.strip() for c in raw.split(",")] if raw else []
        users.append({"name": row.get("Имя",""), "id": tid, "categories": cats})
    return users

def get_tasks_for_date(user_id, date_str_):
    res = []
    for row in tasks_ws.get_all_records():
        if str(row.get("User ID")) == str(user_id) and str(row.get("Дата")) == date_str_:
            res.append(row)
    return res

def get_tasks_for_week(user_id):
    dates = week_dates()
    res = []
    for row in tasks_ws.get_all_records():
        if str(row.get("User ID")) == str(user_id) and str(row.get("Дата")) in dates:
            res.append(row)
    return res

def add_task(date, category, subcategory, task, deadline, user_id, status="", repeat=""):
    date = date or ""
    category = category or ""
    subcategory = subcategory or ""
    task = task or ""
    deadline = deadline or ""
    status = status or ""
    repeat = repeat or ""
    user_id = str(user_id) if user_id is not None else ""
    tasks_ws.append_row(
        [date, category, subcategory, task, deadline, status, repeat, user_id],
        value_input_option="USER_ENTERED"
    )

def format_tasks_grouped(date_title, tasks):
    if not tasks:
        return f"📅 Задачи на {date_title}\n\nПусто — отдыхаем 😎"
    grouped = {}
    for t in tasks:
        cat = (t.get("Категория") or "Без категории").strip()
        sub = (t.get("Подкатегория") or "—").strip()
        grouped.setdefault(cat, {}).setdefault(sub, []).append(t)

    parts = [f"📅 Задачи на <b>{date_title}</b>\n"]
    for cat, subs in grouped.items():
        parts.append(f"📂 <b>{cat}</b>")
        for sub, items in subs.items():
            parts.append(f"  └ <i>{sub}</i>")
            for it in items:
                status = (it.get("Статус") or "").lower()
                done = "✅" if status == "выполнено" else "⬜"
                dl = it.get("Дедлайн") or ""
                rep = (it.get("Повтор") or "").strip()
                sticker = " 🔁" if rep else ""
                parts.append(f"    {done} {it.get('Задача','')}{sticker}  (до {dl})")
        parts.append("")
    return "\n".join(parts).strip()

def format_week_all(user_id):
    out = ["🗓 <b>Все задачи на неделю</b>\n"]
    base = datetime.now(TZ)
    for i in range(7):
        d = base + timedelta(days=i)
        d_str = d.strftime("%d.%m.%Y")
        day_name = to_weekday_ru(d)
        tasks = get_tasks_for_date(user_id, d_str)
        out.append(f"• <b>{day_name}</b> — {d_str}")
        if tasks:
            out.append("")
            out.append(format_tasks_grouped(d_str, tasks))
        else:
            out.append("  Пусто")
        out.append("")
    return "\n".join(out)

def week_days_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    base = datetime.now(TZ)
    for i in range(7):
        d = base + timedelta(days=i)
        btn_text = f"{to_weekday_ru(d)} ({d.strftime('%d.%m.%Y')})"
        markup.add(btn_text)
    markup.add("🗓 Вся неделя", "⬅ Назад")
    return markup

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📅 Сегодня", "📆 Неделя")
    markup.add("🗓 Вся неделя", "➕ Добавить задачу")
    markup.add("✅ Отметить выполнение")
    markup.add("🤖 Спросить ассистента")
    return markup

# =========================
# GPT-АССИСТЕНТ (NLP)
# =========================
def ai_parse_intent(user_text):
    """
    Возвращает JSON:
    { "action": "mark_done|add_task|list_today|list_date|list_week|unknown",
      "date": "ДД.ММ.ГГГГ"|null,
      "task": "...", "category": "...", "subcategory": "...", "deadline": "ЧЧ:ММ"|null }
    """
    if not OPENAI_API_KEY:
        low = user_text.lower()
        if "выполн" in low:
            return {"action": "mark_done", "task": user_text, "date": None, "category": None, "subcategory": None, "deadline": None}
        if "добав" in low or "создай" in low:
            return {"action": "add_task", "task": user_text, "date": None, "category": None, "subcategory": None, "deadline": None}
        if "недел" in low:
            return {"action": "list_week", "date": None, "task": None, "category": None, "subcategory": None, "deadline": None}
        if "сегодн" in low:
            return {"action": "list_today", "date": today_str(), "task": None, "category": None, "subcategory": None, "deadline": None}
        m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", user_text)
        if m:
            return {"action": "list_date", "date": m.group(1), "task": None, "category": None, "subcategory": None, "deadline": None}
        return {"action": "unknown", "date": None, "task": None, "category": None, "subcategory": None, "deadline": None}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        system_msg = (
            "Ты ассистент по задачам. Парсишь намерение пользователя. "
            "Отвечай строго JSON без пояснений. Ключи: "
            "action (mark_done|add_task|list_today|list_date|list_week|unknown), "
            "date (ДД.ММ.ГГГГ или null), task, category, subcategory, deadline (ЧЧ:ММ или null). "
            "Если пользователь говорит 'я выполнил …' — action=mark_done, task=название. "
            "Если 'добавь … завтра в 14:00' — action=add_task, date=завтра, deadline=14:00."
        )
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_text}
            ],
            temperature=0.2
        )
        raw = resp.choices[0].message.content.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        for k in ["action","date","task","category","subcategory","deadline"]:
            data.setdefault(k, None)
        return data
    except Exception as e:
        log.error("AI error: %s", e, exc_info=True)
        return {"action": "unknown", "date": None, "task": None, "category": None, "subcategory": None, "deadline": None}

def find_task_row(user_id, date_str_, task_substr):
    rows = tasks_ws.get_all_records()
    for idx, row in enumerate(rows, start=2):  # 1 — заголовок
        if str(row.get("User ID")) != str(user_id):
            continue
        if date_str_ and str(row.get("Дата")) != date_str_:
            continue
        title = row.get("Задача") or ""
        if task_substr.lower() in title.lower():
            return idx, row
    return None, None

# =========================
# ПОВТОРЯЮЩИЕСЯ ЗАДАЧИ
# =========================
def process_repeating_tasks_for_today():
    if not repeat_ws:
        return
    today = datetime.now(TZ)
    today_d = today.strftime("%d.%m.%Y")
    rows = repeat_ws.get_all_records()

    for r in rows:
        rule = (r.get("Правило") or "").strip().lower()
        desc = (r.get("Описание") or "").strip()
        cat = (r.get("Категория") or "").strip()
        sub = (r.get("Подкатегория") or "").strip()
        time_str = (r.get("Время") or "").strip()
        uid = str(r.get("User ID") or "").strip()
        if not uid or not desc:
            continue

        def add_if_not_exists(target_date):
            day_tasks = get_tasks_for_date(uid, target_date)
            if any((t.get("Задача") or "").strip().lower() == desc.lower() for t in day_tasks):
                return
            add_task(target_date, cat, sub, desc, time_str, uid, status="", repeat=rule)

        if rule == "каждый день":
            add_if_not_exists(today_d)
        elif rule.startswith("каждый"):
            parts = [p.strip() for p in rule.replace("каждый", "").split(",")]
            weekdays_ru = {
                "понедельник": 0, "вторник": 1, "среда": 2,
                "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6
            }
            if weekdays_ru.get(to_weekday_ru(today).lower()) in [weekdays_ru.get(p) for p in parts if p in weekdays_ru]:
                add_if_not_exists(today_d)
        elif rule == "завтра и послезавтра":
            z = (today + timedelta(days=1)).strftime("%d.%m.%Y")
            pz = (today + timedelta(days=2)).strftime("%d.%m.%Y")
            add_if_not_exists(z)
            add_if_not_exists(pz)
        elif rule.startswith("через"):
            m = re.search(r"через\s+(\d+)", rule)
            if m:
                add_if_not_exists(today_d)
        else:
            continue

# =========================
# РАССЫЛКА В 09:00
# =========================
def send_daily_plan():
    try:
        if repeat_ws:
            process_repeating_tasks_for_today()
    except Exception as e:
        log.error("Ошибка повторяющихся задач: %s", e, exc_info=True)

    date_title = today_str()
    for u in get_users():
        uid = u["id"]
        tasks = get_tasks_for_date(uid, date_title)
        msg = format_tasks_grouped(date_title, tasks)
        try:
            bot.send_message(uid, msg, reply_markup=main_menu())
        except Exception as e:
            log.warning("Не удалось отправить план %s: %s", uid, e)

def run_scheduler():
    schedule.clear()
    schedule.every().day.at("09:00").do(send_daily_plan)
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            log.error("Scheduler error: %s", e, exc_info=True)
            time.sleep(2)

# =========================
# ХЕНДЛЕРЫ / КНОПКИ
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я твой ассистент по задачам. Выбирай действие ниже или напиши мне естественным языком (например: «Я выполнил заказ Федя»).",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "📅 Сегодня")
def today_tasks(message):
    uid = message.chat.id
    date_title = today_str()
    tasks = get_tasks_for_date(uid, date_title)
    bot.send_message(uid, format_tasks_grouped(date_title, tasks), reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "📆 Неделя")
def week_menu(message):
    bot.send_message(message.chat.id, "Выбери день:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda m: m.text == "🗓 Вся неделя")
def all_week(message):
    uid = message.chat.id
    bot.send_message(uid, format_week_all(uid), reply_markup=main_menu())

@bot.message_handler(func=lambda m: "(" in (m.text or "") and ")" in (m.text or ""))
def day_tasks(message):
    uid = message.chat.id
    try:
        date_str_ = message.text.split("(")[1].split(")")[0].strip()
    except Exception:
        return
    tasks = get_tasks_for_date(uid, date_str_)
    try:
        d = datetime.strptime(date_str_, "%d.%m.%Y")
        title = f"{to_weekday_ru(d)} — {date_str_}"
    except Exception:
        title = date_str_
    bot.send_message(uid, format_tasks_grouped(title, tasks), reply_markup=main_menu())

# Добавление задач (мастер)
user_steps = {}
temp_task_data = {}

@bot.message_handler(func=lambda m: m.text == "➕ Добавить задачу")
def add_task_start(message):
    uid = message.chat.id
    user_steps[uid] = "date"
    temp_task_data[uid] = {}
    bot.send_message(uid, "Введите дату (ДД.ММ.ГГГГ):")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "date")
def add_task_date(message):
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", message.text.strip()):
        bot.send_message(message.chat.id, "❌ Формат даты неверный. Пример: 13.08.2025")
        return
    temp_task_data[message.chat.id]["date"] = message.text.strip()
    user_steps[message.chat.id] = "category"
    bot.send_message(message.chat.id, "Категория:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "category")
def add_task_category(message):
    temp_task_data[message.chat.id]["category"] = message.text.strip()
    user_steps[message.chat.id] = "subcategory"
    bot.send_message(message.chat.id, "Подкатегория:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "subcategory")
def add_task_subcategory(message):
    temp_task_data[message.chat.id]["subcategory"] = message.text.strip()
    user_steps[message.chat.id] = "title"
    bot.send_message(message.chat.id, "Текст задачи:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "title")
def add_task_title(message):
    temp_task_data[message.chat.id]["task"] = message.text.strip()
    user_steps[message.chat.id] = "deadline"
    bot.send_message(message.chat.id, "Дедлайн (ЧЧ:ММ), можно пусто:")

@bot.message_handler(func=lambda m: user_steps.get(m.chat.id) == "deadline")
def add_task_deadline(message):
    uid = message.chat.id
    dl = message.text.strip()
    if dl and not re.match(r"^\d{2}:\d{2}$", dl):
        bot.send_message(uid, "❌ Формат времени неверный. Пример: 14:30. Или оставьте пусто.")
        return
    data = temp_task_data.get(uid, {})
    add_task(
        data.get("date"), data.get("category"), data.get("subcategory"),
        data.get("task"), dl, uid, status="", repeat=""
    )
    bot.send_message(uid, "✅ Задача добавлена!", reply_markup=main_menu())
    temp_task_data.pop(uid, None)
    user_steps.pop(uid, None)

# Отметка выполнения (мастер)
mark_steps = {}
@bot.message_handler(func=lambda m: m.text == "✅ Отметить выполнение")
def mark_start(message):
    uid = message.chat.id
    date_title = today_str()
    tasks = get_tasks_for_date(uid, date_title)
    if not tasks:
        bot.send_message(uid, "На сегодня задач нет.", reply_markup=main_menu())
        return
    lines = [f"📋 Выберите номер задачи на {date_title}:"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t.get('Задача','')} (до {t.get('Дедлайн','')})")
    bot.send_message(uid, "\n".join(lines))
    mark_steps[uid] = {"date": date_title, "await": True}

@bot.message_handler(func=lambda m: mark_steps.get(m.chat.id, {}).get("await") is True and re.match(r"^\d+$", (m.text or "").strip()))
def mark_pick(message):
    uid = message.chat.id
    info = mark_steps.get(uid, {})
    date_title = info.get("date")
    tasks = get_tasks_for_date(uid, date_title)
    idx = int((message.text or "0").strip()) - 1
    if idx < 0 or idx >= len(tasks):
        bot.send_message(uid, "Неверный номер. Отмена.", reply_markup=main_menu())
        mark_steps.pop(uid, None)
        return
    task_title = tasks[idx].get("Задача") or ""
    row_index, _ = find_task_row(uid, date_title, task_title)
    if not row_index:
        bot.send_message(uid, "Не нашёл задачу. Попробуйте снова.", reply_markup=main_menu())
        mark_steps.pop(uid, None)
        return
    tasks_ws.update_cell(row_index, 6, "выполнено")
    bot.send_message(uid, f"✅ Готово: <b>{task_title}</b>", reply_markup=main_menu())
    mark_steps.pop(uid, None)

# GPT свободный вопрос
@bot.message_handler(func=lambda m: m.text == "🤖 Спросить ассистента")
def ask_ai_prompt(message):
    bot.send_message(
        message.chat.id,
        "Спроси меня о задачах: «я выполнил заказ федя», «добавь задачу завтра в 14:00», «покажи задачи на пятницу».\nНапиши сообщение:"
    )

# Общий NLP-хендлер
@bot.message_handler(func=lambda m: True)
def ai_router(message):
    txt = (message.text or "").strip()
    if txt in ["📅 Сегодня", "📆 Неделя", "🗓 Вся неделя", "➕ Добавить задачу", "✅ Отметить выполнение", "🤖 Спросить ассистента", "⬅ Назад"]:
        return

    intent = ai_parse_intent(txt)
    uid = message.chat.id

    if intent["action"] == "list_week":
        bot.send_message(uid, format_week_all(uid), reply_markup=main_menu())
        return

    if intent["action"] == "list_today":
        d = today_str()
        bot.send_message(uid, format_tasks_grouped(d, get_tasks_for_date(uid, d)), reply_markup=main_menu())
        return

    if intent["action"] == "list_date" and intent.get("date"):
        d = intent["date"]
        try:
            dt = datetime.strptime(d, "%d.%m.%Y")
            title = f"{to_weekday_ru(dt)} — {d}"
        except Exception:
            title = d
        bot.send_message(uid, format_tasks_grouped(title, get_tasks_for_date(uid, d)), reply_markup=main_menu())
        return

    if intent["action"] == "mark_done" and intent.get("task"):
        d = intent.get("date") or today_str()
        row_index, row = find_task_row(uid, d, intent["task"])
        if not row_index:
            for d2 in week_dates():
                row_index, row = find_task_row(uid, d2, intent["task"])
                if row_index:
                    d = d2
                    break
        if not row_index:
            bot.send_message(uid, "Не нашёл такую задачу. Уточни название или дату.")
            return
        tasks_ws.update_cell(row_index, 6, "выполнено")
        bot.send_message(uid, f"✅ Задача отмечена как выполненная: <b>{row.get('Задача','')}</b>")
        return

    if intent["action"] == "add_task" and intent.get("task"):
        d = intent.get("date")
        if not d:
            m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", txt)
            if m:
                d = m.group(1)
        if not d:
            low = txt.lower()
            if "послезавтра" in low:
                d = (datetime.now(TZ) + timedelta(days=2)).strftime("%d.%m.%Y")
            elif "завтра" in low:
                d = (datetime.now(TZ) + timedelta(days=1)).strftime("%d.%m.%Y")
            else:
                d = today_str()

        deadline = intent.get("deadline")
        if not deadline:
            m = re.search(r"\b(\d{2}:\d{2})\b", txt)
            if m:
                deadline = m.group(1)

        add_task(
            d,
            intent.get("category") or "Общее",
            intent.get("subcategory") or "—",
            intent["task"],
            deadline or "",
            uid,
            status="",
            repeat=""
        )
        bot.send_message(uid, f"✅ Добавил задачу на {d}:\n• {intent['task']}" + (f" (до {deadline})" if deadline else ""))
        return

    bot.send_message(uid, "Я не понял запрос 🤔 Попробуй иначе или нажми кнопку в меню.", reply_markup=main_menu())

# =========================
# ВЕБХУК
# =========================
@app.route(f"/{API_TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        try:
            update = types.Update.de_json(request.get_data().decode("utf-8"))
        except Exception:
            abort(400)
        bot.process_new_updates([update])
        return "OK", 200
    return "Method Not Allowed", 405

@app.route("/")
def home():
    return "Bot is running!", 200

# =========================
# СТАРТ СЕРВЕРА
# =========================
def setup_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    ok = bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message"])
    if ok:
        log.info("Webhook set to %s", WEBHOOK_URL)
    else:
        log.error("Не удалось установить вебхук")

if __name__ == "__main__":
    setup_webhook()
    th = threading.Thread(target=run_scheduler, daemon=True)
    th.start()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
