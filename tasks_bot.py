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
from flask import Flask, request
from telebot import TeleBot, types

# ========= ОКРУЖЕНИЕ =========
API_TOKEN        = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")
GOOGLE_SHEETS_URL= os.getenv("GOOGLE_SHEETS_URL")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")

if not API_TOKEN or not WEBHOOK_BASE or not GOOGLE_SHEETS_URL:
    print("[WARN] Не заданы переменные окружения: TELEGRAM_TOKEN / WEBHOOK_BASE / GOOGLE_SHEETS_URL")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}"

LOCAL_TZ = pytz.timezone(os.getenv("TZ", "Europe/Moscow"))

# ========= ЛОГИ =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# ========= БОТ / SHEETS =========
bot = TeleBot(API_TOKEN, parse_mode="HTML")
gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(GOOGLE_SHEETS_URL)

# Основные листы
ws_tasks = sh.worksheet("Все задачи")
ws_users = sh.worksheet("Пользователи")
ws_logs  = sh.worksheet("Логи")
# Поставщики не обязательны, но если есть — поможем ассистенту
try:
    ws_suppliers = sh.worksheet("Поставщики")
except Exception:
    ws_suppliers = None

# ========= КОНСТАНТЫ =========
# «Все задачи» — строгий порядок колонок:
TASK_COLS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]

WEEKDAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
WEEKDAYS_RU_FULL = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]

# ========= УТИЛИТЫ =========
def now_local():
    return datetime.now(LOCAL_TZ)

def today_str():
    return now_local().strftime("%d.%m.%Y")

def weekday_full(dt: datetime) -> str:
    return WEEKDAYS_RU_FULL[dt.weekday()]

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Логи недоступны: {e}")

def get_all(ws):
    return ws.get_all_records()

def ensure_headers(ws, expected):
    try:
        hdr = ws.row_values(1)
        if hdr != expected:
            log.warning(f"Лист {ws.title}: ожидались заголовки {expected}, фактически {hdr}")
    except Exception as e:
        log.error(f"Проверка заголовков {ws.title} не удалась: {e}")

ensure_headers(ws_tasks, TASK_COLS)

# ========= НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ =========
def get_user_settings(user_id):
    """Читает настройки из листа Пользователи. Возвращает дефолты если строка не найдена."""
    defaults = {
        "digest_on": True,
        "digest_time": "08:00",
        "reminders_on": True,
        "remind_before_min": 30,
        "name": ""
    }
    try:
        for r in get_all(ws_users):
            uid = str(r.get("Telegram ID") or "").strip()
            if uid == str(user_id):
                digest_on = str(r.get("Утренний дайджест","да")).strip().lower() in ("да","1","y","true","on")
                digest_time = (r.get("Время дайджеста") or "08:00").strip()
                reminders_on = str(r.get("Напоминания","да")).strip().lower() in ("да","1","y","true","on")
                before = r.get("За сколько мин напоминать")
                try:
                    before = int(before)
                except:
                    before = 30
                return {
                    "digest_on": digest_on,
                    "digest_time": digest_time,
                    "reminders_on": reminders_on,
                    "remind_before_min": before,
                    "name": (r.get("Имя") or "").strip()
                }
    except Exception as e:
        log.error(f"Не удалось прочитать настройки пользователя: {e}")
    return defaults

def set_user_setting(user_id, field, value):
    """Пишет одну настройку в лист Пользователи, создаёт строку если нет."""
    try:
        rows = ws_users.get_all_records()
        target_row = None
        for idx, r in enumerate(rows, start=2):  # данные начинаются со 2 строки
            if str(r.get("Telegram ID") or "").strip() == str(user_id):
                target_row = idx
                break
        # Если нет — создадим
        if not target_row:
            ws_users.append_row([str(user_id), "", "да", "08:00", "да", 30], value_input_option="USER_ENTERED")
            target_row = len(rows) + 2

        headers = ws_users.row_values(1)
        if field not in headers:
            # если новой колонки нет — добавлять не будем, просто лог
            log.warning(f"Нет колонки '{field}' в листе Пользователи")
            return
        col = headers.index(field) + 1
        ws_users.update_cell(target_row, col, value)
    except Exception as e:
        log.error(f"Не удалось сохранить настройку пользователя: {e}")

# ========= ЧТЕНИЕ / ЗАПИСЬ ЗАДАЧ =========
def add_task(date_s, category, subcategory, text, deadline, user_id, status="", repeat="", source=""):
    row = [date_s, category, subcategory, text, deadline, str(user_id), status, repeat, source]
    ws_tasks.append_row(row, value_input_option="USER_ENTERED")

def tasks_for_date(user_id, date_s):
    return [r for r in get_all(ws_tasks) if str(r.get("User ID")) == str(user_id) and (r.get("Дата") or "") == date_s]

def tasks_for_week(user_id, base_date=None):
    if base_date is None:
        base_date = now_local().date()
    days = {(base_date + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)}
    return [r for r in get_all(ws_tasks) if str(r.get("User ID")) == str(user_id) and (r.get("Дата") or "") in days]

def find_row_by_unique(date_s, text, user_id):
    # Находим первую строку по Дата+Задача+User ID
    try:
        cell = ws_tasks.find(text)
        if not cell:
            return None
        # проверим совпадение по дате и юзеру для найденной строки
        row_vals = ws_tasks.row_values(cell.row)
        hdr = ws_tasks.row_values(1)
        row = dict(zip(hdr, row_vals))
        if row.get("Дата") == date_s and str(row.get("User ID")) == str(user_id):
            return cell.row
    except Exception:
        return None
    return None

def mark_done_by_supplier_today(user_id, supplier_key, point=None, match_all=False):
    """Пометить как выполненные «сегодня» задачи по поставщику (и ТТ), вернуть список изменённых."""
    supplier_key = (supplier_key or "").strip().lower()
    today = today_str()
    changed = []
    for r in get_all(ws_tasks):
        if str(r.get("User ID")) != str(user_id): continue
        if (r.get("Дата") or "") != today: continue
        task_text = (r.get("Задача") or "").lower()
        src = (r.get("Источник") or "").strip().lower()
        # распознаём: по тексту и/или источнику
        hit_text = supplier_key and supplier_key in task_text
        if not hit_text and supplier_key in ("к-экспро","ип вылегжанина"):
            hit_text = supplier_key in task_text
        if not hit_text:
            continue
        if point:
            pt = point.strip().lower()
            # точка может быть в подкатегории или в тексте
            tt_text = (r.get("Подкатегория") or "").strip().lower()
            if pt not in tt_text and pt not in task_text:
                continue
        if (r.get("Статус") or "").lower() == "выполнено":
            continue
        row_idx = find_row_by_unique(today, r.get("Задача") or "", user_id)
        if row_idx:
            ws_tasks.update_cell(row_idx, TASK_COLS.index("Статус")+1, "выполнено")
            changed.append(r)
            if not match_all:
                break
    return changed

# ========= РАСШИРЕНИЕ ПОВТОРОВ =========
def expand_repeats_for_date(target_dt: datetime):
    """
    Создаёт задачи на дату target_dt из «шаблонов» в листе «Все задачи».
    Шаблон = строка, где Дата == "" и заполнено поле «Повторяемость».
    Поддерживаются:
      - «каждые N дней HH:MM» (по эпохе 2025-01-01)
      - «каждый вторник HH:MM» / «каждый четверг»
      - «по пн,ср,пт HH:MM»
      - «к-экспро», «ип вылегжанина» — спец-правила (ниже)
    """
    date_s = target_dt.strftime("%d.%m.%Y")
    weekday = WEEKDAYS_RU[target_dt.weekday()]  # 'среда'
    epoch = datetime(2025,1,1, tzinfo=LOCAL_TZ).date()

    existing = {(r.get("User ID"), r.get("Дата"), r.get("Задача")) for r in get_all(ws_tasks)}

    for r in get_all(ws_tasks):
        if (r.get("Дата") or "").strip():
            continue  # это не шаблон
        rule = (r.get("Повторяемость") or "").strip().lower()
        if not rule:
            continue
        user_id = str(r.get("User ID") or "").strip()
        cat = (r.get("Категория") or "").strip()
        subcat = (r.get("Подкатегория") or "").strip()
        text = (r.get("Задача") or "").strip()
        deadline = (r.get("Дедлайн") or "").strip()
        source = (r.get("Источник") or "").strip()

        should_create = False
        time_part = ""

        # спец-логика поставщиков — генерим «сегодня заказ» по шаблону дня
        if rule in ("к-экспро","кэкспро","к-экcпро","k-exp","kexp","к экспро"):
            # Ритм: заказ -> на след.день приемка -> ещё через день снова заказ
            # Шаблон интерпретируем как «каждые 2 дня», ориентируясь на эпоху
            days_since = (target_dt.date() - epoch).days
            if days_since % 2 == 0:
                should_create = True
                time_part = deadline or "14:00"
        elif "вылегжан" in rule:
            # Вылегжанина: заказ -> на след.день приемка -> через 2 дня новый заказ
            # Ставим заказы через день после приемки: итого каждые 3 дня цикл заказов
            days_since = (target_dt.date() - epoch).days
            if days_since % 3 == 0:
                should_create = True
                time_part = deadline or "14:00"
        elif rule.startswith("каждые "):
            m = re.search(r"каждые\s+(\d+)\s*дн", rule)
            if m:
                n = int(m.group(1))
                days_since = (target_dt.date() - epoch).days
                if days_since % n == 0:
                    should_create = True
            m2 = re.search(r"(\d{1,2}:\d{2})", rule)
            if m2:
                time_part = m2.group(1)
        elif rule.startswith("каждый "):
            # «каждый четверг 12:00»
            for wd in WEEKDAYS_RU:
                if wd in rule and wd == weekday:
                    should_create = True
                    break
            m2 = re.search(r"(\d{1,2}:\d{2})", rule)
            if m2:
                time_part = m2.group(1)
        elif rule.startswith("по "):
            # «по пн,ср,пт 10:30»
            short = {"пн":"понедельник","вт":"вторник","ср":"среда","чт":"четверг","пт":"пятница","сб":"суббота","вс":"воскресенье"}
            days = [d.strip() for d in rule.replace("по","").split(",")]
            expanded = [short.get(d, d) for d in days]
            if weekday in expanded:
                should_create = True
            m2 = re.search(r"(\d{1,2}:\d{2})", rule)
            if m2:
                time_part = m2.group(1)

        if should_create:
            key = (user_id, date_s, text)
            if key not in existing:
                add_task(date_s, cat, subcat, text, time_part, user_id, "", "авто", source)
                existing.add(key)

# ========= ФОРМАТИРОВАНИЕ ВЫВОДА =========
def format_grouped(tasks, header_date=None):
    if not tasks:
        return "Задач нет."
    # Сортировка: Источник → Категория → Подкатегория → Дедлайн
    def k(r):
        dl = r.get("Дедлайн") or ""
        try:
            t = datetime.strptime(dl,"%H:%M").time()
        except:
            t = datetime.min.time()
        return ((r.get("Источник") or ""), (r.get("Категория") or ""), (r.get("Подкатегория") or ""), t, (r.get("Задача") or ""))
    tasks = sorted(tasks, key=k)

    out = []
    if header_date:
        dt = datetime.strptime(header_date, "%d.%m.%Y")
        out.append(f"• {weekday_full(dt)} — {header_date}\n")

    cur_src = cur_cat = cur_sub = None
    for r in tasks:
        src = (r.get("Источник") or "").capitalize() or "—"
        cat = (r.get("Категория") or "")
        sub = (r.get("Подкатегория") or "") or "—"
        txt = (r.get("Задача") or "")
        dl  = (r.get("Дедлайн") or "")
        st  = (r.get("Статус") or "").lower()
        rep = (r.get("Повторяемость") or "").strip() != ""

        icon = "✅" if st == "выполнено" else ("🔁" if rep else "⬜")

        if src != cur_src:
            out.append(f"📂 <b>{src}</b>")
            cur_src = src; cur_cat = cur_sub = None
        if cat != cur_cat:
            out.append(f"  └ <b>{cat or '—'}</b>")
            cur_cat = cat; cur_sub = None
        if sub != cur_sub:
            out.append(f"    └ <i>{sub}</i>")
            cur_sub = sub
        line = f"      {icon} {txt}"
        if dl: line += f"  <i>(до {dl})</i>"
        out.append(line)
    return "\n".join(out)

# ========= КЛАВИАТУРЫ / СОСТОЯНИЯ =========
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя")
    kb.row("➕ Добавить", "✅ Я сделал…", "🧠 Ассистент")
    kb.row("⚙️ Настройки")
    return kb

def week_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    base = now_local().date()
    for i in range(7):
        d = base + timedelta(days=i)
        kb.add(f"{weekday_full(datetime(d.year,d.month,d.day))} ({d.strftime('%d.%m.%Y')})")
    kb.add("⬅ Назад")
    return kb

USER_STATE = {}
USER_BUF   = {}

def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None:
        USER_BUF[uid] = data

def clear_state(uid):
    USER_STATE.pop(uid, None)
    USER_BUF.pop(uid, None)

# ========= GPT АССИСТЕНТ =========
def ai_respond(prompt, context=""):
    if not OPENAI_API_KEY:
        return "Без GPT: начни с задач с ближайшим дедлайном и высокой важностью."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = ("Ты личный ассистент по задачам. Кратко, по делу, на русском. "
               "Учитывай дедлайны и источники (Кофейня/Табачка/WB/Личное). "
               "Форматируй ответ списком с эмодзи.")
        msg = f"Запрос: {prompt}\n\nЗадачи:\n{context[:3500]}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":msg}],
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"AI error: {e}")
        return "Ассистент сейчас недоступен."

# ========= ХЕНДЛЕРЫ =========
@bot.message_handler(commands=["start"])
def start(m):
    bot.send_message(m.chat.id, "Привет! Я помогу управлять задачами. Выбери действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def show_today(m):
    uid = m.chat.id
    expand_repeats_for_date(now_local())
    d = today_str()
    tasks = tasks_for_date(uid, d)
    bot.send_message(uid, f"📅 Задачи на {d}\n\n" + format_grouped(tasks, header_date=d), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def choose_day(m):
    bot.send_message(m.chat.id, "Выбери день:", reply_markup=week_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def all_week(m):
    uid = m.chat.id
    for i in range(7):
        expand_repeats_for_date(now_local() + timedelta(days=i))
    tasks = tasks_for_week(uid)
    if not tasks:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu())
        return
    by_day = {}
    for r in tasks:
        by_day.setdefault(r["Дата"], []).append(r)
    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s,"%d.%m.%Y")):
        parts.append(format_grouped(by_day[d], header_date=d))
        parts.append("")  # пустая строка между днями
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text)
def specific_day(m):
    uid = m.chat.id
    try:
        date_s = m.text.split("(")[1].strip(")")
        dt = datetime.strptime(date_s, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Не понял дату, попробуй ещё раз.", reply_markup=main_menu())
        return
    expand_repeats_for_date(LOCAL_TZ.localize(dt))
    t = tasks_for_date(uid, date_s)
    bot.send_message(uid, f"📅 Задачи на {date_s}\n\n" + format_grouped(t, header_date=date_s), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def add_start(m):
    set_state(m.chat.id, "adding_text")
    bot.send_message(m.chat.id,
        "Опиши задачу одним сообщением (можно с повторяемостью):\n"
        "Примеры:\n"
        "• Заказать К-Экспро Центр и Полет каждые 2 дня 14:00 (кофейня)\n"
        "• Вылегжанина Центр — заказ завтра 14:00 (кофейня)\n"
        "• Личное — записаться к стоматологу 20.08 10:00\n")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "adding_text")
def add_parse(m):
    uid = m.chat.id
    text = m.text.strip()
    try:
        # Очень простая эвристика + поля в понятный вид.
        # Поддержим источники:
        src = ""
        tl = text.lower()
        if "кофейн" in tl: src = "кофейня"
        elif "табач" in tl: src = "табачка"
        elif "wb" in tl or "wild" in tl: src = "wb"
        elif "личн" in tl: src = "личное"

        # Подкатегории (точки)
        sub = ""
        pts = []
        if "центр" in tl: pts.append("центр")
        if "полет" in tl or "полёт" in tl: pts.append("полет")
        if "климов" in tl: pts.append("климово")
        if pts:
            sub = ", ".join([p.capitalize() for p in pts])

        # Время
        mtime = re.search(r"(\d{1,2}:\d{2})", text)
        tpart = mtime.group(1) if mtime else ""

        # Дата (может отсутствовать — тогда шаблон)
        mdate = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
        dpart = mdate.group(1) if mdate else ""

        # Повторяемость
        repeat = ""
        if "каждые 2 дня" in tl or "каждый второй день" in tl:
            repeat = f"каждые 2 дня {tpart}".strip()
        if "каждый" in tl or "по " in tl:
            # включим как есть — пользователь пишет «каждый вторник 12:00» / «по пн,ср»
            # мы не будем портить формулировку
            repeat = (repeat or text).lower()
            # но если в repeat нет времени — допишем найденное
            if tpart and (not re.search(r"\d{1,2}:\d{2}", repeat)):
                repeat += f" {tpart}"

        # Категория — грубо
        cat = "Общее"
        if any(k in tl for k in ["заказ","закуп"]): cat = "Закупка"
        if any(k in tl for k in ["приемк","приёмк"]): cat = "Поставка"

        # Поставщик — просто подставим в текст, правила — через «Повторяемость»
        supplier_tag = ""
        if "к-экспро" in tl or "k-exp" in tl:
            supplier_tag = "к-экспро"
            if not repeat:
                repeat = "к-экспро"
        if "вылегжан" in tl:
            supplier_tag = "ип вылегжанина"
            if not repeat:
                repeat = "ип вылегжанина"

        # Текст задачи — как ввёл пользователь
        task_text = text

        if not dpart and not repeat:
            # если ни даты, ни повтора — поставим на сегодня
            dpart = today_str()

        if pts:
            # Сгенерируем строки «по точкам»
            for pt in pts:
                sub1 = pt.capitalize()
                add_task(dpart or "", cat, sub1, task_text, tpart, uid, "", repeat, src)
        else:
            add_task(dpart or "", cat, sub, task_text, tpart, uid, "", repeat, src)

        bot.send_message(uid, "✅ Добавлено.", reply_markup=main_menu())
        log_event(uid, "add_task_free", text)
    except Exception as e:
        log.error(f"add_parse error: {e}")
        bot.send_message(uid, "Не удалось добавить задачу.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def done_start(m):
    set_state(m.chat.id, "done_text")
    bot.send_message(m.chat.id,
        "Напиши что сделал. Примеры:\n"
        "• я сделал заказы к-экспро центр\n"
        "• я сделал все заказы к-экспро\n"
        "• отметил сделать задачу «Позвонить ...» как выполненную")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "done_text")
def done_parse(m):
    uid = m.chat.id
    txt = m.text.strip().lower()
    try:
        # 1) Быстрый путь — поставщики
        sup = None
        if "к-экспро" in txt or "k-exp" in txt: sup = "к-экспро"
        if "вылегжан" in txt: sup = "ип вылегжанина"
        pt = None
        if "центр" in txt: pt = "центр"
        if "полет" in txt or "полёт" in txt: pt = "полет"
        if "климов" in txt: pt = "климово"
        match_all = "все" in txt or "всё" in txt

        changed = []
        if sup:
            changed = mark_done_by_supplier_today(uid, sup, point=pt, match_all=match_all)
        else:
            # 2) По фразе «отметь задачу … как выполненную»
            # найдём первую задачку сегодня, в тексте которой есть ключевая часть
            key = None
            m1 = re.search(r"задач[ауе]\s+«(.+?)»", txt) or re.search(r"задач[ауе]\s+\"(.+?)\"", txt)
            if m1:
                key = m1.group(1).lower()
            if key:
                today = today_str()
                for r in tasks_for_date(uid, today):
                    t = (r.get("Задача") or "").lower()
                    if key in t and (r.get("Статус") or "").lower() != "выполнено":
                        row_idx = find_row_by_unique(today, r.get("Задача") or "", uid)
                        if row_idx:
                            ws_tasks.update_cell(row_idx, TASK_COLS.index("Статус")+1, "выполнено")
                            changed = [r]
                            break

        # 3) Если это был поставщик — запланируем по правилам автоматически
        if sup and changed:
            # Для простоты — создадим «приемку завтра» и «следующий заказ» по их ритмам
            base = now_local().date()
            if sup == "к-экспро":
                delivery = base + timedelta(days=1)
                next_order = base + timedelta(days=2)
            else:  # вылегжанина
                delivery = base + timedelta(days=1)
                next_order = delivery + timedelta(days=2)

            # возьмём точки из изменённых задач
            tps = list({ (r.get("Подкатегория") or "").strip() for r in changed if r.get("Подкатегория") })
            for tp in tps:
                add_task(delivery.strftime("%d.%m.%Y"), "Поставка", tp, f"Приемка {sup} ({tp})", "10:00", uid, "", "авто", "кофейня")
                add_task(next_order.strftime("%d.%m.%Y"), "Закупка", tp, f"Заказ {sup} ({tp})", "14:00", uid, "", "авто", "кофейня")

        if changed:
            bot.send_message(uid, f"✅ Отметил выполненным: {len(changed)}.", reply_markup=main_menu())
        else:
            bot.send_message(uid, "Не нашёл подходящих задач. Уточни формулировку.", reply_markup=main_menu())

        log_event(uid, "done_free", txt)
    except Exception as e:
        log.error(f"done_parse error: {e}")
        bot.send_message(uid, "Ошибка при обработке.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def assistant_start(m):
    set_state(m.chat.id, "assistant_text")
    bot.send_message(m.chat.id, "Сформулируй запрос (приоритизация, расписание и т.п.).")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "assistant_text")
def assistant_reply(m):
    uid = m.chat.id
    q = m.text.strip()
    try:
        # контекст — неделя задач
        t = tasks_for_week(uid)
        lines = []
        for r in sorted(t, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
            lines.append(f"{r['Дата']} • {r.get('Источник','')} / {r.get('Категория','')} / {r.get('Подкатегория','') or '—'} — {r.get('Задача','')} (до {r.get('Дедлайн','') or '—'}) [{r.get('Статус','')}]")
        ctx = "\n".join(lines)
        answer = ai_respond(q, ctx)
        bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())
        log_event(uid, "assistant", q)
    except Exception as e:
        log.error(f"assistant error: {e}")
        bot.send_message(uid, "Ассистент сейчас недоступен.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def settings_menu(m):
    uid = m.chat.id
    s = get_user_settings(uid)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("⏰ Время дайджеста", "📨 Утренний дайджест: " + ("Вкл" if s["digest_on"] else "Выкл"))
    kb.row("🔔 Напоминания: " + ("Вкл" if s["reminders_on"] else "Выкл"), "⏳ За сколько мин")
    kb.row("⬅ Назад")
    bot.send_message(uid,
        f"Текущие настройки:\n"
        f"• Дайджест: {'Вкл' if s['digest_on'] else 'Выкл'} в {s['digest_time']}\n"
        f"• Напоминания: {'Вкл' if s['reminders_on'] else 'Выкл'} за {s['remind_before_min']} мин",
        reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "⏰ Время дайджеста")
def set_digest_time(m):
    set_state(m.chat.id, "set_digest_time")
    bot.send_message(m.chat.id, "Напиши время в формате ЧЧ:ММ (например 08:00).")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "set_digest_time")
def save_digest_time(m):
    uid = m.chat.id
    t = m.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", t):
        bot.send_message(uid, "Формат времени ЧЧ:ММ. Попробуй ещё раз.", reply_markup=main_menu())
    else:
        set_user_setting(uid, "Время дайджеста", t)
        bot.send_message(uid, f"Готово. Дайджест в {t}.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text.startswith("📨 Утренний дайджест:"))
def toggle_digest(m):
    uid = m.chat.id
    s = get_user_settings(uid)
    new = "нет" if s["digest_on"] else "да"
    set_user_setting(uid, "Утренний дайджест", new)
    bot.send_message(uid, f"Дайджест теперь: {'Вкл' if new=='да' else 'Выкл'}.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text.startswith("🔔 Напоминания:"))
def toggle_reminders(m):
    uid = m.chat.id
    s = get_user_settings(uid)
    new = "нет" if s["reminders_on"] else "да"
    set_user_setting(uid, "Напоминания", new)
    bot.send_message(uid, f"Напоминания теперь: {'Вкл' if new=='да' else 'Выкл'}.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⏳ За сколько мин")
def set_remind_before(m):
    set_state(m.chat.id, "set_before")
    bot.send_message(m.chat.id, "Сколько минут до дедлайна присылать напоминание? Введите число, например 30.")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "set_before")
def save_remind_before(m):
    uid = m.chat.id
    try:
        val = int(m.text.strip())
        set_user_setting(uid, "За сколько мин напоминать", val)
        bot.send_message(uid, f"Ок, напоминать за {val} мин до дедлайна.", reply_markup=main_menu())
    except:
        bot.send_message(uid, "Нужно число, например 30.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def back_main(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# ========= РАССЫЛКИ / НАПОМИНАНИЯ =========
def job_morning_digest():
    """Ежедневно расширяем повторы на сегодня и шлём дайджест тем, у кого включено."""
    try:
        expand_repeats_for_date(now_local())
        users = get_all(ws_users)
        d = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            s = get_user_settings(uid)
            if not s["digest_on"]:
                continue
            # время проверяем в отдельном джобе (см. job_tick), здесь просто на случай ручного вызова
            t = tasks_for_date(uid, d)
            if t:
                bot.send_message(uid, f"📅 План на {d}\n\n" + format_grouped(t, header_date=d))
    except Exception as e:
        log.error(f"job_morning_digest error: {e}")

def send_reminders_tick():
    """Каждую минуту проверяем: у кого включены напоминания — слать перед дедлайном."""
    try:
        now = now_local()
        today = today_str()
        users = get_all(ws_users)
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            s = get_user_settings(uid)
            if not s["reminders_on"]:
                continue
            before = s["remind_before_min"]
            tlist = tasks_for_date(uid, today)
            for r in tlist:
                if (r.get("Статус") or "").lower() == "выполнено":
                    continue
                dl = (r.get("Дедлайн") or "").strip()
                if not re.match(r"^\d{1,2}:\d{2}$", dl):
                    continue
                try:
                    hh, mm = map(int, dl.split(":"))
                    dl_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    remind_at = dl_dt - timedelta(minutes=int(before))
                    # триггер по минуте
                    if remind_at.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
                        txt = r.get("Задача") or ""
                        src = (r.get("Источник") or "").capitalize()
                        sub = (r.get("Подкатегория") or "") or "—"
                        bot.send_message(uid, f"🔔 Напоминание: {txt}\nИсточник: {src} / {sub}\nДедлайн: {dl}")
                except Exception:
                    continue
    except Exception as e:
        log.error(f"send_reminders_tick error: {e}")

def job_tick():
    """Минутный тикер: персональные времена дайджеста + напоминания."""
    try:
        now = now_local()
        users = get_all(ws_users)
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            s = get_user_settings(uid)
            # Персональный дайджест
            if s["digest_on"]:
                t = s["digest_time"] or "08:00"
                if re.match(r"^\d{2}:\d{2}$", t):
                    if now.strftime("%H:%M") == t:
                        # Чтобы не шлёпать несколько раз в ту же минуту — доверимся минутному тикеру (один раз)
                        d = today_str()
                        expand_repeats_for_date(now)
                        ts = tasks_for_date(uid, d)
                        if ts:
                            bot.send_message(uid, f"📅 План на {d}\n\n" + format_grouped(ts, header_date=d))
        # Напоминания
        send_reminders_tick()
    except Exception as e:
        log.error(f"job_tick error: {e}")

def scheduler_loop():
    schedule.clear()
    # Минутный тикер
    schedule.every().minute.do(job_tick)
    # На всякий случай — «бекап» общего утреннего дайджеста на 08:00 МСК
    schedule.every().day.at("08:00").do(job_morning_digest)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ========= WEBHOOK =========
app = Flask(__name__)

@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    try:
        update = request.get_data().decode("utf-8")
        upd = types.Update.de_json(update)
        bot.process_new_updates([upd])
    except Exception as e:
        log.error(f"Webhook error: {e}")
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running!"

# ========= START =========
if __name__ == "__main__":
    if not API_TOKEN or not WEBHOOK_URL:
        raise RuntimeError("Не заданы TELEGRAM_TOKEN или WEBHOOK_BASE")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)

    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
