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

# ===================== ОКРУЖЕНИЕ =====================
API_TOKEN        = os.getenv("TELEGRAM_TOKEN")
TABLE_URL        = os.getenv("GOOGLE_SHEETS_URL")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
TZ_NAME          = os.getenv("TZ", "Europe/Moscow")
WEBHOOK_URL      = f"{WEBHOOK_BASE}/{API_TOKEN}" if API_TOKEN and WEBHOOK_BASE else None
LOCAL_TZ         = pytz.timezone(TZ_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

REQUIRED_ENVS = ["TELEGRAM_TOKEN", "GOOGLE_SHEETS_URL", "WEBHOOK_BASE"]
missing = [v for v in REQUIRED_ENVS if not os.getenv(v)]
if missing:
    log.warning("Не заданы переменные окружения: %s", ", ".join(missing))

# ===================== БОТ / SHEETS =====================
bot = TeleBot(API_TOKEN, parse_mode="HTML")

# Подключение к существующим листам. НИЧЕГО НЕ СОЗДАЁМ.
def must_get_ws(sh, title):
    try:
        return sh.worksheet(title)
    except Exception:
        log.error("Не найден лист: %s (создавать автоматически НЕ будем)", title)
        raise

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)

    # Профильные листы задач
    WS_TITLES = ["Кофейня", "Табачка", "Личное", "WB"]
    WS_MAP = {t: must_get_ws(sh, t) for t in WS_TITLES}

    # Служебные
    ws_suppliers = must_get_ws(sh, "Поставщики")
    ws_users     = must_get_ws(sh, "Пользователи")
    ws_logs      = must_get_ws(sh, "Логи")
    log.info("Google Sheets подключены.")
except Exception as e:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

TASK_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]
PAGE_SIZE = 7

# ===================== УТИЛИТЫ =====================
def now_local():
    return datetime.now(LOCAL_TZ)

def today_str(dt=None):
    if dt is None: dt = now_local()
    return dt.strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

def row_to_dict_list(ws):
    return ws.get_all_records()

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception:
        pass

def sha_task_id(user_id, date_s, cat, subcat, text, deadline):
    key = f"{user_id}|{date_s}|{cat}|{subcat}|{text}|{deadline}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def find_row_index_by_id(ws, task_id, rows):
    for i, r in enumerate(rows, start=2):
        rid = sha_task_id(str(r.get("User ID","")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        if rid == task_id:
            return i
    return None

def category_to_ws(category: str):
    """Выбираем лист для задачи по категории."""
    key = (category or "").strip()
    if key in WS_MAP:
        return WS_MAP[key]
    # Мягкое сопоставление:
    low = key.lower()
    for name in WS_TITLES:
        if name.lower() == low:
            return WS_MAP[name]
    # по умолчанию — «Личное»
    return WS_MAP["Личное"]

def all_task_rows_for_user(uid):
    """Сбор всех задач пользователя из ВСЕХ профильных листов."""
    bundle = []
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID","")) == str(uid):
                bundle.append((title, ws, r))
    return bundle

def add_task(category, date_s, subcategory, text, deadline, user_id, status="", repeat="", source=""):
    ws = category_to_ws(category)
    row = [date_s, category, subcategory, text, deadline, str(user_id), status, repeat, source]
    ws.append_row(row, value_input_option="USER_ENTERED")

def update_cell(ws, row_idx, col_name, value):
    col = TASK_HEADERS.index(col_name) + 1
    ws.update_cell(row_idx, col, value)

def is_order_task(text: str) -> bool:
    t = (text or "").lower()
    return "заказ" in t or "заказать" in t

def guess_supplier(text: str) -> str:
    t = (text or "").lower()
    if "к-экспро" in t or "к экспро" in t or "k-exp" in t:
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

# ===================== ПРАВИЛА ПОСТАВЩИКОВ =====================
SUPPLIER_RULES = {}  # name_lower -> rule dict

def load_supplier_rules_from_sheet():
    SUPPLIER_RULES.clear()
    try:
        rows = row_to_dict_list(ws_suppliers)
        for r in rows:
            name = str(r.get("Поставщик") or r.get("Название") or "").strip()
            if not name:
                continue
            name_l = name.lower()
            rule_raw = str(r.get("Правило") or r.get("Rule") or "").strip().lower()
            deadline = str(r.get("ДедлайнЗаказа") or "14:00").strip()
            d_off_raw = r.get("DeliveryOffsetDays") or r.get("СмещениеПоставкиДней") or 1
            try:
                d_off = int(d_off_raw)
            except:
                d_off = 1

            rule = None
            if "кажд" in rule_raw:
                # напр. "каждые 2 дня"
                nums = re.findall(r"\d+", rule_raw)
                n = int(nums[0]) if nums else 2
                rule = {"kind":"cycle_every_n_days","order_every_days":n,"delivery_offset_days":d_off,"order_deadline":deadline,"emoji":"📦"}
            elif "shelf" in rule_raw or "storage" in rule_raw or "72" in rule_raw or "час" in rule_raw:
                nums = re.findall(r"\d+", rule_raw)
                shelf = int(nums[0]) if nums else 72
                rule = {"kind":"delivery_shelf_then_order","delivery_offset_days":d_off,"shelf_hours":shelf,"order_deadline":deadline,"emoji":"🥘"}
            else:
                # дефолт: поставка завтра, заказ через 2 дня
                rule = {"kind":"cycle_every_n_days","order_every_days":2,"delivery_offset_days":d_off,"order_deadline":deadline,"emoji":"📦"}

            SUPPLIER_RULES[name_l] = rule
    except Exception as e:
        log.warning("Не смог загрузить правила поставщиков: %s", e)

load_supplier_rules_from_sheet()

def plan_next_by_supplier_rule(user_id, supplier_name, category, subcategory):
    key = (supplier_name or "").strip().lower()
    rule = SUPPLIER_RULES.get(key)
    if not rule:
        return []
    created = []
    today = now_local().date()

    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = today + timedelta(days=rule["order_every_days"])
        add_task(category, delivery_day.strftime("%d.%m.%Y"), subcategory,
                 f"{rule.get('emoji','📦')} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 "10:00", user_id, source=f"auto:delivery:{supplier_name}")
        add_task(category, next_order_day.strftime("%d.%m.%Y"), subcategory,
                 f"{rule.get('emoji','📦')} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 rule["order_deadline"], user_id, source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))
    else:
        delivery_day = today + timedelta(days=rule.get("delivery_offset_days",1))
        next_order_day = delivery_day + timedelta(days=2)  # после 72ч
        add_task(category, delivery_day.strftime("%d.%m.%Y"), subcategory,
                 f"{rule.get('emoji','📦')} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 "11:00", user_id, source=f"auto:delivery:{supplier_name}")
        add_task(category, next_order_day.strftime("%d.%m.%Y"), subcategory,
                 f"{rule.get('emoji','📦')} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 rule["order_deadline"], user_id, source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))
    return created

# ===================== ФОРМАТИРОВАНИЕ =====================
def format_grouped(tasks, header_date=None):
    if not tasks:
        return "Задач нет."
    def k(r):
        dl = r.get("Дедлайн") or ""
        try:
            dlk = datetime.strptime(dl, "%H:%M").time()
        except:
            dlk = datetime.min.time()
        return (r.get("Категория",""), r.get("Подкатегория",""), dlk, r.get("Задача",""))
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

# ===================== ПОЛУЧЕНИЕ ЗАДАЧ =====================
def get_tasks_by_date(uid, date_s):
    res = []
    for title, ws in WS_MAP.items():
        for r in row_to_dict_list(ws):
            if str(r.get("User ID","")) == str(uid) and r.get("Дата") == date_s:
                res.append(r)
    return res

def get_tasks_between(uid, start_dt, days=7):
    dates = {(start_dt + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days)}
    res = []
    for title, ws in WS_MAP.items():
        for r in row_to_dict_list(ws):
            if str(r.get("User ID","")) == str(uid) and r.get("Дата") in dates:
                res.append(r)
    return res

# ===================== INLINE ПАГИНАЦИЯ =====================
def mk_cb(action, **kwargs):
    payload = {"a": action, **kwargs}
    s = json.dumps(payload, ensure_ascii=False)
    sig = hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6]
    return f"{sig}|{s}"

def parse_cb(data):
    try:
        sig, s = data.split("|", 1)
        check = hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6]
        if sig != check: return None
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
    if nav: kb.row(*nav)
    return kb

# ===================== МЕНЮ =====================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить","🔎 Найти","✅ Выполнить")
    kb.row("🚚 Поставка","🧠 Ассистент","⚙️ Настройки")
    return kb

def supply_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🆕 Добавить поставщика","📦 Заказы сегодня")
    kb.row("⬅ Назад")
    return kb

# ===================== GPT: ПАРСИНГ / АССИСТЕНТ =====================
def parse_natural_datetime(text):
    """Пытаемся извлечь дату/время из текста. Сначала GPT, затем фолбэк."""
    d_s, t_s = "", ""
    used_ai = False

    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = ("Ты извлекаешь дату/время из русского текста относительно текущего времени. "
                   "Верни JSON {date:'ДД.ММ.ГГГГ'|'', time:'ЧЧ:ММ'|''}. Никаких пояснений.")
            prompt = f"Текст: {text}\nТекущая дата/время: {now_local().strftime('%d.%m.%Y %H:%M')}"
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
                temperature=0.0
            )
            raw = r.choices[0].message.content.strip()
            obj = json.loads(raw)
            d_s = obj.get("date") or ""
            t_s = obj.get("time") or ""
            used_ai = True
        except Exception as e:
            log.warning("parse_natural_datetime AI fail: %s", e)

    if not used_ai:
        low = text.lower()
        mtime = re.search(r"(\d{1,2}:\d{2})", text)
        if mtime: t_s = mtime.group(1)
        if "сегодня" in low: d_s = today_str()
        elif "завтра" in low: d_s = today_str(now_local()+timedelta(days=1))
        else:
            mdate = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if mdate: d_s = mdate.group(1)
    return d_s, t_s

def ai_parse_to_tasks(text, fallback_user_id):
    items = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив. "
                "Элементы: {category:'Кофейня|Табачка|Личное|WB', subcategory:'', task:'', date:'ДД.ММ.ГГГГ'|'', time:'ЧЧ:ММ'|'', repeat:''|описание, supplier:''|''}. "
                "Категорию выбирай из 4 фиксированных. Если не уверен — 'Личное'."
            )
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":text}],
                temperature=0.1
            )
            raw = r.choices[0].message.content.strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            for it in parsed:
                items.append({
                    "category": it.get("category") or "Личное",
                    "subcategory": it.get("subcategory") or "",
                    "task": it.get("task") or "",
                    "date": it.get("date") or "",
                    "deadline": it.get("time") or "",
                    "user_id": fallback_user_id,
                    "repeat": it.get("repeat") or "",
                    "supplier": it.get("supplier") or ""
                })
            used_ai = True
        except Exception as e:
            log.error("ai_parse_to_tasks fail: %s", e)

    if not used_ai:
        d_s, t_s = parse_natural_datetime(text)
        # примитивная категоризация
        low = text.lower()
        if any(x in low for x in ["кофейн","к-экспро","вылегжан"]):
            cat = "Кофейня"
        elif "табач" in low:
            cat = "Табачка"
        elif "wb" in low or "wild" in low:
            cat = "WB"
        else:
            cat = "Личное"
        sub = "Центр" if "центр" in low else ("Полет" if ("полет" in low or "полёт" in low) else "")
        items = [{
            "category": cat, "subcategory": sub, "task": text.strip(),
            "date": d_s or today_str(), "deadline": t_s or "",
            "user_id": fallback_user_id, "repeat":"", "supplier":""
        }]
    return items

def ai_assist_answer(query, user_id):
    try:
        rows = get_tasks_between(user_id, now_local().date(), 14)
        brief = []
        for r in sorted(rows, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
            brief.append(f"{r['Дата']} • {r['Категория']}/{r['Подкатегория'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
        context = "\n".join(brief)[:6000]
        if not OPENAI_API_KEY:
            return "Совет: начни с ближайших дедлайнов. Сложные задачи разбей на короткие шаги с конкретным временем."

        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = "Ты умный ассистент по задачам. Говори кратко, по делу, на русском, маркерами."
        prompt = f"Запрос: {query}\n\nМои задачи (14 дней):\n{context}\n\nСформируй предложенный план с приоритетами и временем."
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.error("AI assistant error: %s", e)
        return "Не удалось сформировать ответ ассистента."

# ===================== СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЯ =====================
USER_STATE = {}
USER_DATA  = {}

def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None:
        USER_DATA[uid] = data

def get_state(uid): return USER_STATE.get(uid)
def get_data(uid):  return USER_DATA.get(uid, {})
def clear_state(uid):
    USER_STATE.pop(uid, None); USER_DATA.pop(uid, None)

# ===================== ХЕНДЛЕРЫ КОМАНД/МЕНЮ =====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я готов. Выбирай действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_tasks_by_date(uid, date_s)
    items = []
    for r in rows:
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r), tid, r.get("Категория","Личное")))
    if not items:
        bot.send_message(uid, f"📅 Задачи на {date_s}\n\nЗадач нет.", reply_markup=main_menu()); return

    # пагинация
    page = 1
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = types.InlineKeyboardMarkup()
    for text, tid, _ in slice_items:
        kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
    # навигация
    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa="open")))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa="open")))
    if nav: kb.row(*nav)

    header = f"📅 Задачи на {date_s}\n\n" + format_grouped(rows, header_date=date_s)
    bot.send_message(uid, header, reply_markup=kb)

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
def handle_add(m):
    uid = m.chat.id
    set_state(uid, "add_name")
    bot.send_message(uid, "Напиши название задачи (без даты/времени).")

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    set_state(uid, "search_text")
    bot.send_message(uid, "Что ищем? Введи часть названия/категории/подкатегории/даты.")

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_menu(m):
    uid = m.chat.id
    set_state(uid, "done_text")
    bot.send_message(uid, "Опиши, что выполнено. Пример: «сделал заказ к-экспро центр»")

@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставка")
def handle_supply(m):
    bot.send_message(m.chat.id, "Меню поставок:", reply_markup=supply_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    uid = m.chat.id
    set_state(uid, "add_supplier")
    bot.send_message(uid, "Введи: <b>Название; Правило; Дедлайн</b>\nНапр.: «К-Экспро; каждые 2 дня; 14:00»")

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def handle_today_orders(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_tasks_by_date(uid, date_s)
    orders = [r for r in rows if is_order_task(r.get("Задача",""))]
    if not orders:
        bot.send_message(uid, "Сегодня заказов нет.", reply_markup=supply_menu()); return
    kb = types.InlineKeyboardMarkup()
    for i,r in enumerate(orders, start=1):
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        kb.add(types.InlineKeyboardButton(build_task_line(r, i), callback_data=mk_cb("open", id=tid)))
    bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    uid = m.chat.id
    set_state(uid, "assistant_text")
    bot.send_message(uid, "Что нужно? (спланировать, расставить приоритеты, найти узкие места и т.д.)")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# ===================== ПОШАГОВОЕ ДОБАВЛЕНИЕ =====================
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_name")
def add_step_name(m):
    uid = m.chat.id
    USER_DATA[uid] = {"task": m.text.strip()}
    set_state(uid, "add_category")
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("Кофейня","Табачка")
    kb.row("Личное","WB")
    kb.row("⬅ Назад")
    bot.send_message(uid, "Выбери категорию:", reply_markup=kb)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_category")
def add_step_category(m):
    uid = m.chat.id
    cat = m.text.strip()
    if cat not in WS_TITLES:
        bot.send_message(uid, "Выбери одну из кнопок категории.")
        return
    data = get_data(uid)
    data["category"] = cat
    set_state(uid, "add_subcategory", data)
    bot.send_message(uid, "Введи подкатегорию (например: Центр / Полет / или оставь пусто).")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subcategory")
def add_step_subcategory(m):
    uid = m.chat.id
    data = get_data(uid)
    data["subcategory"] = m.text.strip()
    set_state(uid, "add_date", data)
    bot.send_message(uid, "Введи дату (напр. «сегодня», «завтра», «13.08.2025»). Можно писать по-человечески.")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_date")
def add_step_date(m):
    uid = m.chat.id
    data = get_data(uid)
    ds, _ = parse_natural_datetime(m.text.strip())
    if not ds:
        bot.send_message(uid, "Не распознал дату. Напиши «сегодня», «завтра» или ДД.ММ.ГГГГ.")
        return
    data["date"] = ds
    set_state(uid, "add_time", data)
    bot.send_message(uid, "Время дедлайна? (ЧЧ:ММ). Можно пропустить, отправив «-».")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_time")
def add_step_time(m):
    uid = m.chat.id
    data = get_data(uid)
    t = m.text.strip()
    if t != "-" and not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ или «-».")
        return
    data["deadline"] = "" if t == "-" else t
    set_state(uid, "add_repeat", data)
    bot.send_message(uid, "Повторяемость? (например: «каждую среду», «каждые 2 дня»). Можно «-».")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_repeat")
def add_step_repeat(m):
    uid = m.chat.id
    data = get_data(uid)
    rep = m.text.strip()
    data["repeat"] = "" if rep == "-" else rep
    # Сохраняем
    add_task(data["category"], data["date"], data["subcategory"], data["task"], data["deadline"], uid, status="", repeat=data["repeat"], source="manual:wizard")
    clear_state(uid)
    bot.send_message(uid, "✅ Задача добавлена.", reply_markup=main_menu())

# ===================== ТЕКСТОВЫЕ СОСТОЯНИЯ =====================
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "search_text")
def search_text(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    found = []
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID","")) != str(uid): continue
            hay = " ".join([str(r.get(k,"")) for k in TASK_HEADERS]).lower()
            if q in hay:
                tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
                found.append((build_task_line(r), tid, title, ws, r))
    if not found:
        bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); clear_state(uid); return
    total_pages = (len(found)+PAGE_SIZE-1)//PAGE_SIZE
    page = 1
    slice_items = found[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = types.InlineKeyboardMarkup()
    for text, tid, *_ in slice_items:
        kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
    nav = []
    if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa="open")))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa="open")))
    if nav: kb.row(*nav)
    bot.send_message(uid, "Найденные задачи:", reply_markup=kb)
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "done_text")
def done_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()
    supplier = guess_supplier(txt)
    today = today_str()
    changed = 0
    last = None
    # Ищем сегодняшние задачи по всем листам
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        for i, r in enumerate(rows, start=2):
            if str(r.get("User ID","")) != str(uid) or r.get("Дата") != today: continue
            if r.get("Статус","").lower() == "выполнено": continue
            t = (r.get("Задача","") or "").lower()
            if supplier and supplier.lower() not in t: continue
            if not supplier and not any(w in t for w in ["заказ","сделал","выпол"]): continue
            update_cell(ws, i, "Статус", "выполнено")
            changed += 1
            last = r
    msg = f"✅ Отмечено выполненным: {changed}."
    if changed and supplier and last:
        plan_next_by_supplier_rule(uid, supplier, last.get("Категория","Личное"), last.get("Подкатегория",""))
        msg += " 🔮 Запланирована приемка/следующий заказ по правилу поставщика."
    bot.send_message(uid, msg, reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "assistant_text")
def assistant_text(m):
    uid = m.chat.id
    bot.send_message(uid, "🧠 " + ai_assist_answer(m.text.strip(), uid), reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_supplier")
def add_supplier_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    try:
        parts = [p.strip() for p in txt.split(";")]
        name = parts[0]
        rule = parts[1] if len(parts) > 1 else ""
        deadline = parts[2] if len(parts) > 2 else "14:00"
        ws_suppliers.append_row([name, rule, deadline], value_input_option="USER_ENTERED")
        load_supplier_rules_from_sheet()
        bot.send_message(uid, f"✅ Поставщик «{name}» добавлен.", reply_markup=supply_menu())
    except Exception as e:
        log.error("add_supplier error: %s", e)
        bot.send_message(uid, "Не получилось добавить поставщика.", reply_markup=supply_menu())
    finally:
        clear_state(uid)

# ===================== КАРТОЧКИ / CALLBACK =====================
def render_task_card(uid, task_id):
    # ищем задачу по всем листам
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if idx:
            r = rows[idx-2]
            date_s = r.get("Дата","")
            header = (f"<b>{r.get('Задача','')}</b>\n"
                      f"📅 {weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))} — {date_s}\n"
                      f"📁 {r.get('Категория','—')} / {r.get('Подкатегория','—')} ({title})\n"
                      f"⏰ Дедлайн: {r.get('Дедлайн') or '—'}\n"
                      f"📝 Статус: {r.get('Статус') or '—'}")
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done_card", id=task_id, title=title)))
            if is_order_task(r.get("Задача","")):
                kb.add(types.InlineKeyboardButton("🚚 Принять поставку", callback_data=mk_cb("accept_delivery", id=task_id, title=title)))
            kb.add(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("add_sub", id=task_id, title=title)))
            kb.add(types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("remind_set", id=task_id, title=title)))
            kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data=mk_cb("close_card")))
            return header, kb
    return "Задача не найдена.", None

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid = c.message.chat.id
    data = parse_cb(c.data) if c.data and c.data != "noop" else None
    if not data:
        bot.answer_callback_query(c.id); return
    a = data.get("a")

    if a == "page":
        page = int(data.get("p", 1))
        date_s = today_str()
        rows = get_tasks_by_date(uid, date_s)
        items = []
        for r in rows:
            tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            items.append((build_task_line(r), tid))
        total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
        page = max(1, min(page, total_pages))
        slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
        kb = types.InlineKeyboardMarkup()
        for text, tid in slice_items:
            kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
        nav = []
        if page > 1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa="open")))
        nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa="open")))
        if nav: kb.row(*nav)
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

    if a == "done_card":
        task_id = data.get("id")
        # найти и отметить выполненным
        for title, ws in WS_MAP.items():
            rows = row_to_dict_list(ws)
            idx = find_row_index_by_id(ws, task_id, rows)
            if not idx: continue
            r = rows[idx-2]
            update_cell(ws, idx, "Статус", "выполнено")
            supplier = guess_supplier(r.get("Задача",""))
            msg = "✅ Готово."
            if supplier:
                plan_next_by_supplier_rule(uid, supplier, r.get("Категория","Личное"), r.get("Подкатегория",""))
                msg += " Запланирована приемка/следующий заказ."
            bot.answer_callback_query(c.id, msg, show_alert=True)
            text, kb = render_task_card(uid, task_id)
            if kb: bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
            else:  bot.edit_message_text(text, uid, c.message.message_id)
            return
        bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True)
        return

    if a == "accept_delivery":
        task_id = data.get("id")
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("Сегодня",  callback_data=mk_cb("accept_delivery_date", id=task_id, d="today")),
            types.InlineKeyboardButton("Завтра",   callback_data=mk_cb("accept_delivery_date", id=task_id, d="tomorrow")),
        )
        kb.row(types.InlineKeyboardButton("📅 Другая дата", callback_data=mk_cb("accept_delivery_pick", id=task_id)))
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда принять поставку?", reply_markup=kb)
        return

    if a == "accept_delivery_pick":
        task_id = data.get("id")
        set_state(uid, "pick_delivery_date", {"task_id": task_id})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")
        return

    if a == "accept_delivery_date":
        task_id = data.get("id"); when = data.get("d")
        # найдём родителя
        parent = None
        for title, ws in WS_MAP.items():
            rows = row_to_dict_list(ws)
            idx = find_row_index_by_id(ws, task_id, rows)
            if idx:
                parent = (ws, rows[idx-2]); break
        if not parent:
            bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
        ws, r = parent
        date_s = today_str() if when=="today" else today_str(now_local()+timedelta(days=1))
        supplier = guess_supplier(r.get("Задача","")) or "Поставка"
        add_task(r.get("Категория","Личное"), date_s, r.get("Подкатегория",""),
                 f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                 "10:00", uid, source=f"subtask:{task_id}")
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
        bot.send_message(uid, "Когда напомнить? ЧЧ:ММ (локальное время).")
        return

# ===================== ТЕКСТ: подзадачи / дата / напоминания =====================
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    text = m.text.strip()

    # найдём родителя
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx: continue
        parent = rows[idx-2]
        add_task(parent.get("Категория","Личное"), parent.get("Дата",""), parent.get("Подкатегория",""),
                 f"• {text}", parent.get("Дедлайн",""), uid, source=f"subtask:{task_id}")
        bot.send_message(uid, "Подзадача добавлена.", reply_markup=main_menu())
        clear_state(uid); return
    bot.send_message(uid, "Родительская задача не найдена.", reply_markup=main_menu())
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
        bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    # найдём родителя
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx: continue
        r = rows[idx-2]
        supplier = guess_supplier(r.get("Задача","")) or "Поставка"
        add_task(r.get("Категория","Личное"), ds, r.get("Подкатегория",""),
                 f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                 "10:00", uid, source=f"subtask:{task_id}")
        bot.send_message(uid, f"Создана задача на {ds}.", reply_markup=main_menu())
        clear_state(uid); return
    bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    t = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
    # найдём задачу и поставим метку
    for title, ws in WS_MAP.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx: continue
        cur_source = rows[idx-2].get("Источник","") or ""
        new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
        update_cell(ws, idx, "Источник", new_source)
        bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu())
        clear_state(uid); return
    bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu())
    clear_state(uid)

# ===================== ДАЙДЖЕСТ / НАПОМИНАНИЯ / ПОВТОРЯЕМЫЕ =====================
NOTIFIED = set()

def job_daily_digest():
    try:
        users = row_to_dict_list(ws_users)
        today = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            tasks = get_tasks_by_date(uid, today)
            if not tasks: continue
            text = f"📅 План на {today}\n\n" + format_grouped(tasks, header_date=today)
            try:
                bot.send_message(uid, text)
            except Exception as e:
                log.error("send digest error %s", e)
    except Exception as e:
        log.error("job_daily_digest error %s", e)

def job_reminders():
    try:
        today = today_str()
        for title, ws in WS_MAP.items():
            rows = row_to_dict_list(ws)
            for r in rows:
                if r.get("Дата") != today: 
                    continue
                src = (r.get("Источник") or "")
                if "remind:" not in src: 
                    continue
                matches = re.findall(r"remind:(\d{1,2}:\d{2})", src)
                for tm in matches:
                    key = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн","")) + "|" + today + "|" + tm
                    if key in NOTIFIED: 
                        continue
                    try:
                        hh, mm = map(int, tm.split(":"))
                        nowt = now_local().time()
                        if (nowt.hour, nowt.minute) >= (hh, mm):
                            bot.send_message(r.get("User ID"), f"⏰ Напоминание: {r.get('Категория','')}/{r.get('Подкатегория','')} — {r.get('Задача','')} (до {r.get('Дедлайн') or '—'})")
                            NOTIFIED.add(key)
                    except Exception:
                        pass
    except Exception as e:
        log.error("job_reminders error %s", e)

def plan_week_for_repeats():
    """Разворачиваем повторяемые задачи на 7 дней вперёд по всем листам."""
    try:
        start = now_local().date()
        horizon = { (start + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(1,8) }
        for title, ws in WS_MAP.items():
            rows = row_to_dict_list(ws)
            for r in rows:
                rep = (r.get("Повторяемость","") or "").strip().lower()
                if not rep: 
                    continue
                base_cat = r.get("Категория","Личное")
                sub      = r.get("Подкатегория","")
                text     = r.get("Задача","")
                deadline = r.get("Дедлайн","")
                uid      = r.get("User ID","")
                # простые паттерны
                if "каждые" in rep:
                    nums = re.findall(r"\d+", rep)
                    step = int(nums[0]) if nums else 2
                    # создаём точки через step дня от сегодня
                    for i in range(step, 8, step):
                        ds = (start + timedelta(days=i)).strftime("%d.%m.%Y")
                        if ds in horizon:
                            add_task(base_cat, ds, sub, text, deadline, uid, status="", repeat="", source="recur:auto")
                elif any(x in rep for x in ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]):
                    target = {
                        "понедельник":0,"вторник":1,"среда":2,"четверг":3,"пятница":4,"суббота":5,"воскресенье":6
                    }[[d for d in target] if False else "понедельник"]  # hack for linter
                # облегчим: если встречается «среда» — ставим ближайшую среду и плюс неделю вперёд
                    for name, wd in {"понедельник":0,"вторник":1,"среда":2,"четверг":3,"пятница":4,"суббота":5,"воскресенье":6}.items():
                        if name in rep:
                            # найти ближайший день недели в горизонте
                            for i in range(1,8):
                                d = start + timedelta(days=i)
                                if d.weekday() == wd:
                                    ds = d.strftime("%d.%m.%Y")
                                    add_task(base_cat, ds, sub, text, deadline, uid, status="", repeat="", source="recur:auto")
                                    break
    except Exception as e:
        log.error("plan_week_for_repeats error %s", e)

def scheduler_thread():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_reminders)
    schedule.every().day.at("08:05").do(plan_week_for_repeats)
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
        log.error("Webhook error: %s", e)
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
