import os
import re
import json
import time
import pytz
import queue
import gspread
import logging
import schedule
import threading
from datetime import datetime, timedelta
from flask import Flask, request
from telebot import TeleBot, types

# =========================
#   ОКРУЖЕНИЕ / НАСТРОЙКИ
# =========================
API_TOKEN        = os.getenv("TELEGRAM_TOKEN")
TABLE_URL        = os.getenv("GOOGLE_SHEETS_URL")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")  # например: https://<app>.onrender.com
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")

if not API_TOKEN or not TABLE_URL or not WEBHOOK_BASE:
    print("[WARN] Не заданы переменные окружения. Нужны: TELEGRAM_TOKEN, GOOGLE_SHEETS_URL, WEBHOOK_BASE")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}"

LOCAL_TZ = pytz.timezone("Europe/Moscow")  # фиксировано, как ты просил

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# =========================
#   TELEGRAM / SHEETS
# =========================
bot = TeleBot(API_TOKEN, parse_mode="HTML")

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)
except Exception as e:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

# Основные листы-направления
DIRECTION_SHEETS = {
    "Кофейня": sh.worksheet("Кофейня"),
    "Табачка": sh.worksheet("Табачка"),
    "WB"     : sh.worksheet("WB"),
    "Личное" : sh.worksheet("Личное"),
}
WS_SUPPLIERS = sh.worksheet("Поставщики")
WS_USERS     = sh.worksheet("Пользователи")
WS_LOGS      = sh.worksheet("Логи")

TASK_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]
SUPP_HEADERS = ["Поставщик","Направление","ТТ","Период_дней","Доставка_дней","Хранение_дней","Старт_цикла","Авто","Активен"]

def ensure_headers(ws, expected):
    try:
        head = ws.row_values(1)
        if head != expected:
            log.warning(f"Колонки листа '{ws.title}' отличаются.\nОжидались: {expected}\nФактически: {head}")
    except Exception as e:
        log.error(f"Не смог проверить заголовки {ws.title}: {e}")

for name, ws in DIRECTION_SHEETS.items():
    ensure_headers(ws, TASK_HEADERS)
ensure_headers(WS_SUPPLIERS, SUPP_HEADERS)

# =========================
#          Утилиты
# =========================
def now_local():
    return datetime.now(LOCAL_TZ)

def dstr(dt):
    return dt.strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    return ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][dt.weekday()]

def log_event(user_id, action, payload=""):
    try:
        WS_LOGS.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Логи недоступны: {e}")

def add_task(ws, date_s, category, subcat, text, deadline, user_id, status="", repeat="", source="ручной"):
    row = [date_s, category, subcat, text, deadline, str(user_id), status, repeat, source]
    ws.append_row(row, value_input_option="USER_ENTERED")

def parse_tt_list(tt_cell: str):
    # "Центр,Полет" -> ["Центр","Полет"]
    return [t.strip() for t in (tt_cell or "").split(",") if t.strip()]

def get_all_tasks_for_date(user_id, date_s):
    out = []
    for direction, ws in DIRECTION_SHEETS.items():
        try:
            rows = ws.get_all_records()
        except Exception as e:
            log.error(f"Не удалось прочитать лист {direction}: {e}")
            rows = []
        for r in rows:
            if r.get("Дата") == date_s and str(r.get("User ID")) == str(user_id):
                r["_direction"] = direction  # для группировки в выводе
                out.append(r)
    return out

def get_all_tasks_for_week(user_id, start_dt=None):
    if not start_dt:
        start_dt = now_local().date()
    period = {dstr(start_dt + timedelta(days=i)) for i in range(7)}
    out = []
    for direction, ws in DIRECTION_SHEETS.items():
        try:
            rows = ws.get_all_records()
        except Exception as e:
            log.error(f"Не удалось прочитать лист {direction}: {e}")
            rows = []
        for r in rows:
            if r.get("Дата") in period and str(r.get("User ID")) == str(user_id):
                r["_direction"] = direction
                out.append(r)
    return out

def format_tasks_grouped(tasks, header_date=None):
    if not tasks:
        return "Задач нет."

    # сортировка по: направление, категория, подкатегория, дедлайн, текст
    def keyf(r):
        dl = r.get("Дедлайн") or ""
        try:
            dl_k = datetime.strptime(dl, "%H:%M").time()
        except:
            dl_k = datetime.min.time()
        return (r.get("_direction",""), r.get("Категория",""), r.get("Подкатегория",""), dl_k, r.get("Задача",""))

    tasks = sorted(tasks, key=keyf)

    out = []
    if header_date:
        dt = datetime.strptime(header_date, "%d.%m.%Y")
        out.append(f"• {weekday_ru(dt)} — {header_date}\n")

    cur_dir = cur_cat = cur_sub = None
    for r in tasks:
        d  = r.get("_direction","")
        c  = r.get("Категория","")
        sc = r.get("Подкатегория","")
        txt= r.get("Задача","")
        dl = r.get("Дедлайн","") or ""
        st = (r.get("Статус","") or "").lower()
        rep= (r.get("Повторяемость","") or "")
        icon = "✅" if st == "выполнено" else ("🔁" if rep else "⬜")

        if d != cur_dir:
            out.append(f"📂 <b>{d}</b>")
            cur_dir = d; cur_cat = cur_sub = None
        if c != cur_cat:
            out.append(f"  └ <b>{c}</b>")
            cur_cat = c; cur_sub = None
        if sc != cur_sub:
            out.append(f"    └ <i>{sc or '—'}</i>")
            cur_sub = sc
        line = f"      {icon} {txt}"
        if dl:
            line += f"  <i>(до {dl})</i>"
        out.append(line)
    return "\n".join(out)

# =========================
#  Поставщики → автогенерация
# =========================
def load_suppliers(active_only=True):
    rows = WS_SUPPLIERS.get_all_records()
    out = []
    for r in rows:
        if active_only and (str(r.get("Активен") or "").strip().lower() not in ("да","1","y","true","on")):
            continue
        out.append(r)
    return out

def plan_from_supplier_row(r, target_date: datetime):
    """
    Возвращает список задач, которые нужно создать на target_date,
    исходя из периодичности поставщика.
    Поля: Период_дней, Доставка_дней, Хранение_дней, Старт_цикла, Авто
    Логика:
      - если Авто=Да, и (target_date - Старт) % Период_дней == 0 → это день заказа.
      - в день заказа создаём «Закупка» (до 14:00 по умолчанию).
      - если Доставка_дней > 0 → на (заказ+доставка) создаём «Поставка / Приемка».
      - если Хранение_дней > 0 → «следующий заказ» ставит цикл так, чтобы интервал между доставками ≈ Хранение_дней.
        (но базово цикл уже задан Период_дней — этого достаточно)
    """
    try:
        start = datetime.strptime(str(r.get("Старт_цикла") or "").strip(), "%d.%m.%Y").date()
    except:
        # если нет стартовой даты — привязка к 01.01.2025
        start = datetime(2025,1,1).date()

    period = int(r.get("Период_дней") or 0)
    delivery = int(r.get("Доставка_дней") or 0)
    # shelf = int(r.get("Хранение_дней") or 0)  # пока не используем явно
    auto = str(r.get("Авто") or "").strip().lower() in ("да","1","y","true","on")

    if not auto or period <= 0:
        return []  # не авто — бот не генерит

    delta = (target_date.date() - start).days
    if delta < 0:
        return []
    is_order_day = (delta % period == 0)

    if not is_order_day:
        # возможно сегодня день «приемки»
        if delivery > 0 and (delta - delivery) >= 0 and ((delta - delivery) % period == 0):
            # значит заказ был "delivery" дней назад
            # создаём только приемку
            return [{
                "Тип":"Поставка",
                "Категория":"Поставка",
                "Подкатегория":"Приемка",
                "Задача": f"Приемка {r.get('Поставщик')} ({r.get('ТТ')})",
                "Дедлайн":"11:00"
            }]
        return []

    # День заказа — создаём заказ; приемку создадим отдельно в день доставки
    return [{
        "Тип":"Закупка",
        "Категория":"Закупка",
        "Подкатегория": r.get("Поставщик"),
        "Задача": f"Заказ {r.get('Поставщик')} ({r.get('ТТ')})",
        "Дедлайн":"14:00"
    }]

def upsert_tasks_for_date_from_suppliers(target_dt: datetime, user_id):
    """
    Для даты target_dt: пройтись по поставщикам и создать недостающие задачи
    в соответствующих листах-направлениях, без дублей.
    """
    created = 0
    suppliers = load_suppliers(active_only=True)
    date_s = dstr(target_dt)

    # Снимем слепок всех задач на дату, чтобы не плодить дубли
    existing_by_dir = {}
    for direction, ws in DIRECTION_SHEETS.items():
        try:
            rows = ws.get_all_records()
        except:
            rows = []
        existing_by_dir[direction] = {(r.get("Дата"), r.get("Категория"), r.get("Подкатегория"), r.get("Задача"), str(r.get("User ID"))) for r in rows}

    for r in suppliers:
        direction = (r.get("Направление") or "").strip()
        tt_list = parse_tt_list(r.get("ТТ") or "")
        if direction not in DIRECTION_SHEETS:
            continue
        ws = DIRECTION_SHEETS[direction]
        plan = plan_from_supplier_row(r, target_dt)
        if not plan:
            continue

        for tt in tt_list or ["—"]:
            for item in plan:
                key = (date_s, item["Категория"], tt, item["Задача"].replace("({})".format(r.get("ТТ")), f"({tt})"), str(user_id))
                # соберём фактический текст
                task_text = item["Задача"].replace(str(r.get("ТТ")), tt)
                dedl = item["Дедлайн"]
                # проверка дубля (используем Подкатегория как ТТ)
                dedup = (date_s, item["Категория"], tt, task_text, str(user_id))
                if dedup in existing_by_dir[direction]:
                    continue
                add_task(ws, date_s, item["Категория"], tt, task_text, dedl, user_id, status="", repeat="авто", source="поставщики")
                existing_by_dir[direction].add(dedup)
                created += 1
    return created

# =========================
#           Меню
# =========================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Задачи","🚚 Поставщики")
    kb.row("🤖 GPT ассистент","⚙ Настройки")
    return kb

def tasks_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить задачу","✅ Я сделал…")
    kb.row("🔁 Генерировать неделю","⬅ Назад")
    return kb

def suppliers_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить поставщика","📋 Список поставщиков")
    kb.row("🔁 Генерировать неделю","⬅ Назад")
    return kb

def back_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("⬅ Назад")
    return kb

def choose_direction_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("Кофейня","Табачка")
    kb.row("WB","Личное")
    kb.row("Отмена")
    return kb

def choose_tt_kb(direction):
    # фиксированные точки: Кофейня: Центр, Полет; Табачка: Центр, Полет, Климово
    tts = []
    if direction == "Кофейня":
        tts = ["Центр","Полет"]
    elif direction == "Табачка":
        tts = ["Центр","Полет","Климово"]
    else:
        tts = ["—"]
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for t in tts:
        kb.add(t)
    kb.add("Готово","Отмена")
    return kb

# =========================
#         FSM
# =========================
STATE = {}
BUFFER = {}

def set_state(uid, s, data=None):
    STATE[uid] = s
    if data is not None:
        BUFFER[uid] = data

def clr_state(uid):
    STATE.pop(uid, None)
    BUFFER.pop(uid, None)

# =========================
#         Хендлеры
# =========================
app = Flask(__name__)

@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Выбери раздел:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def back(m):
    clr_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# -------- Раздел "Задачи"
@bot.message_handler(func=lambda msg: msg.text == "📅 Задачи")
def go_tasks(m):
    bot.send_message(m.chat.id, "Раздел «Задачи»", reply_markup=tasks_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today(m):
    uid = m.chat.id
    # автоген на сегодня из поставщиков
    upsert_tasks_for_date_from_suppliers(now_local(), uid)
    date_s = dstr(now_local())
    tasks = get_all_tasks_for_date(uid, date_s)
    bot.send_message(uid, f"📅 Задачи на {date_s}\n\n" + format_tasks_grouped(tasks, header_date=date_s), reply_markup=tasks_menu())

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week(m):
    uid = m.chat.id
    # автоген на 7 дней вперёд
    for i in range(7):
        upsert_tasks_for_date_from_suppliers(now_local()+timedelta(days=i), uid)
    tasks = get_all_tasks_for_week(uid)
    if not tasks:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=tasks_menu()); return
    by_day = {}
    for r in tasks:
        by_day.setdefault(r["Дата"], []).append(r)
    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        parts.append(format_tasks_grouped(by_day[d], header_date=d))
        parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=tasks_menu())

@bot.message_handler(func=lambda msg: msg.text == "🔁 Генерировать неделю")
def gen_week(m):
    uid = m.chat.id
    total = 0
    for i in range(7):
        total += upsert_tasks_for_date_from_suppliers(now_local()+timedelta(days=i), uid)
    bot.send_message(uid, f"Готово. Сгенерировано задач: {total}", reply_markup=tasks_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить задачу")
def add_task_start(m):
    set_state(m.chat.id, "add_task_direction", {"task":{}})
    bot.send_message(m.chat.id, "Выбери направление:", reply_markup=choose_direction_kb())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_direction")
def add_task_direction(m):
    uid = m.chat.id
    direction = m.text.strip()
    if direction not in DIRECTION_SHEETS:
        bot.send_message(uid, "Выбери из кнопок.", reply_markup=choose_direction_kb()); return
    BUFFER[uid]["task"]["direction"] = direction
    set_state(uid, "add_task_category", BUFFER[uid])
    bot.send_message(uid, "Категория? (например: Закупка, Поставка, Общее)", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_category")
def add_task_category(m):
    uid = m.chat.id
    cat = m.text.strip()
    BUFFER[uid]["task"]["category"] = cat
    set_state(uid, "add_task_subcat", BUFFER[uid])
    bot.send_message(uid, "Подкатегория (например: К-Экспро / ИП Вылегжанина / или просто — Центр):", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_subcat")
def add_task_subcat(m):
    uid = m.chat.id
    sub = m.text.strip()
    BUFFER[uid]["task"]["subcat"] = sub
    set_state(uid, "add_task_text", BUFFER[uid])
    bot.send_message(uid, "Текст задачи:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_text")
def add_task_text(m):
    uid = m.chat.id
    text = m.text.strip()
    BUFFER[uid]["task"]["text"] = text
    set_state(uid, "add_task_deadline", BUFFER[uid])
    bot.send_message(uid, "Дедлайн (ЧЧ:ММ) или «—»:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_deadline")
def add_task_deadline(m):
    uid = m.chat.id
    ded = m.text.strip()
    if ded == "—": ded = ""
    BUFFER[uid]["task"]["deadline"] = ded
    set_state(uid, "add_task_date", BUFFER[uid])
    bot.send_message(uid, "Дата (ДД.ММ.ГГГГ) или «сегодня/завтра»:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "add_task_date")
def add_task_date(m):
    uid = m.chat.id
    val = m.text.strip().lower()
    if val == "сегодня":
        date_s = dstr(now_local())
    elif val == "завтра":
        date_s = dstr(now_local()+timedelta(days=1))
    else:
        try:
            datetime.strptime(val, "%d.%m.%Y")
            date_s = val
        except:
            bot.send_message(uid, "Формат даты ДД.ММ.ГГГГ или «сегодня/завтра».", reply_markup=back_menu()); return

    t = BUFFER[uid]["task"]
    ws = DIRECTION_SHEETS[t["direction"]]
    add_task(ws, date_s, t["category"], t["subcat"], t["text"], t["deadline"], uid, status="", repeat="", source="ручной")
    bot.send_message(uid, f"✅ Добавлено в {t['direction']} на {date_s}:\n• {t['category']} / {t['subcat']} — {t['text']} (до {t['deadline'] or '—'})", reply_markup=tasks_menu())
    log_event(uid, "add_task", json.dumps(t, ensure_ascii=False))
    clr_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def done_start(m):
    set_state(m.chat.id, "done_text", {})
    bot.send_message(m.chat.id, "Напиши в свободной форме. Примеры:\n• сделал заказы к-экспро центр\n• сделал все заказы к-экспро\n• принял поставку ИП Вылегжанина", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "done_text")
def done_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()

    # очень простой парсер: ищем поставщика/ТТ/тип
    supplier = None
    if "к-экспро" in txt or "k-exp" in txt: supplier = "К-Экспро"
    elif "вылегжан" in txt: supplier = "ИП Вылегжанина"

    tt = None
    if "центр" in txt: tt = "Центр"
    if "полет" in txt or "полёт" in txt: tt = "Полет"
    if "климов" in txt: tt = "Климово"

    today_s = dstr(now_local())
    changed = 0

    # пройти все листы и отметить подходящие задачи на сегодня
    for direction, ws in DIRECTION_SHEETS.items():
        rows = ws.get_all_records()
        for idx, r in enumerate(rows, start=2):  # с 2-й строки
            if r.get("Дата") != today_s: continue
            if str(r.get("User ID")) != str(uid): continue
            text = (r.get("Задача") or "").lower()
            subc = (r.get("Подкатегория") or "")
            tt_match = (tt is None) or (subc.strip().lower() == tt.strip().lower()) or (tt in text)
            supp_match = (supplier is None) or (supplier.lower() in text)
            if tt_match and supp_match and r.get("Статус","").lower() != "выполнено":
                ws.update_cell(idx, TASK_HEADERS.index("Статус")+1, "выполнено")
                changed += 1

    # + автоген по правилам, если указан поставщик
    extra = ""
    if supplier:
        # создаём приемку/следующий заказ по дате (как минимум доставка на завтра и т.п. создадутся в свои дни авто-планом)
        for i in range(1,3):  # прогоняем 2 дня вперёд, чтобы не пропустить приемку
            upsert_tasks_for_date_from_suppliers(now_local()+timedelta(days=i), uid)
        extra = "\n🔮 Запланировал последующие шаги по правилам поставщиков."

    bot.send_message(uid, f"✅ Отмечено выполненным: {changed}.{extra}", reply_markup=tasks_menu())
    log_event(uid, "done_free_text", txt)
    clr_state(uid)

# -------- Раздел "Поставщики"
@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставщики")
def go_suppliers(m):
    bot.send_message(m.chat.id, "Раздел «Поставщики»", reply_markup=suppliers_menu())

@bot.message_handler(func=lambda msg: msg.text == "📋 Список поставщиков")
def list_suppliers(m):
    rows = load_suppliers(active_only=False)
    if not rows:
        bot.send_message(m.chat.id, "Список пуст.", reply_markup=suppliers_menu()); return
    lines = []
    for r in rows[:50]:
        lines.append(f"• {r.get('Поставщик')} — {r.get('Направление')} [{r.get('ТТ')}] / период {r.get('Период_дней')} / авто={r.get('Авто')} / активен={r.get('Активен')}")
    bot.send_message(m.chat.id, "\n".join(lines), reply_markup=suppliers_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить поставщика")
def add_supplier_start(m):
    set_state(m.chat.id, "supp_name", {"supp":{}})
    bot.send_message(m.chat.id, "Название поставщика:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_name")
def supp_name(m):
    uid = m.chat.id
    BUFFER[uid]["supp"]["name"] = m.text.strip()
    set_state(uid, "supp_direction", BUFFER[uid])
    bot.send_message(uid, "Направление:", reply_markup=choose_direction_kb())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_direction")
def supp_direction(m):
    uid = m.chat.id
    direction = m.text.strip()
    if direction not in DIRECTION_SHEETS:
        bot.send_message(uid, "Выбери направление из кнопок.", reply_markup=choose_direction_kb()); return
    BUFFER[uid]["supp"]["direction"] = direction
    BUFFER[uid]["supp"]["tts"] = []
    set_state(uid, "supp_tt", BUFFER[uid])
    bot.send_message(uid, "Выбери ТТ (несколько по очереди), затем «Готово».", reply_markup=choose_tt_kb(direction))

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_tt")
def supp_tt(m):
    uid = m.chat.id
    val = m.text.strip()
    if val == "Отмена":
        clr_state(uid)
        bot.send_message(uid, "Отменено.", reply_markup=suppliers_menu()); return
    if val == "Готово":
        if not BUFFER[uid]["supp"]["tts"]:
            bot.send_message(uid, "Выбери хотя бы одну ТТ.", reply_markup=choose_tt_kb(BUFFER[uid]["supp"]["direction"])); return
        set_state(uid, "supp_period", BUFFER[uid])
        bot.send_message(uid, "Период (дней) между заказами, например «2»:", reply_markup=back_menu())
        return
    # добавить ТТ
    if val not in BUFFER[uid]["supp"]["tts"]:
        BUFFER[uid]["supp"]["tts"].append(val)
    bot.send_message(uid, f"Добавил: {val}. Ещё или «Готово».", reply_markup=choose_tt_kb(BUFFER[uid]["supp"]["direction"]))

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_period")
def supp_period(m):
    uid = m.chat.id
    try:
        n = int(m.text.strip())
        if n <= 0: raise ValueError()
    except:
        bot.send_message(uid, "Укажи целое число > 0.", reply_markup=back_menu()); return
    BUFFER[uid]["supp"]["period"] = n
    set_state(uid, "supp_delivery", BUFFER[uid])
    bot.send_message(uid, "Доставка (дней от заказа до приемки), например «1»:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_delivery")
def supp_delivery(m):
    uid = m.chat.id
    try:
        d = int(m.text.strip())
        if d < 0: raise ValueError()
    except:
        bot.send_message(uid, "Укажи целое число ≥ 0.", reply_markup=back_menu()); return
    BUFFER[uid]["supp"]["delivery"] = d
    set_state(uid, "supp_shelf", BUFFER[uid])
    bot.send_message(uid, "Хранение (дней), например «0» если не важно:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_shelf")
def supp_shelf(m):
    uid = m.chat.id
    try:
        s = int(m.text.strip())
        if s < 0: raise ValueError()
    except:
        bot.send_message(uid, "Укажи целое число ≥ 0.", reply_markup=back_menu()); return
    BUFFER[uid]["supp"]["shelf"] = s
    set_state(uid, "supp_start", BUFFER[uid])
    bot.send_message(uid, "Дата старта цикла (ДД.ММ.ГГГГ) или «сегодня»:", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_start")
def supp_start(m):
    uid = m.chat.id
    val = m.text.strip().lower()
    if val == "сегодня":
        start_s = dstr(now_local())
    else:
        try:
            datetime.strptime(val, "%d.%m.%Y")
            start_s = val
        except:
            bot.send_message(uid, "Формат ДД.ММ.ГГГГ или «сегодня».", reply_markup=back_menu()); return
    BUFFER[uid]["supp"]["start"] = start_s
    set_state(uid, "supp_auto", BUFFER[uid])
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Да","Нет"); kb.row("⬅ Назад")
    bot.send_message(uid, "Авто-генерация задач включена? (Да/Нет)", reply_markup=kb)

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "supp_auto")
def supp_auto(m):
    uid = m.chat.id
    val = m.text.strip().lower()
    if val not in ("да","нет"):
        bot.send_message(uid, "Ответь «Да» или «Нет».", reply_markup=back_menu()); return
    BUFFER[uid]["supp"]["auto"] = "Да" if val == "да" else "Нет"
    # Активен = Да по умолчанию
    data = BUFFER[uid]["supp"]
    WS_SUPPLIERS.append_row([
        data["name"], data["direction"], ",".join(data["tts"]),
        data["period"], data["delivery"], data["shelf"],
        data["start"], data["auto"], "Да"
    ], value_input_option="USER_ENTERED")
    bot.send_message(uid, f"✅ Поставщик добавлен:\n• {data['name']} — {data['direction']} [{', '.join(data['tts'])}]\n• Период {data['period']} дн., доставка {data['delivery']} дн., хранение {data['shelf']} дн.\n• Старт {data['start']}, авто={data['auto']}",
                     reply_markup=suppliers_menu())
    log_event(uid, "add_supplier", json.dumps(data, ensure_ascii=False))
    clr_state(uid)

# -------- GPT ассистент
@bot.message_handler(func=lambda msg: msg.text == "🤖 GPT ассистент")
def ai_start(m):
    set_state(m.chat.id, "ai_query", {})
    bot.send_message(m.chat.id, "Сформулируй запрос: приоритизировать дела, составить план на день/неделю, найти окна и т.п.", reply_markup=back_menu())

@bot.message_handler(func=lambda msg: STATE.get(msg.chat.id) == "ai_query")
def ai_query(m):
    uid = m.chat.id
    q = m.text.strip()

    # соберём контекст недели
    tasks = get_all_tasks_for_week(uid)
    lines = []
    for r in sorted(tasks, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
        lines.append(f"{r['Дата']} • {r['_direction']} / {r['Категория']} / {r['Подкатегория']} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','') or ''}]")
    context = "\n".join(lines)[:4000]

    if not OPENAI_API_KEY:
        bot.send_message(uid, "🧠 (Без GPT) Совет: начни с задач с ближайшим дедлайном и высокой ценностью.", reply_markup=main_menu())
        clr_state(uid); return

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = ("Ты личный ассистент по задачам. Дай краткий, по делу план: что делать сегодня/на неделю, на что обратить внимание. "
               "Отвечай на русском, используй буллеты и чёткие приоритеты.")
        prompt = f"Запрос: {q}\n\nМои задачи:\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        ans = resp.choices[0].message.content.strip()
        bot.send_message(uid, f"🧠 {ans}", reply_markup=main_menu())
        log_event(uid, "ai_query", q)
    except Exception as e:
        log.error(f"AI error: {e}", exc_info=True)
        bot.send_message(uid, "Не удалось получить ответ ассистента.", reply_markup=main_menu())
    finally:
        clr_state(uid)

# -------- Настройки (пока-заглушка для расширений)
@bot.message_handler(func=lambda msg: msg.text == "⚙ Настройки")
def settings(m):
    bot.send_message(m.chat.id, "В настройках позже добавим напоминания по времени и др.", reply_markup=main_menu())

# =========================
#    ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ
# =========================
def job_daily_digest():
    try:
        users = WS_USERS.get_all_records()
        today_s = dstr(now_local())
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            # сгенерить задачи поставщиков на сегодня
            upsert_tasks_for_date_from_suppliers(now_local(), uid)
            tasks = get_all_tasks_for_date(uid, today_s)
            if not tasks: continue
            txt = f"📅 План на {today_s}\n\n" + format_tasks_grouped(tasks, header_date=today_s)
            try:
                bot.send_message(uid, txt)
            except Exception as e:
                log.error(f"Не смог отправить {uid}: {e}")
    except Exception as e:
        log.error(f"job_daily_digest error: {e}", exc_info=True)

def scheduler_thread():
    schedule.clear()
    # 08:00 по Москве
    schedule.every().day.at("08:00").do(job_daily_digest)
    while True:
        schedule.run_pending()
        time.sleep(1)

# =========================
#          WEBHOOK
# =========================
@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    try:
        update = request.get_data().decode("utf-8")
        upd = types.Update.de_json(update)
        bot.process_new_updates([upd])
    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!"

# =========================
#          START
# =========================
if __name__ == "__main__":
    if not API_TOKEN or not WEBHOOK_URL:
        raise RuntimeError("Не заданы TELEGRAM_TOKEN или WEBHOOK_BASE")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)

    threading.Thread(target=scheduler_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
