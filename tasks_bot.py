# tasks_bot.py
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import pytz
import hmac
import hashlib
import logging
import schedule
import threading
from datetime import datetime, timedelta

import gspread
from flask import Flask, request
from telebot import TeleBot, types

# ===================== ОКРУЖЕНИЕ / НАСТРОЙКИ =====================
API_TOKEN        = os.getenv("TELEGRAM_TOKEN")
TABLE_URL        = os.getenv("GOOGLE_SHEETS_URL")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
TZ_NAME          = os.getenv("TZ", "Europe/Moscow")
WEBHOOK_URL      = f"{WEBHOOK_BASE}/{API_TOKEN}" if API_TOKEN and WEBHOOK_BASE else None

LOCAL_TZ = pytz.timezone(TZ_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

REQUIRED_ENVS = ["TELEGRAM_TOKEN", "GOOGLE_SHEETS_URL", "WEBHOOK_BASE"]
missing = [v for v in REQUIRED_ENVS if not os.getenv(v)]
if missing:
    log.warning("Не заданы переменные окружения: %s", ", ".join(missing))

bot = TeleBot(API_TOKEN, parse_mode="HTML")

# ===================== GOOGLE SHEETS =====================
# Ключевые листы (категории задач)
CATEGORIES = ["Кофейня", "Табачка", "Личное", "WB"]
SHEET_SUPPLIERS = "Поставщики"
SHEET_USERS     = "Пользователи"
SHEET_LOGS      = "Логи"

# Ожидаемая структура колонок на листах задач
TASK_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)

    # Проверяем наличие всех листов задач, но НЕ создаём
    SHEETS = {}
    for cat in CATEGORIES:
        try:
            SHEETS[cat] = sh.worksheet(cat)
        except gspread.exceptions.WorksheetNotFound:
            log.error(f"Не найден лист задач: {cat} (создавать автоматически НЕ будем)")

    try:
        ws_suppliers = sh.worksheet(SHEET_SUPPLIERS)
    except gspread.exceptions.WorksheetNotFound:
        ws_suppliers = None
        log.error("Лист 'Поставщики' не найден.")

    try:
        ws_users = sh.worksheet(SHEET_USERS)
    except gspread.exceptions.WorksheetNotFound:
        ws_users = None
        log.error("Лист 'Пользователи' не найден.")

    try:
        ws_logs = sh.worksheet(SHEET_LOGS)
    except gspread.exceptions.WorksheetNotFound:
        ws_logs = None
        log.error("Лист 'Логи' не найден.")

    log.info("Google Sheets подключены.")
except Exception:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

# ===================== УТИЛИТЫ / ОБЩЕЕ =====================
PAGE_SIZE = 7
NOTIFIED  = set()  # для напоминаний: ключ = taskId|date|HH:MM

def now_local():
    return datetime.now(LOCAL_TZ)

def today_str(dt=None):
    if dt is None:
        dt = now_local()
    return dt.strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

def parse_time_hhmm(s):
    try:
        return datetime.strptime(s, "%H:%M").time()
    except Exception:
        return None

def log_event(user_id, action, payload=""):
    if not ws_logs: return
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception:
        pass

def sha_task_id(user_id, date_s, cat, subcat, text, deadline):
    key = f"{user_id}|{date_s}|{cat}|{subcat}|{text}|{deadline}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def is_order_task(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in ["заказ", "заказать"])

def guess_supplier(text: str) -> str:
    t = (text or "").lower()
    if "к-экспро" in t or "к экспро" in t or "k-exp" in t or "кэкспро" in t:
        return "К-Экспро"
    if "вылегжан" in t:
        return "ИП Вылегжанина"
    return ""

def normalize_tt_from_subcat(subcat: str) -> str:
    s = (subcat or "").strip().lower()
    if "центр" in s: return "Центр"
    if "полет" in s or "полёт" in s: return "Полет"
    if "климов" in s: return "Климово"
    return subcat or ""

def get_ws_by_category(cat: str):
    return SHEETS.get(cat)

def get_all_task_rows():
    """Считать все задачи со всех категорий в общий список dict."""
    all_rows = []
    for cat, ws in SHEETS.items():
        if not ws: 
            continue
        try:
            rows = ws.get_all_records()
        except Exception as e:
            log.error(f"Не удалось прочитать лист {cat}: {e}")
            rows = []
        for r in rows:
            r["Категория"] = r.get("Категория") or cat  # страховка
            all_rows.append(r)
    return all_rows

def find_row_index_by_id(ws, task_id, rows):
    # rows = ws.get_all_records(); индекс в таблице с 2-й строки
    for i, r in enumerate(rows, start=2):
        rid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        if rid == task_id:
            return i
    return None

def append_task(cat, row_vals):
    ws = get_ws_by_category(cat)
    if not ws:
        raise RuntimeError(f"Лист категории '{cat}' не найден.")
    ws.append_row(row_vals, value_input_option="USER_ENTERED")

def update_cell(ws, row_idx, col_name, value):
    col = TASK_HEADERS.index(col_name) + 1
    ws.update_cell(row_idx, col, value)

# ===================== ПРАВИЛА ПОСТАВЩИКОВ (из листа) =====================
# Поддерживаем такие типы логики:
# 1) "каждые N дней" — ключевые слова: "каждые", число в правиле
# 2) "shelf Xh / Xд" или поле "Хранение (дни) / Хранение_дней" — заказ после хранения
SUPPLIER_RULES = {}

def _safe_int(x, default=0):
    try:
        if x is None or x == "": return default
        if isinstance(x, (int, float)): return int(x)
        x = str(x).strip().replace(",", ".")
        if x.endswith("д") or x.endswith("дн"):  # "3д"
            x = re.findall(r"\d+", x)[0]
        return int(float(x))
    except Exception:
        return default

def load_supplier_rules_from_sheet():
    SUPPLIER_RULES.clear()
    if not ws_suppliers:
        log.warning("Лист 'Поставщики' недоступен — правила не загружены.")
        return
    try:
        rows = ws_suppliers.get_all_records()
        for r in rows:
            name = str(r.get("Поставщик") or "").strip()
            if not name: 
                continue
            active = str(r.get("Активен") or "").strip().lower() in ("1","да","true","y","yes","on")
            auto   = str(r.get("Авто") or "").strip().lower() in ("1","да","true","y","yes","on")
            rule_s = str(r.get("Правило") or "").strip().lower()
            deadline = (str(r.get("ДедлайнЗаказа") or "14:00")).strip()
            emoji = (str(r.get("Emoji") or "📦")).strip()

            deliv_off = _safe_int(r.get("DeliveryOffsetDays"), 1)
            storage_days = _safe_int(r.get("Хранение (дни)") or r.get("Хранение_дней"), 0)
            start_cycle = str(r.get("Старт_цикла") or "").strip()  # опционально: ДД.ММ.ГГГГ

            if not active:
                continue

            rule = {"name": name, "deadline": deadline, "emoji": emoji,
                    "delivery_offset_days": max(0, deliv_off),
                    "storage_days": max(0, storage_days),
                    "auto": auto, "start_cycle": start_cycle}

            # парсим «каждые N дней»
            if "каждые" in rule_s:
                nums = re.findall(r"\d+", rule_s)
                rule["kind"] = "cycle_every_n_days"
                rule["order_every_days"] = int(nums[0]) if nums else 2
            elif "shelf" in rule_s or "час" in rule_s or "хран" in rule_s:
                # хранение -> новый заказ после хранения
                rule["kind"] = "delivery_shelf_then_order"
                # storage_days уже учтён
            else:
                # дефолт — «каждые 2 дня»
                rule["kind"] = "cycle_every_n_days"
                rule["order_every_days"] = 2

            SUPPLIER_RULES[name.lower()] = rule
    except Exception as e:
        log.warning(f"Не смог загрузить правила поставщиков: {e}")

load_supplier_rules_from_sheet()

def plan_next_by_supplier_rule(user_id, supplier_name, category, subcategory, base_task_text):
    """
    При отметке заказа как выполненного: ставим приёмку и новый заказ по правилам поставщика.
    """
    key = (supplier_name or "").strip().lower()
    rule = SUPPLIER_RULES.get(key)
    if not rule:
        return []

    created = []
    today = now_local().date()
    cat = category or "Кофейня"
    sub = normalize_tt_from_subcat(subcategory)

    def _add(date_dt, text, deadline, source):
        row = [
            date_dt.strftime("%d.%m.%Y"),
            cat, sub, text, deadline, str(user_id),
            "", "", source
        ]
        append_task(cat, row)

    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule.get("delivery_offset_days", 1))
        next_order_day = today + timedelta(days=rule.get("order_every_days", 2))
        _add(delivery_day, f"{rule['emoji']} Принять поставку {supplier_name} ({sub or '—'})", "10:00", f"auto:delivery:{supplier_name}")
        _add(next_order_day, f"{rule['emoji']} Заказать {supplier_name} ({sub or '—'})", rule.get("deadline", "14:00"), f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))

    elif rule["kind"] == "delivery_shelf_then_order":
        delivery_day = today + timedelta(days=rule.get("delivery_offset_days", 1))
        # новый заказ после хранения (в днях)
        next_order_day = delivery_day + timedelta(days=rule.get("storage_days", 2))
        _add(delivery_day, f"{rule['emoji']} Принять поставку {supplier_name} ({sub or '—'})", "11:00", f"auto:delivery:{supplier_name}")
        _add(next_order_day, f"{rule['emoji']} Заказать {supplier_name} ({sub or '—'})", rule.get("deadline", "14:00"), f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))

    return created

# ===================== ФОРМАТИРОВАНИЕ =====================
def format_grouped(tasks, header_date=None):
    if not tasks:
        return "Задач нет."
    def k(r):
        dl = r.get("Дедлайн") or ""
        t = parse_time_hhmm(dl)
        return (r.get("Категория",""), r.get("Подкатегория",""), t or datetime.min.time(), r.get("Задача",""))
    tasks = sorted(tasks, key=k)

    out = []
    if header_date:
        dt = datetime.strptime(header_date, "%d.%m.%Y")
        out.append(f"• {weekday_ru(dt)} — {header_date}\n")
    cur_cat = cur_sub = None
    for r in tasks:
        cat = r.get("Категория","") or "—"
        sub = r.get("Подкатегория","") or "—"
        txt = r.get("Задача","")
        dl  = r.get("Дедлайн","")
        st  = (r.get("Статус","") or "").lower()
        rep = (r.get("Повторяемость","") or "").strip()
        icon = "✅" if st == "выполнено" else ("🔁" if rep else "⬜")
        if cat != cur_cat:
            out.append(f"📂 <b>{cat}</b>"); cur_cat = cat; cur_sub = None
        if sub != cur_sub:
            out.append(f"  └ <b>{sub}</b>"); cur_sub = sub
        line = f"    └ {icon} {txt}"
        if dl: line += f"  <i>(до {dl})</i>"
        out.append(line)
    return "\n".join(out)

def build_task_line(r, i=None):
    dl = r.get("Дедлайн") or "—"
    prefix = f"{i}. " if i is not None else ""
    return f"{prefix}{r.get('Категория','—')}/{r.get('Подкатегория','—')}: {r.get('Задача','')[:40]}… (до {dl})"

def get_tasks_by_date(user_id, date_s):
    uid = str(user_id)
    rows = get_all_task_rows()
    return [r for r in rows if str(r.get("User ID")) == uid and (r.get("Дата") or "") == date_s]

def get_tasks_between(user_id, start_date: datetime.date, days=7):
    uid = str(user_id)
    date_set = {(start_date + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days)}
    rows = get_all_task_rows()
    return [r for r in rows if str(r.get("User ID")) == uid and (r.get("Дата") or "") in date_set]

# ===================== ПАГИНАЦИЯ / INLINE CB =====================
def mk_cb(action, **kwargs):
    payload = {"a": action, **kwargs}
    s = json.dumps(payload, ensure_ascii=False)
    sig = hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6]
    return f"{sig}|{s}"

def parse_cb(data):
    try:
        sig, s = data.split("|", 1)
        check = hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6]
        if sig != check:
            return None
        return json.loads(s)
    except Exception:
        return None

def page_buttons(items, page, total_pages, prefix_action):
    kb = types.InlineKeyboardMarkup()
    for it in items:
        kb.add(types.InlineKeyboardButton(it[0], callback_data=mk_cb(prefix_action, id=it[1])))
    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa=prefix_action)))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa=prefix_action)))
    if nav:
        kb.row(*nav)
    return kb

def render_task_card(uid, task_id):
    # ищем задачу и конкретный лист
    for cat, ws in SHEETS.items():
        if not ws: 
            continue
        rows = ws.get_all_records()
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx:
            continue
        r = rows[idx-2]
        if str(r.get("User ID")) != str(uid):
            continue
        date_s = r.get("Дата","")
        header = (
            f"<b>{r.get('Задача','')}</b>\n"
            f"📅 {weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))} — {date_s}\n"
            f"📁 {r.get('Категория',cat)} / {r.get('Подкатегория','—')}\n"
            f"⏰ Дедлайн: {r.get('Дедлайн') or '—'}\n"
            f"📝 Статус: {r.get('Статус') or '—'}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=task_id)))
        if is_order_task(r.get("Задача","")):
            kb.add(types.InlineKeyboardButton("🚚 Принять поставку", callback_data=mk_cb("accept_delivery", id=task_id)))
        kb.add(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("add_sub", id=task_id)))
        kb.add(types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("remind_set", id=task_id)))
        kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data=mk_cb("close_card")))
        return header, kb
    return "Задача не найдена.", None

# ===================== МЕНЮ =====================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня", "📆 Неделя")
    kb.row("➕ Добавить", "🧩 Мастер добавления", "🔎 Найти")
    kb.row("✅ Выполнить", "🚚 Закупочный день", "🧠 Ассистент")
    kb.row("⚙️ Настройки")
    return kb

def supply_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📦 Заказы сегодня", "📥 Приёмки сегодня")
    kb.row("🆕 Добавить поставщика", "⬅ Назад")
    return kb

# ===================== GPT: ПАРСИНГ / АССИСТЕНТ =====================
def ai_parse_to_tasks(text, fallback_user_id):
    """
    Возвращает список объектов:
    {date, time, category, subcategory, task, repeat, supplier}
    """
    items = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов.\n"
                "Объект: {date:'ДД.ММ.ГГГГ'|'' , time:'ЧЧ:ММ'|'' , category:'Кофейня|Табачка|Личное|WB'|'' , "
                "subcategory:'' , task:'', repeat:''|описание , supplier:''|имя}.\n"
                "Если есть слова 'К-Экспро' или 'ИП Вылегжанина' — это заказ. "
                "Подкатегория может быть 'Центр'/'Полет', если встречается."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":text}],
                temperature=0.1
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict): parsed = [parsed]
            for it in parsed:
                items.append({
                    "date": it.get("date") or "",
                    "time": it.get("time") or "",
                    "category": it.get("category") or "",
                    "subcategory": it.get("subcategory") or "",
                    "task": it.get("task") or "",
                    "repeat": it.get("repeat") or "",
                    "supplier": it.get("supplier") or ""
                })
            used_ai = True
        except Exception as e:
            log.error(f"AI parse failed: {e}")

    if not used_ai:
        # фоллбек
        tl = text.lower()
        cat = ""
        if any(x in tl for x in ["кофейн","к-экспро","вылегжан"]): cat = "Кофейня"
        elif "табач" in tl: cat = "Табачка"
        elif "wb" in tl: cat = "WB"
        else: cat = "Личное"
        sub = "Центр" if "центр" in tl else ("Полет" if ("полет" in tl or "полёт" in tl) else "")
        dl = re.search(r"(\d{1,2}:\d{2})", text)
        deadline = dl.group(1) if dl else ""
        if "сегодня" in tl: d = today_str()
        elif "завтра" in tl: d = today_str(now_local()+timedelta(days=1))
        else:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text); d = m.group(1) if m else ""
        items = [{
            "date": d, "time": deadline, "category": cat, "subcategory": sub,
            "task": text.strip(), "repeat":"", "supplier":""
        }]
    return items

def ai_assist_answer(query, user_id):
    try:
        rows = get_tasks_between(user_id, now_local().date(), 7)
        brief = []
        for r in sorted(rows, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
            brief.append(f"{r['Дата']} • {r['Категория']}/{r['Подкатегория'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
        context = "\n".join(brief)[:4000]
        if not OPENAI_API_KEY:
            return "Совет: начни с задач с ближайшим дедлайном; крупные задачи разбей на 2–3 подзадачи."
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = "Ты операционный ассистент по задачам и закупкам. Отвечай кратко, буллетами, на русском."
        prompt = f"Запрос: {query}\n\nКонтекст задач на 7 дней:\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"AI assistant error: {e}")
        return "Не удалось сформировать ответ ассистента."

# ===================== СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЯ =====================
USER_STATE = {}   # uid -> state
USER_DATA  = {}   # uid -> dict

def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None:
        USER_DATA[uid] = data

def get_state(uid):
    return USER_STATE.get(uid)

def get_data(uid):
    return USER_DATA.get(uid, {})

def clear_state(uid):
    USER_STATE.pop(uid, None)
    USER_DATA.pop(uid, None)

# ===================== ХЕЛПЕРЫ ДЛЯ ДОБАВЛЕНИЯ =====================
def add_task_smart(cat, date_s, subcat, text, deadline, user_id, repeat="", source=""):
    row = [date_s, cat, subcat, text, deadline, str(user_id), "", repeat, source]
    append_task(cat, row)

def mark_done_by_id(task_id, user_id):
    for cat, ws in SHEETS.items():
        if not ws: continue
        rows = ws.get_all_records()
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx: 
            continue
        r = rows[idx-2]
        if str(r.get("User ID")) != str(user_id):
            return False, None
        update_cell(ws, idx, "Статус", "выполнено")
        return True, r
    return False, None

# ===================== ОБРАБОТЧИКИ КОМАНД/МЕНЮ =====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам и закупкам. Что делаем?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    d = today_str()
    rows = get_tasks_by_date(uid, d)
    if not rows:
        bot.send_message(uid, f"📅 Задачи на {d}\n\nЗадач нет.", reply_markup=main_menu()); return
    # список -> карточки (inline)
    items = []
    for r in rows:
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r), tid))
    page = 1
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    header = f"📅 Задачи на {d}\n\n" + format_grouped(rows, header_date=d)
    bot.send_message(uid, header, reply_markup=main_menu())
    bot.send_message(uid, "Открой карточку задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week(m):
    uid = m.chat.id
    tasks = get_tasks_between(uid, now_local().date(), 7)
    if not tasks:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu()); return
    by_day = {}
    for r in tasks:
        by_day.setdefault(r["Дата"], []).append(r)
    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        parts.append(format_grouped(by_day[d], header_date=d))
        parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add_free(m):
    uid = m.chat.id
    set_state(uid, "adding_text")
    bot.send_message(uid, "Опиши задачу одним сообщением. Я распаршу дату/время/категорию.\nНапр.: «Заказать К-Экспро Центр завтра до 14:00»")

@bot.message_handler(func=lambda msg: msg.text == "🧩 Мастер добавления")
def handle_add_wizard(m):
    uid = m.chat.id
    set_state(uid, "wizard_cat")
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(*CATEGORIES)
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Выбери категорию:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    set_state(uid, "search_text")
    bot.send_message(uid, "Что ищем? Введи часть названия/категории/подкатегории/даты.")

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_menu(m):
    uid = m.chat.id
    set_state(uid, "done_text")
    bot.send_message(uid, "Напиши, что выполнено. Примеры:\n<b>сделал заказ к-экспро центр</b>\n<b>закрыли заказ вылегжанина</b>")

@bot.message_handler(func=lambda msg: msg.text == "🚚 Закупочный день")
def handle_supply(m):
    bot.send_message(m.chat.id, "Меню закупок:", reply_markup=supply_menu())

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def handle_today_orders(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_tasks_by_date(uid, date_s)
    orders = [r for r in rows if is_order_task(r.get("Задача",""))]
    if not orders:
        bot.send_message(uid, "Сегодня заказов нет.", reply_markup=supply_menu()); return
    items = []
    for i,r in enumerate(orders, start=1):
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r, i), tid))
    kb = types.InlineKeyboardMarkup()
    for text, tid in items:
        kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
    bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "📥 Приёмки сегодня")
def handle_today_deliveries(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_tasks_by_date(uid, date_s)
    deliveries = [r for r in rows if "принять поставку" in (r.get("Задача","") or "").lower()]
    if not deliveries:
        bot.send_message(uid, "На сегодня приёмок нет.", reply_markup=supply_menu()); return
    items = []
    for i,r in enumerate(deliveries, start=1):
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r, i), tid))
    kb = types.InlineKeyboardMarkup()
    for text, tid in items:
        kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
    bot.send_message(uid, "Приёмки на сегодня:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    uid = m.chat.id
    set_state(uid, "add_supplier")
    bot.send_message(uid, "Формат: <b>Поставщик; Правило; ДедлайнЗаказа; Emoji; DeliveryOffsetDays; Хранение_дней; Старт_цикла; Авто(1/0); Активен(1/0)</b>\n\nНапр.: \nК-Экспро; каждые 2 дня; 14:00; 📦; 1; 0; ; 1; 1")

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    uid = m.chat.id
    set_state(uid, "assistant_text")
    bot.send_message(uid, "Что нужно? (спланировать день, выделить приоритеты, составить график закупок и т.д.)")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text in ["⬅ Назад","⬅ Отмена"])
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# ===================== ТЕКСТОВЫЕ СОСТОЯНИЯ =====================
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "adding_text")
def adding_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    try:
        items = ai_parse_to_tasks(txt, uid)
        created = 0
        for it in items:
            cat  = it["category"] or "Личное"
            ws = get_ws_by_category(cat)
            if not ws:
                bot.send_message(uid, f"Не найден лист категории «{cat}». Задача пропущена.", reply_markup=main_menu())
                continue
            date_s = it["date"] or today_str()
            sub    = it["subcategory"] or ""
            task   = it["task"]
            tm     = it["time"] or ""
            repeat = it["repeat"] or ""
            source = it["supplier"] or ""
            add_task_smart(cat, date_s, sub, task, tm, uid, repeat=repeat, source=source)
            created += 1
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=main_menu())
        log_event(uid, "add_task_nlp", txt)
    except Exception as e:
        log.error(f"adding_text error: {e}")
        bot.send_message(uid, "Не смог добавить. Попробуй иначе.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "search_text")
def search_text(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    found = []
    for cat, ws in SHEETS.items():
        if not ws: continue
        rows = ws.get_all_records()
        for r in rows:
            if str(r.get("User ID")) != str(uid): 
                continue
            hay = " ".join([str(r.get(k,"")) for k in TASK_HEADERS]).lower()
            if q in hay:
                tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",cat), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
                found.append((build_task_line(r), tid))
    if not found:
        bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); clear_state(uid); return
    total_pages = (len(found)+PAGE_SIZE-1)//PAGE_SIZE
    page = 1
    slice_items = found[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    bot.send_message(uid, "Найденные задачи:", reply_markup=kb)
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "done_text")
def done_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()
    supplier = guess_supplier(txt)
    # Закрываем сегодняшние подходящие задачи
    changed = 0
    last_closed = None
    rows = get_tasks_by_date(uid, today_str())
    for r in rows:
        if (r.get("Статус","") or "").lower() == "выполнено":
            continue
        t = (r.get("Задача","") or "").lower()
        if supplier and supplier.lower() not in t:
            continue
        if not supplier and not any(w in t for w in ["к-экспро","вылегжан","заказ","сделал","оформил"]):
            continue
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        ok, _r = mark_done_by_id(tid, uid)
        if ok:
            changed += 1
            last_closed = _r
    msg = f"✅ Отмечено выполненным: {changed}."
    if changed and last_closed and supplier:
        created = plan_next_by_supplier_rule(uid, supplier, last_closed.get("Категория","Кофейня"), last_closed.get("Подкатегория",""), last_closed.get("Задача",""))
        if created:
            msg += "\n🔮 Запланировано: " + ", ".join([f"приёмка {d1.strftime('%d.%m')} → заказ {d2.strftime('%d.%m')}" for d1,d2 in created])
    bot.send_message(uid, msg, reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "assistant_text")
def assistant_text(m):
    uid = m.chat.id
    answer = ai_assist_answer(m.text.strip(), uid)
    bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_supplier")
def add_supplier_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    if not ws_suppliers:
        bot.send_message(uid, "Лист 'Поставщики' недоступен.", reply_markup=supply_menu()); clear_state(uid); return
    try:
        # Поставщик; Правило; ДедлайнЗаказа; Emoji; DeliveryOffsetDays; Хранение_дней; Старт_цикла; Авто; Активен
        parts = [p.strip() for p in txt.split(";")]
        while len(parts) < 9:
            parts.append("")
        ws_suppliers.append_row(parts[:9], value_input_option="USER_ENTERED")
        load_supplier_rules_from_sheet()
        bot.send_message(uid, f"✅ Поставщик «{parts[0]}» добавлен.", reply_markup=supply_menu())
    except Exception as e:
        log.error(f"add_supplier error: {e}")
        bot.send_message(uid, "Не получилось добавить поставщика.", reply_markup=supply_menu())
    finally:
        clear_state(uid)

# ====== МАСТЕР ДОБАВЛЕНИЯ (категория → подкатегория → текст → дата → время → повтор) ======
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_cat")
def wizard_cat(m):
    uid = m.chat.id
    cat = (m.text or "").strip()
    if cat not in CATEGORIES:
        bot.send_message(uid, "Выбери категорию кнопкой.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(*[types.KeyboardButton(c) for c in CATEGORIES])); return
    set_state(uid, "wizard_sub", {"cat":cat})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Центр","Полет","—")
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Выбери подкатегорию (или «—»):", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_sub")
def wizard_sub(m):
    uid = m.chat.id
    data = get_data(uid)
    sub = (m.text or "").strip()
    if sub == "—": sub = ""
    set_state(uid, "wizard_task", {"cat":data["cat"], "sub":sub})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Введи название задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_task")
def wizard_task(m):
    uid = m.chat.id
    data = get_data(uid)
    task = m.text.strip()
    set_state(uid, "wizard_date", {"cat":data["cat"], "sub":data["sub"], "task":task})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Сегодня","Завтра")
    kb.row("📅 Другая дата","⬅ Отмена")
    bot.send_message(uid, "Выбери дату:", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_date")
def wizard_date(m):
    uid = m.chat.id
    data = get_data(uid)
    t = m.text.strip().lower()
    if t == "сегодня": ds = today_str()
    elif t == "завтра": ds = today_str(now_local()+timedelta(days=1))
    elif t in ["📅 другая дата","другая дата"]:
        set_state(uid, "wizard_date_manual", data)
        bot.send_message(uid, "Введи дату ДД.ММ.ГГГГ:")
        return
    else:
        bot.send_message(uid, "Выбери дату кнопкой или «📅 Другая дата»."); return
    set_state(uid, "wizard_time", {**data, "date": ds})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("09:00","12:00","14:00","16:00","—")
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Выбери дедлайн (или «—»):", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_date_manual")
def wizard_date_manual(m):
    uid = m.chat.id
    data = get_data(uid)
    ds = m.text.strip()
    try:
        datetime.strptime(ds, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Неверный формат. Нужен ДД.ММ.ГГГГ."); return
    set_state(uid, "wizard_time", {**data, "date": ds})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("09:00","12:00","14:00","16:00","—")
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Выбери дедлайн (или «—»):", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_time")
def wizard_time(m):
    uid = m.chat.id
    data = get_data(uid)
    t = m.text.strip()
    if t == "—":
        tm = ""
    else:
        if not re.fullmatch(r"\d{1,2}:\d{2}", t):
            bot.send_message(uid, "Формат ЧЧ:ММ или «—»."); return
        tm = t
    set_state(uid, "wizard_repeat", {**data, "time": tm})
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("—","каждый день","каждые 2 дня","каждую неделю")
    kb.row("⬅ Отмена")
    bot.send_message(uid, "Повторяемость:", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "wizard_repeat")
def wizard_repeat(m):
    uid = m.chat.id
    data = get_data(uid)
    rep = m.text.strip()
    if rep == "—": rep = ""
    # записываем
    try:
        add_task_smart(data["cat"], data["date"], data["sub"], data["task"], data["time"], uid, repeat=rep)
        bot.send_message(uid, "✅ Задача добавлена.", reply_markup=main_menu())
        log_event(uid, "add_task_wizard", json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log.error(f"wizard add error: {e}")
        bot.send_message(uid, "Не удалось добавить задачу (проверь, что лист категории существует).", reply_markup=main_menu())
    finally:
        clear_state(uid)

# ===================== INLINE CALLBACKS =====================
@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid = c.message.chat.id
    data = parse_cb(c.data) if c.data and c.data != "noop" else None
    if not data:
        bot.answer_callback_query(c.id); return

    a = data.get("a")

    if a == "page":
        # Для простоты обновим список «Сегодня»
        date_s = today_str()
        rows = get_tasks_by_date(uid, date_s)
        items = []
        for r in rows:
            tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            items.append((build_task_line(r), tid))
        total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
        page = max(1, min(int(data.get("p", 1)), total_pages))
        slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
        kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
        try:
            bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=kb)
        except Exception:
            pass
        bot.answer_callback_query(c.id); return

    if a == "open":
        task_id = data.get("id")
        text, kb = render_task_card(uid, task_id)
        bot.answer_callback_query(c.id)
        if kb: bot.send_message(uid, text, reply_markup=kb)
        else:  bot.send_message(uid, text)
        return

    if a == "close_card":
        bot.answer_callback_query(c.id, "Закрыто")
        try:
            bot.delete_message(uid, c.message.message_id)
        except Exception:
            pass
        return

    if a == "done":
        task_id = data.get("id")
        ok, r = mark_done_by_id(task_id, uid)
        if not ok:
            bot.answer_callback_query(c.id, "Не смог отметить", show_alert=True); return
        supplier = guess_supplier(r.get("Задача",""))
        msg = "✅ Готово."
        if supplier:
            created = plan_next_by_supplier_rule(uid, supplier, r.get("Категория","Кофейня"), r.get("Подкатегория",""), r.get("Задача",""))
            if created: msg += " Запланирована приёмка/следующий заказ."
        bot.answer_callback_query(c.id, msg, show_alert=True)
        # перерисуем карточку
        text, kb = render_task_card(uid, task_id)
        if kb: bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
        else:  bot.edit_message_text(text, uid, c.message.message_id)
        return

    if a == "accept_delivery":
        task_id = data.get("id")
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("Сегодня", callback_data=mk_cb("accept_delivery_date", id=task_id, d="today")),
            types.InlineKeyboardButton("Завтра", callback_data=mk_cb("accept_delivery_date", id=task_id, d="tomorrow")),
        )
        kb.row(types.InlineKeyboardButton("📅 Другая дата", callback_data=mk_cb("accept_delivery_pick", id=task_id)))
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда принять поставку?", reply_markup=kb)
        return

    if a == "accept_delivery_pick":
        task_id = data.get("id")
        set_state(uid, "pick_delivery_date", {"task_id": task_id})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи дату ДД.ММ.ГГГГ:")
        return

    if a == "accept_delivery_date":
        task_id = data.get("id")
        when = data.get("d")
        # найдём исходную задачу
        base = None; base_ws = None; base_idx = None
        for cat, ws in SHEETS.items():
            if not ws: continue
            rows = ws.get_all_records()
            idx = find_row_index_by_id(ws, task_id, rows)
            if idx:
                base = rows[idx-2]; base_ws = ws; base_idx = idx; break
        if not base:
            bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
        if when == "today": date_s = today_str()
        elif when == "tomorrow": date_s = today_str(now_local()+timedelta(days=1))
        else: date_s = today_str()
        supplier = guess_supplier(base.get("Задача","")) or "Поставка"
        cat = base.get("Категория") or "Кофейня"
        sub = normalize_tt_from_subcat(base.get("Подкатегория",""))
        add_task_smart(cat, date_s, sub, f"🚚 Принять поставку {supplier} ({sub or '—'})", "10:00", uid, repeat="", source=f"subtask:{task_id}")
        bot.answer_callback_query(c.id, f"Создана задача на {date_s}", show_alert=True)
        return

    if a == "add_sub":
        task_id = data.get("id")
        set_state(uid, "add_subtask_text", {"task_id": task_id})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи текст подзадачи:")
        return

    if a == "remind_set":
        task_id = data.get("id")
        set_state(uid, "set_reminder", {"task_id": task_id})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда напомнить? Формат ЧЧ:ММ (локальное время).")
        return

# ====== ТЕКСТ ДЛЯ ПОДЗАДАЧ/ДАТЫ/НАПОМИНАНИЙ ======
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    text = m.text.strip()
    # найдём родителя
    base = None
    for cat, ws in SHEETS.items():
        if not ws: continue
        rows = ws.get_all_records()
        idx = find_row_index_by_id(ws, task_id, rows)
        if idx:
            base = (cat, rows[idx-2]); break
    if not base:
        bot.send_message(uid, "Родительская задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    cat, parent = base
    add_task_smart(cat, parent.get("Дата",""), parent.get("Подкатегория",""), f"• {text}", parent.get("Дедлайн",""), uid, repeat="", source=f"subtask:{task_id}")
    bot.send_message(uid, "Подзадача добавлена.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "pick_delivery_date")
def pick_delivery_date(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    ds = m.text.strip()
    try:
        datetime.strptime(ds, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Дата некорректна. Нужен ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    base = None
    for cat, ws in SHEETS.items():
        if not ws: continue
        rows = ws.get_all_records()
        idx = find_row_index_by_id(ws, task_id, rows)
        if idx:
            base = (cat, rows[idx-2]); break
    if not base:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    cat, r = base
    supplier = guess_supplier(r.get("Задача","")) or "Поставка"
    sub = normalize_tt_from_subcat(r.get("Подкатегория",""))
    add_task_smart(cat, ds, sub, f"🚚 Принять поставку {supplier} ({sub or '—'})", "10:00", uid, repeat="", source=f"subtask:{task_id}")
    bot.send_message(uid, f"Создана задача на {ds}.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    t = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
    # найдём запись и допишем в «Источник» маркер remind:HH:MM
    for cat, ws in SHEETS.items():
        if not ws: continue
        rows = ws.get_all_records()
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx: 
            continue
        cur_source = rows[idx-2].get("Источник","") or ""
        new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
        update_cell(ws, idx, "Источник", new_source)
        bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu())
        clear_state(uid)
        return
    bot.send_message(uid, "Не нашёл задачу.", reply_markup=main_menu()); clear_state(uid)

# ===================== ДАЙДЖЕСТ/НАПОМИНАНИЯ =====================
def job_daily_digest():
    try:
        if not ws_users: return
        users = ws_users.get_all_records()
        today = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid:
                continue
            tasks = get_tasks_by_date(uid, today)
            if not tasks:
                continue
            text = f"📅 План на {today}\n\n" + format_grouped(tasks, header_date=today)
            try:
                bot.send_message(uid, text)
            except Exception as e:
                log.error(f"send digest error {e}")
    except Exception as e:
        log.error(f"job_daily_digest error {e}")

def job_reminders():
    try:
        today = today_str()
        rows = get_all_task_rows()
        for r in rows:
            if (r.get("Дата") or "") != today: 
                continue
            src = (r.get("Источник") or "")
            if "remind:" not in src:
                continue
            matches = re.findall(r"remind:(\d{1,2}:\d{2})", src)
            for tm in matches:
                key = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн","")) + "|" + today + "|" + tm
                if key in NOTIFIED:
                    continue
                hh, mm = map(int, tm.split(":"))
                nowt = now_local().time()
                if (nowt.hour, nowt.minute) >= (hh, mm):
                    try:
                        bot.send_message(r.get("User ID"), f"⏰ Напоминание: {r.get('Категория','')}/{r.get('Подкатегория','')} — {r.get('Задача','')} (до {r.get('Дедлайн') or '—'})")
                        NOTIFIED.add(key)
                    except Exception:
                        pass
    except Exception as e:
        log.error(f"job_reminders error {e}")

def scheduler_thread():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ===================== WEBHOOK / FLASK =====================
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

# ===================== START =====================
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")))
