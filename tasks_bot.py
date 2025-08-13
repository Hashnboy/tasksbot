# tasks_bot.py
import os
import re
import json
import logging
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

import pytz
from flask import Flask, request
import telebot
from telebot import types
import gspread

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------
# Конфиг / окружение
# ---------------------------
API_TOKEN = os.getenv("API_TOKEN", "").strip()
TABLE_URL = os.getenv("TABLE_URL", "").strip()
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "/etc/secrets/credentials.json")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TZ_NAME = os.getenv("TZ", "UTC")

if not API_TOKEN or ":" not in API_TOKEN:
    raise RuntimeError("API_TOKEN пустой или некорректный")

if not TABLE_URL:
    raise RuntimeError("TABLE_URL не задан")

if not os.path.exists(CREDENTIALS_FILE):
    raise RuntimeError(f"Не найден файл cred: {CREDENTIALS_FILE}")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
log = logging.getLogger("tasks-bot")

# Часовой пояс
tz = pytz.timezone(TZ_NAME)

# ---------------------------
# Telegram bot (только webhook)
# ---------------------------
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

# ---------------------------
# Google Sheets
# ---------------------------
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)
tasks_ws = sh.worksheet("Задачи")
users_ws = sh.worksheet("Пользователи")

# Ожидаемая структура листа "Задачи":
# Дата | Категория | Подкатегория | Задача | Дедлайн | Статус | Повторяемость | User ID
# where: Статус: "выполнено" / "" ; Повторяемость: строка или шаблон; User ID: chat.id

# ---------------------------
# OpenAI (GPT ассистент)
# ---------------------------
from openai import OpenAI
oai_client = None
if OPENAI_API_KEY:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.error(f"OpenAI init error: {e}")

SYSTEM_PROMPT = """Ты — личный ассистент по задачам.
У тебя есть список задач пользователя (сегодня, неделя, категории).
Твоя цель — понять намерение и вернуть СТРОГО JSON с полями:
{
  "action": "...",        // one of: "list_today", "list_week", "list_day", "mark_done", "add_task", "reschedule", "help"
  "date": "ДД.ММ.ГГГГ",   // если нужно
  "task_query": "...",    // текст для поиска задачи (фрагмент)
  "category": "...",
  "subcategory": "...",
  "deadline": "ЧЧ:ММ",
  "repeat": "...",        // маркер повторяемости
  "free_text": "..."      // орг. описание
}
Если команда неясна, верни action="help".
Отвечай ТОЛЬКО JSON без комментариев.
Примеры:
"я выполнил заказ табака" => {"action":"mark_done","task_query":"заказ табака"}
"добавь завтра купить молоко в 10:00" => {"action":"add_task","date":"<завтрашняя дата>","task_query":"купить молоко","deadline":"10:00"}
"перенеси заказ кофе на пятницу 15:00" => {"action":"reschedule","date":"<ближайшая пятница>","task_query":"заказ кофе","deadline":"15:00"}
"""

def weekday_name_ru(dt: datetime) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

def week_dates(start: datetime) -> list:
    return [(start + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]

def normalize_date_str(s: str) -> str:
    # ожидаем ДД.ММ.ГГГГ
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", s):
        return s
    return ""

def get_users() -> list:
    items = []
    for row in users_ws.get_all_records():
        tid = str(row.get("Telegram ID") or "").strip()
        if tid:
            cats = []
            raw = (row.get("Категории задач") or "").strip()
            if raw:
                cats = [c.strip() for c in raw.split(",") if c.strip()]
            items.append({"id": tid, "name": row.get("Имя",""), "categories": cats})
    return items

def get_tasks_raw():
    return tasks_ws.get_all_records()

def filter_tasks_by_user(tasks, user_id):
    return [t for t in tasks if str(t.get("User ID")) == str(user_id)]

def tasks_for_date(user_id, date_str):
    rows = filter_tasks_by_user(get_tasks_raw(), user_id)
    return [r for r in rows if (r.get("Дата") == date_str)]

def tasks_for_week(user_id, start: datetime):
    dates = set(week_dates(start))
    rows = filter_tasks_by_user(get_tasks_raw(), user_id)
    return [r for r in rows if r.get("Дата") in dates]

def append_task(date_s, category, subcategory, task, deadline, user_id, status="", repeat=""):
    tasks_ws.append_row([date_s, category, subcategory, task, deadline, status, repeat, str(user_id)])

def find_first_cell_in_col(ws, col_idx, value):
    # аккуратный поиск в колонке: вернем первую ячейку с точным совпадением
    try:
        cells = ws.findall(value)
        for c in cells:
            if c.col == col_idx:
                return c
    except Exception:
        pass
    return None

def fuzzy_pick_task(rows, query):
    # простая "размытая" выборка по подстроке
    q = (query or "").strip().lower()
    if not q:
        return None
    scored = []
    for r in rows:
        desc = str(r.get("Задача",""))
        if q in desc.lower():
            # чем короче разница, тем лучше
            scored.append((len(desc) - len(q), r))
    scored.sort(key=lambda x: x[0])
    return scored[0][1] if scored else None

def format_tasks_grouped(rows, title_date: str = "") -> str:
    # Группировка по Категория/Подкатегория + красивые значки
    if not rows:
        return "Задач нет. Отдохни 😊"
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cat = r.get("Категория","Без категории") or "Без категории"
        sub = r.get("Подкатегория","—") or "—"
        groups[cat][sub].append(r)

    lines = []
    if title_date:
        # пример: • Среда — 13.08.2025
        try:
            dt = datetime.strptime(title_date, "%d.%m.%Y")
            lines.append(f"• <b>{weekday_name_ru(dt)}</b> — <b>{title_date}</b>\n")
        except Exception:
            lines.append(f"• <b>{title_date}</b>\n")

    for cat, subs in groups.items():
        lines.append(f"📂 <b>{cat}</b>")
        for sub, items in subs.items():
            lines.append(f"  └ <i>{sub}</i>")
            for t in items:
                status = (t.get("Статус","") or "").lower()
                icon = "✅" if status == "выполнено" else "⬜"
                dl = t.get("Дедлайн","") or ""
                rep = t.get("Повторяемость","") or ""
                rep_icon = " 🔁" if rep.strip() else ""
                lines.append(f"    {icon} {t.get('Задача','')}{rep_icon}  (до {dl})")
            lines.append("")  # пустая строка между подгруппами
        lines.append("")      # пустая строка между категориями
    return "\n".join(lines).strip()

def format_week_list(rows_by_date) -> str:
    if not rows_by_date:
        return "На неделю задач нет."
    out = []
    for date_s, items in rows_by_date:
        out.append(f"🗓 <b>{weekday_name_ru(datetime.strptime(date_s,'%d.%m.%Y'))}</b> — <b>{date_s}</b>")
        if not items:
            out.append("  • Нет задач\n")
            continue
        for i, t in enumerate(items, 1):
            rep_icon = " 🔁" if (t.get("Повторяемость","") or "").strip() else ""
            dl = t.get("Дедлайн","") or ""
            out.append(f"  {i}. {t.get('Задача','')}{rep_icon} (до {dl})")
        out.append("")  # пробел между днями
    return "\n".join(out).strip()

# ---------------------------
# Клавиатуры
# ---------------------------
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📅 Сегодня", "📆 Неделя")
    kb.add("🗓 Вся неделя", "➕ Добавить задачу")
    kb.add("ℹ️ Помощь")
    return kb

def week_days_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(7):
        d = today + timedelta(days=i)
        kb.add(f"{weekday_name_ru(d)} ({d.strftime('%d.%m.%Y')})")
    kb.add("⬅ Назад")
    return kb

# ---------------------------
# GPT разбор намерений
# ---------------------------
def gpt_intent(user_text: str, today_str: str, week_dates_list: list) -> dict:
    if not oai_client:
        # Fallback: без GPT — простейшие эвристики
        txt = user_text.lower()
        if "сегодня" in txt:
            return {"action":"list_today"}
        if "недел" in txt and "вся" in txt:
            return {"action":"list_week"}
        if "перенес" in txt or "перенеси" in txt:
            return {"action":"reschedule","task_query":user_text}
        if "выполнил" in txt or "сделал" in txt:
            return {"action":"mark_done","task_query":user_text}
        if "добав" in txt:
            return {"action":"add_task","free_text":user_text}
        return {"action":"help"}

    # Подготавливаем "контекст" (минимум, чтобы не слать лишнего)
    context = {
        "today": today_str,
        "week": week_dates_list
    }

    try:
        msg = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[
                {"role":"system","content":SYSTEM_PROMPT},
                {"role":"user","content":f"Контекст: {json.dumps(context, ensure_ascii=False)}\nТекст: {user_text}"}
            ],
            response_format={"type":"json_object"},
        )
        raw = msg.choices[0].message.content
        data = json.loads(raw)
        return data
    except Exception as e:
        log.error(f"GPT intent error: {e}")
        return {"action":"help"}

# ---------------------------
# Обработчики команд / кнопок
# ---------------------------
@bot.message_handler(commands=["start"])
def on_start(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я твой ассистент по задачам. Выбирай действие ниже или просто пиши мне по-человечески, что нужно сделать 🤝",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def on_help(message):
    bot.send_message(message.chat.id,
        "<b>Примеры:</b>\n"
        "• «Покажи задачи на сегодня»\n"
        "• «Вся неделя»\n"
        "• «Я выполнил заказ табака»\n"
        "• «Добавь завтра заказать кофе в 14:00»\n"
        "• «Перенеси мой заказ кофе на пятницу 15:00»",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "📅 Сегодня")
def on_today(message):
    today = datetime.now(tz).strftime("%d.%m.%Y")
    rows = tasks_for_date(message.chat.id, today)
    txt = f"📅 <b>Задачи на {today}</b>\n\n" + format_tasks_grouped(rows, title_date=today)
    bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda m: m.text == "📆 Неделя")
def on_week_menu(message):
    bot.send_message(message.chat.id, "Выбери день недели:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda m: m.text == "🗓 Вся неделя")
def on_week_all(message):
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = tasks_for_week(message.chat.id, start)
    # Сгруппируем по датам
    bucket = defaultdict(list)
    for r in rows:
        bucket[r.get("Дата","")] += [r]
    ordered = []
    for d in week_dates(start):
        ordered.append((d, bucket.get(d, [])))
    bot.send_message(message.chat.id, format_week_list(ordered))

@bot.message_handler(func=lambda m: "(" in (m.text or "") and ")" in (m.text or ""))
def on_day_pick(message):
    # формат кнопки: "Среда (13.08.2025)"
    try:
        date_str = message.text.split("(")[1].strip(")")
    except Exception:
        date_str = ""
    if not normalize_date_str(date_str):
        bot.send_message(message.chat.id, "Не понял дату 🤔", reply_markup=main_menu())
        return
    rows = tasks_for_date(message.chat.id, date_str)
    txt = f"📅 <b>Задачи на {date_str}</b>\n\n" + format_tasks_grouped(rows, title_date=date_str)
    bot.send_message(message.chat.id, txt, reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "⬅ Назад")
def on_back(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=main_menu())

# ---------------------------
# Natural language (GPT ассистент)
# ---------------------------
def handle_intent(message, intent: dict):
    uid = message.chat.id
    today = datetime.now(tz)
    today_s = today.strftime("%d.%m.%Y")

    action = intent.get("action","help")
    if action == "help":
        return on_help(message)

    if action == "list_today":
        rows = tasks_for_date(uid, today_s)
        bot.send_message(uid, f"📅 <b>Задачи на {today_s}</b>\n\n" + format_tasks_grouped(rows, title_date=today_s))
        return

    if action == "list_week":
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = tasks_for_week(uid, start)
        bucket = defaultdict(list)
        for r in rows:
            bucket[r.get("Дата","")] += [r]
        ordered = []
        for d in week_dates(start):
            ordered.append((d, bucket.get(d, [])))
        bot.send_message(uid, format_week_list(ordered))
        return

    if action == "list_day":
        d = intent.get("date","")
        d = normalize_date_str(d)
        if not d:
            bot.send_message(uid, "Нужна дата в формате ДД.ММ.ГГГГ.")
            return
        rows = tasks_for_date(uid, d)
        bot.send_message(uid, f"📅 <b>Задачи на {d}</b>\n\n" + format_tasks_grouped(rows, title_date=d))
        return

    if action == "mark_done":
        query = (intent.get("task_query") or "").strip()
        rows = tasks_for_date(uid, today_s)
        if not rows:
            bot.send_message(uid, "На сегодня нет задач для отметки.")
            return
        pick = fuzzy_pick_task(rows, query) if query else None
        if not pick:
            bot.send_message(uid, "Не нашёл задачу. Уточни название.")
            return
        # Найти в таблице: колонка "Задача" — 4, "Статус" — 6
        try:
            desc = pick.get("Задача","")
            cell = tasks_ws.find(desc)
            if cell:
                tasks_ws.update_cell(cell.row, 6, "выполнено")
                bot.send_message(uid, f"✅ Отметил: <b>{desc}</b>")
            else:
                bot.send_message(uid, "Не смог найти строку в таблице.")
        except Exception as e:
            log.exception(e)
            bot.send_message(uid, "Ошибка при отметке задачи.")
        return

    if action == "add_task":
        # Пытаемся собрать поля
        date_s = normalize_date_str(intent.get("date","")) or today_s
        category = intent.get("category","Без категории")
        subcat = intent.get("subcategory","—")
        desc = intent.get("task_query") or intent.get("free_text") or "Без описания"
        deadline = intent.get("deadline","")
        repeat = intent.get("repeat","")
        try:
            append_task(date_s, category, subcat, desc, deadline, uid, status="", repeat=repeat)
            bot.send_message(uid, f"✅ Добавил задачу на <b>{date_s}</b>:\n• {desc} (до {deadline})")
        except Exception as e:
            log.exception(e)
            bot.send_message(uid, "Не удалось добавить задачу.")
        return

    if action == "reschedule":
        # Найти задачу (по умолчанию из сегодня), изменить дату/дедлайн
        query = (intent.get("task_query") or "").strip()
        new_date = normalize_date_str(intent.get("date","")) or today_s
        new_deadline = intent.get("deadline","")
        rows = filter_tasks_by_user(get_tasks_raw(), uid)
        pick = fuzzy_pick_task(rows, query) if query else None
        if not pick:
            bot.send_message(uid, "Не нашёл задачу для переноса. Уточни название.")
            return
        try:
            desc = pick.get("Задача","")
            cell = tasks_ws.find(desc)
            if cell:
                # Колонки: 1-Дата, 5-Дедлайн
                tasks_ws.update_cell(cell.row, 1, new_date)
                if new_deadline:
                    tasks_ws.update_cell(cell.row, 5, new_deadline)
                bot.send_message(uid, f"🔁 Перенёс <b>{desc}</b> на <b>{new_date}</b>{(' '+new_deadline) if new_deadline else ''}")
            else:
                bot.send_message(uid, "Не смог найти строку в таблице.")
        except Exception as e:
            log.exception(e)
            bot.send_message(uid, "Ошибка при переносе задачи.")
        return

    # На всякий случай
    on_help(message)

@bot.message_handler(func=lambda m: True)
def on_any_text(message):
    text = (message.text or "").strip()
    # Кнопки и спец-команды уже перехвачены выше; здесь — свободный текст
    today = datetime.now(tz)
    intent = gpt_intent(text, today.strftime("%d.%m.%Y"), week_dates(today))
    handle_intent(message, intent)

# ---------------------------
# Автоплан на 09:00
# ---------------------------
def send_daily_plan():
    try:
        today_s = datetime.now(tz).strftime("%d.%m.%Y")
        for u in get_users():
            uid = u["id"]
            rows = tasks_for_date(uid, today_s)
            if rows:
                bot.send_message(uid, f"🌅 Доброе утро!\nВот твой план на <b>{today_s}</b>:\n\n" + format_tasks_grouped(rows, title_date=today_s))
    except Exception as e:
        log.exception(e)

scheduler = BackgroundScheduler(timezone=tz)
# Каждый день в 09:00 локального TZ
scheduler.add_job(send_daily_plan, CronTrigger(hour=9, minute=0))

# ---------------------------
# Flask + Webhook
# ---------------------------
app = Flask(__name__)

@app.route("/" + API_TOKEN, methods=["POST"])
def tg_webhook():
    try:
        json_str = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.exception(e)
        return "ERR", 500
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!", 200

def setup_webhook():
    # Удалим на всякий случай, затем установим
    try:
        bot.remove_webhook()
    except Exception:
        pass
    webhook_url = f"{WEBHOOK_BASE}/{API_TOKEN}"
    ok = bot.set_webhook(url=webhook_url, max_connections=40)
    if ok:
        log.info(f"Webhook set to: {webhook_url}")
    else:
        log.error("Failed to set webhook")

# ---------------------------
# Entry
# ---------------------------
if __name__ == "__main__":
    setup_webhook()
    scheduler.start()
    # Flask dev server (на Render это ок для простого сервиса)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
