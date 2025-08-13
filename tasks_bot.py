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

# =============== ОКРУЖЕНИЕ ===============
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
miss = [v for v in REQUIRED_ENVS if not os.getenv(v)]
if miss:
    log.warning("Не заданы переменные окружения: %s", ", ".join(miss))

# =============== ИНИЦИАЛИЗАЦИЯ ===============
bot = TeleBot(API_TOKEN, parse_mode="HTML")

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)

    # Основные листы
    WS = {}
    EXPECTED_TASK_SHEETS = ["Кофейня","Табачка","WB","Личное"]
    for name in EXPECTED_TASK_SHEETS:
        try:
            WS[name] = sh.worksheet(name)
        except Exception:
            WS[name] = None
            log.error("Не найден лист задач: %s (создавать автоматически НЕ будем)", name)

    ws_suppliers = sh.worksheet("Поставщики")
    ws_users     = sh.worksheet("Пользователи")
    ws_logs      = sh.worksheet("Логи")
    log.info("Google Sheets подключены.")
except Exception as e:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

TASKS_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]

# =============== УТИЛИТЫ ===============
PAGE_SIZE = 7

def now_local():
    return datetime.now(LOCAL_TZ)

def today_str(dt=None):
    if dt is None:
        dt = now_local()
    return dt.strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload],
                           value_input_option="USER_ENTERED")
    except Exception:
        pass

def row_to_dict_list(ws):
    return ws.get_all_records() if ws else []

def sha_task_id(user_id, date_s, cat, subcat, text, deadline):
    key = f"{user_id}|{date_s}|{cat}|{subcat}|{text}|{deadline}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def find_row_index_by_id(task_id, rows):
    for i, r in enumerate(rows, start=2):
        rid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                          r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        if rid == task_id:
            return i
    return None

def ws_for_category(category: str):
    cat = (category or "").strip()
    if cat in WS and WS[cat] is not None:
        return WS[cat]
    # Мягкое соответствие
    m = {
        "коф": "Кофейня",
        "таб": "Табачка",
        "лич": "Личное",
        "wb":  "WB",
    }
    for k, v in m.items():
        if k in cat.lower() and WS.get(v):
            return WS[v]
    # по умолчанию — Личное (если есть)
    if WS.get("Личное"):
        return WS["Личное"]
    # если совсем нет — None
    return None

def task_exists(ws, date_s, category, subcategory, text, deadline, user_id):
    """Защита от дублей: ищем точное совпадение ключевых полей."""
    if not ws:
        return False
    rows = row_to_dict_list(ws)
    for r in rows:
        if (str(r.get("User ID")) == str(user_id) and
            (r.get("Дата") or "") == (date_s or "") and
            (r.get("Категория") or "") == (category or "") and
            (r.get("Подкатегория") or "") == (subcategory or "") and
            (r.get("Задача") or "") == (text or "") and
            (r.get("Дедлайн") or "") == (deadline or "")):
            return True
    return False

def add_task(date_s, category, subcategory, text, deadline, user_id, status="", repeat="", source=""):
    ws = ws_for_category(category)
    if not ws:
        raise RuntimeError(f"Не найден лист для категории «{category}». Создайте лист или укажите другую категорию.")
    if task_exists(ws, date_s, category, subcategory, text, deadline, user_id):
        log.info("Дубль пропущен: %s / %s / %s", date_s, category, text)
        return False
    row = [date_s, category, subcategory, text, deadline, str(user_id), status, repeat, source]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return True

def mark_done_by_id(task_id, user_id):
    # Ищем по всем листам
    for cat, ws in WS.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(task_id, rows)
        if not idx:
            continue
        r = rows[idx-2]
        if str(r.get("User ID")) != str(user_id):
            return False, None
        ws.update_cell(idx, TASKS_HEADERS.index("Статус")+1, "выполнено")
        return True, r
    return False, None

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

# =============== ПРАВИЛА ПОСТАВЩИКОВ ===============
SUPPLIER_RULES = {
    "к-экспро": {
        "kind": "cycle_every_n_days",
        "order_every_days": 2,
        "delivery_offset_days": 1,
        "order_deadline": "14:00",
        "emoji": "📦"
    },
    "ип вылегжанина": {
        "kind": "delivery_shelf_then_order",
        "delivery_offset_days": 1,
        "shelf_hours": 72,
        "order_deadline": "14:00",
        "emoji": "🥘"
    }
}

def load_supplier_rules_from_sheet():
    try:
        rows = row_to_dict_list(ws_suppliers)
        for r in rows:
            name = (r.get("Поставщик") or r.get("Название") or "").strip().lower()
            if not name:
                continue
            kind = (r.get("Правило") or r.get("Rule") or "").strip().lower()
            if not kind:
                continue
            d_off = int(str(r.get("DeliveryOffsetDays") or r.get("СмещениеПоставкиДней") or 1))
            order_deadline = (r.get("ДедлайнЗаказа") or "14:00").strip()
            emoji = (r.get("Emoji") or "📦").strip()
            if "каждые" in kind:
                n = int(re.findall(r"\d+", kind)[0]) if re.findall(r"\d+", kind) else 2
                SUPPLIER_RULES[name] = {
                    "kind": "cycle_every_n_days",
                    "order_every_days": n,
                    "delivery_offset_days": d_off,
                    "order_deadline": order_deadline,
                    "emoji": emoji
                }
            elif "72" in kind or "shelf" in kind or "storage" in kind:
                shelf = int(re.findall(r"\d+", kind)[0]) if re.findall(r"\d+", kind) else 72
                SUPPLIER_RULES[name] = {
                    "kind": "delivery_shelf_then_order",
                    "delivery_offset_days": d_off,
                    "shelf_hours": shelf,
                    "order_deadline": order_deadline,
                    "emoji": emoji
                }
    except Exception as e:
        log.warning("Не смог загрузить правила поставщиков из листа: %s", e)

load_supplier_rules_from_sheet()

def plan_next_by_supplier_rule(user_id, supplier_name, category, subcategory, base_task_text):
    key = (supplier_name or "").strip().lower()
    rule = SUPPLIER_RULES.get(key)
    if not rule:
        return []
    created = []
    today = now_local().date()

    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = today + timedelta(days=rule["order_every_days"])
        add_task(delivery_day.strftime("%d.%m.%Y"), category, subcategory,
                 f"{rule['emoji']} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 "10:00", user_id, status="", repeat="", source=f"auto:delivery:{supplier_name}")
        add_task(next_order_day.strftime("%d.%m.%Y"), category, subcategory,
                 f"{rule['emoji']} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 rule["order_deadline"], user_id, status="", repeat="", source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))

    elif rule["kind"] == "delivery_shelf_then_order":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = delivery_day + timedelta(days=2)
        add_task(delivery_day.strftime("%d.%m.%Y"), category, subcategory,
                 f"{rule['emoji']} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 "11:00", user_id, status="", repeat="", source=f"auto:delivery:{supplier_name}")
        add_task(next_order_day.strftime("%d.%m.%Y"), category, subcategory,
                 f"{rule['emoji']} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                 rule["order_deadline"], user_id, status="", repeat="", source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))
    return created

# =============== ЧТЕНИЕ ЗАДАЧ ===============
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

def get_all_task_rows():
    rows = []
    for cat, ws in WS.items():
        if not ws: continue
        r = row_to_dict_list(ws)
        rows.extend(r)
    return rows

def get_tasks_by_date(user_id, date_s):
    rows = get_all_task_rows()
    return [r for r in rows if str(r.get("User ID")) == str(user_id) and r.get("Дата") == date_s]

def get_tasks_between(user_id, start_dt, days=7):
    dates = {(start_dt + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days)}
    rows = get_all_task_rows()
    return [r for r in rows if str(r.get("User ID")) == str(user_id) and r.get("Дата") in dates]

# =============== ПАГИНАЦИЯ / INLINE ===============
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
    if nav: kb.row(*nav)
    return kb

def build_task_line(r, i=None):
    dl = r.get("Дедлайн") or "—"
    prefix = f"{i}. " if i is not None else ""
    return f"{prefix}{r.get('Категория','—')}/{r.get('Подкатегория','—')}: {r.get('Задача','')[:40]}… (до {dl})"

# =============== МЕНЮ ===============
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить","🔎 Найти","✅ Выполнить")
    kb.row("🚚 Поставщики","🧠 Ассистент","⚙️ Настройки")
    kb.row("☕ Кофейня","🚬 Табачка","🛒 WB","👤 Личное")
    return kb

def supply_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🆕 Добавить поставщика","📦 Заказы сегодня")
    kb.row("⬅ Назад")
    return kb

def category_menu(cat_name):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(f"📅 Сегодня — {cat_name}", f"📆 Неделя — {cat_name}")
    kb.row(f"➕ Добавить — {cat_name}", f"🔎 Найти — {cat_name}")
    kb.row("⬅ Назад")
    return kb

# =============== GPT: ПАРСИНГ И АССИСТЕНТ ===============
def ai_parse_to_tasks(text, fallback_user_id, forced_category=None):
    items = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов.\n"
                "Объект: {date:'ДД.ММ.ГГГГ'|'' , time:'ЧЧ:ММ'|'' , category:'Кофейня'|'Табачка'|'WB'|'Личное' , "
                "subcategory, task, repeat:''|описание, supplier:''|имя}.\n"
                "Если из контекста следует поставщик — заполни supplier и category (обычно 'Кофейня' или 'Закупка/Кофейня').\n"
                "Не пиши ничего кроме JSON."
            )
            user_prompt = text
            if forced_category:
                user_prompt += f"\n\nКатегорию по умолчанию считать: {forced_category}"
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":user_prompt}],
                temperature=0.1
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            for it in parsed:
                items.append({
                    "date": it.get("date") or "",
                    "category": it.get("category") or forced_category or "Личное",
                    "subcategory": it.get("subcategory") or "",
                    "task": it.get("task") or "",
                    "deadline": it.get("time") or "",
                    "user_id": fallback_user_id,
                    "repeat": it.get("repeat") or "",
                    "source": (it.get("supplier") or "")
                })
            used_ai = True
        except Exception as e:
            log.error("AI parse failed: %s", e)

    if not used_ai:
        tl = text.lower()
        # форс категория если указана
        if forced_category:
            cat = forced_category
        else:
            if any(x in tl for x in ["кофейн","к-экспро","вылегжан"]):
                cat = "Кофейня"
            elif "табач" in tl:
                cat = "Табачка"
            elif "wb" in tl or "wild" in tl:
                cat = "WB"
            else:
                cat = "Личное"
        sub = "Центр" if "центр" in tl else ("Полет" if ("полет" in tl or "полёт" in tl) else "")
        dl = re.search(r"(\d{1,2}:\d{2})", text)
        deadline = dl.group(1) if dl else ""
        d = ""
        if "сегодня" in tl: d = today_str()
        elif "завтра" in tl: d = today_str(now_local()+timedelta(days=1))
        else:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            d = m.group(1) if m else ""
        items = [{
            "date": d, "category": cat, "subcategory": sub,
            "task": text.strip(), "deadline": deadline,
            "user_id": fallback_user_id, "repeat":"", "source":""
        }]
    return items

def ai_assist_answer(query, user_id):
    try:
        rows = get_tasks_between(user_id, now_local().date(), 14)  # больше контекста
        brief = []
        for r in sorted(rows, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"),
                                             x.get("Категория",""), x.get("Подкатегория",""),
                                             x.get("Дедлайн","") or "", x.get("Задача","") or "")):
            brief.append(f"{r['Дата']} • {r['Категория']}/{r.get('Подкатегория') or '—'} — {r['Задача']} (до {r.get('Дедлайн') or '—'}) [{r.get('Статус','') or ''}]")
        context = "\n".join(brief)[:8000]
        if not OPENAI_API_KEY:
            return "Совет: начни с задач с ближайшим дедлайном и высокой важностью. Разбей крупные задачи на 2-3 подзадачи."
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = (
            "Ты умный ассистент по задачам. Возможные действия:\n"
            "- упорядочить задачи по приоритету;\n"
            "- предложить подзадачи;\n"
            "- предложить время выполнения (таймблоки);\n"
            "- выявить просрочки и повторяющиеся задачи;\n"
            "- предложить, что можно закрыть сегодня.\n"
            "Отвечай кратко, на русском, буллетами."
        )
        prompt = f"Запрос: {query}\n\nМои задачи (14 дней):\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("AI assistant error: %s", e)
        return "Не удалось сформировать ответ ассистента."

# =============== СОСТОЯНИЯ ===============
USER_STATE = {}
USER_DATA  = {}

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

# =============== ХЕНДЛЕРЫ ===============
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам. Что делаем?", reply_markup=main_menu())

# Быстрый доступ по темам
@bot.message_handler(func=lambda msg: msg.text in ["☕ Кофейня","🚬 Табачка","🛒 WB","👤 Личное"])
def handle_category_root(m):
    cat = {"☕ Кофейня":"Кофейня","🚬 Табачка":"Табачка","🛒 WB":"WB","👤 Личное":"Личное"}[m.text]
    set_state(m.chat.id, "cat_ctx", {"cat": cat})
    bot.send_message(m.chat.id, f"Раздел: <b>{cat}</b>", reply_markup=category_menu(cat))

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_tasks_by_date(uid, date_s)
    items = []
    for r in rows:
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                          r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r), tid))
    if not items:
        bot.send_message(uid, f"📅 Задачи на {date_s}\n\nЗадач нет.", reply_markup=main_menu()); return
    page = 1
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    header = f"📅 Задачи на {date_s}\n\n" + format_grouped(rows, header_date=date_s)
    bot.send_message(uid, header)
    bot.send_message(uid, "Открой карточку задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("📅 Сегодня — "))
def handle_today_cat(m):
    uid = m.chat.id
    cat = m.text.split("—",1)[1].strip()
    date_s = today_str()
    rows = [r for r in get_tasks_by_date(uid, date_s) if r.get("Категория")==cat]
    if not rows:
        bot.send_message(uid, f"В {cat} на сегодня задач нет.", reply_markup=category_menu(cat)); return
    items = []
    for r in rows:
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                          r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r), tid))
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    kb = page_buttons(items[:PAGE_SIZE], 1, total_pages, "open")
    header = f"📅 {cat}: сегодня\n\n" + format_grouped(rows, header_date=date_s)
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

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("📆 Неделя — "))
def handle_week_cat(m):
    uid = m.chat.id
    cat = m.text.split("—",1)[1].strip()
    tasks = [r for r in get_tasks_between(uid, now_local().date(), 7) if r.get("Категория")==cat]
    if not tasks:
        bot.send_message(uid, f"В {cat} на неделю задач нет.", reply_markup=category_menu(cat)); return
    by_day = {}
    for r in tasks:
        by_day.setdefault(r["Дата"], []).append(r)
    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        rows = [r for r in by_day[d] if r.get("Категория")==cat]
        parts.append(format_grouped(rows, header_date=d))
        parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=category_menu(cat))

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    uid = m.chat.id
    set_state(uid, "adding_text")
    bot.send_message(uid, "Опиши задачу одним сообщением (я сам распаршу дату/время/категорию).")

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("➕ Добавить — "))
def handle_add_cat(m):
    uid = m.chat.id
    cat = m.text.split("—",1)[1].strip()
    set_state(uid, "adding_text_cat", {"cat": cat})
    bot.send_message(uid, f"Опиши задачу (категория по умолчанию: {cat}).")

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    set_state(uid, "search_text")
    bot.send_message(uid, "Что ищем? Введи часть названия/категории/подкатегории/даты.")

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("🔎 Найти — "))
def handle_search_cat(m):
    uid = m.chat.id
    cat = m.text.split("—",1)[1].strip()
    set_state(uid, "search_text_cat", {"cat": cat})
    bot.send_message(uid, f"Что ищем в {cat}?")

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_menu(m):
    uid = m.chat.id
    set_state(uid, "done_text")
    bot.send_message(uid, "Напиши, что выполнено. Примеры:\n<b>сделал заказ к-экспро центр</b>\n<b>закрыли заказ вылегжанина</b>")

@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставщики")
def handle_supply(m):
    bot.send_message(m.chat.id, "Меню поставщиков:", reply_markup=supply_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    uid = m.chat.id
    set_state(uid, "add_supplier")
    bot.send_message(uid, "Формат:\n<b>Название; Правило; ДедлайнЗаказа(optional)</b>\nПримеры:\nК-Экспро; каждые 2 дня; 14:00\nИП Вылегжанина; shelf 72h; 14:00")

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
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                          r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r, i), tid))
    kb = types.InlineKeyboardMarkup()
    for text, tid in items:
        kb.add(types.InlineKeyboardButton(text, callback_data=mk_cb("open", id=tid)))
    bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    uid = m.chat.id
    set_state(uid, "assistant_text")
    bot.send_message(uid, "Что нужно? (спланировать день, выделить приоритеты, таймблоки и т.д.)")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# =============== ТЕКСТОВЫЕ СОСТОЯНИЯ ===============
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "adding_text")
def adding_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    try:
        items = ai_parse_to_tasks(txt, uid)
        created = 0
        for it in items:
            ok = add_task(it["date"] or today_str(), it["category"], it["subcategory"], it["task"],
                          it["deadline"], uid, status="", repeat=it["repeat"], source=it["source"])
            if ok: created += 1
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=main_menu())
        log_event(uid, "add_task_nlp", txt)
    except Exception as e:
        log.error("adding_text error: %s", e)
        bot.send_message(uid, f"Не смог добавить: {e}", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "adding_text_cat")
def adding_text_cat(m):
    uid = m.chat.id
    data = get_data(uid)
    forced_cat = data.get("cat")
    txt = m.text.strip()
    try:
        items = ai_parse_to_tasks(txt, uid, forced_category=forced_cat)
        created = 0
        for it in items:
            ok = add_task(it["date"] or today_str(), it["category"], it["subcategory"], it["task"],
                          it["deadline"], uid, status="", repeat=it["repeat"], source=it["source"])
            if ok: created += 1
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=category_menu(forced_cat))
        log_event(uid, "add_task_nlp", f"{forced_cat}: {txt}")
    except Exception as e:
        log.error("adding_text_cat error: %s", e)
        bot.send_message(uid, f"Не смог добавить: {e}", reply_markup=category_menu(forced_cat))
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) in ["search_text","search_text_cat"])
def search_text(m):
    uid = m.chat.id
    st = get_state(uid)
    q = m.text.strip().lower()
    data = get_data(uid)
    forced_cat = data.get("cat") if st == "search_text_cat" else None

    found = []
    for cat, ws in WS.items():
        if not ws: continue
        if forced_cat and cat != forced_cat: continue
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID")) != str(uid): continue
            hay = " ".join([str(r.get(k,"")) for k in TASKS_HEADERS]).lower()
            if q in hay:
                tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                                  r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
                found.append((build_task_line(r), tid))
    if not found:
        bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); clear_state(uid); return
    total_pages = (len(found)+PAGE_SIZE-1)//PAGE_SIZE
    kb = page_buttons(found[:PAGE_SIZE], 1, total_pages, prefix_action="open")
    bot.send_message(uid, "Найденные задачи:", reply_markup=kb)
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "done_text")
def done_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()
    supplier = guess_supplier(txt)
    # ищем задачи сегодня по всем листам
    changed = 0
    last_closed = None
    for cat, ws in WS.items():
        if not ws: continue
        rows = [r for r in row_to_dict_list(ws) if r.get("Дата")==today_str() and str(r.get("User ID"))==str(uid)]
        for r in rows:
            if (r.get("Статус","") or "").lower() == "выполнено":
                continue
            t = (r.get("Задача","") or "").lower()
            if supplier and supplier.lower() not in t:
                continue
            if not supplier and not any(w in t for w in ["к-экспро","вылегжан","заказ","сделал","заказать"]):
                continue
            tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                              r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            ok, _ = mark_done_by_id(tid, uid)
            if ok:
                changed += 1
                last_closed = r

    msg = f"✅ Отмечено выполненным: {changed}."
    if changed and last_closed and supplier:
        created = plan_next_by_supplier_rule(uid, supplier,
                                             last_closed.get("Категория","Кофейня"),
                                             last_closed.get("Подкатегория",""),
                                             last_closed.get("Задача",""))
        if created:
            msg += "\n🔮 Запланировано: " + ", ".join([f"приемка {d1.strftime('%d.%m')} → новый заказ {d2.strftime('%d.%m')}" for d1,d2 in created])
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

# =============== КАРТОЧКИ / CALLBACKS ===============
def render_task_card(uid, task_id):
    # перебираем все листы
    for cat, ws in WS.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(task_id, rows)
        if not idx:
            continue
        r = rows[idx-2]
        date_s = r.get("Дата","")
        header = (
            f"<b>{r.get('Задача','')}</b>\n"
            f"📅 {weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))} — {date_s}\n"
            f"📁 {r.get('Категория','—')} / {r.get('Подкатегория','—')}\n"
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
            tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                              r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            items.append((build_task_line(r), tid))
        total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
        page = max(1, min(page, total_pages))
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
        if kb:
            bot.send_message(uid, text, reply_markup=kb)
        else:
            bot.send_message(uid, text)
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
            created = plan_next_by_supplier_rule(uid, supplier, r.get("Категория","Кофейня"),
                                                 r.get("Подкатегория",""), r.get("Задача",""))
            if created:
                msg += " Запланирована приемка/следующий заказ."
        bot.answer_callback_query(c.id, msg, show_alert=True)
        text, kb = render_task_card(uid, task_id)
        if kb:
            bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
        else:
            bot.edit_message_text(text, uid, c.message.message_id)
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
        bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")
        return

    if a == "accept_delivery_date":
        task_id = data.get("id")
        when = data.get("d")
        # ищем задачу
        parent = None
        parent_ws = None
        for cat, ws in WS.items():
            rows = row_to_dict_list(ws)
            idx = find_row_index_by_id(task_id, rows)
            if idx:
                parent = rows[idx-2]
                parent_ws = ws
                break
        if not parent:
            bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
        date_s = today_str() if when == "today" else today_str(now_local()+timedelta(days=1))
        supplier = guess_supplier(parent.get("Задача","")) or "Поставка"
        add_task(date_s, parent.get("Категория",""), parent.get("Подкатегория",""),
                 f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(parent.get('Подкатегория','')) or '—'})",
                 "10:00", uid, status="", repeat="", source=f"subtask:{task_id}")
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

# =============== ПОДЗАДАЧИ / ДАТЫ / НАПОМИНАНИЯ ===============
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id")
    text = m.text.strip()
    # найдём родителя
    parent = None
    for cat, ws in WS.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(task_id, rows)
        if idx:
            parent = rows[idx-2]
            break
    if not parent:
        bot.send_message(uid, "Родительская задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    add_task(parent.get("Дата",""), parent.get("Категория",""), parent.get("Подкатегория",""),
             f"• {text}", parent.get("Дедлайн",""), uid, status="", repeat="", source=f"subtask:{task_id}")
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
        bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    parent = None
    for cat, ws in WS.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(task_id, rows)
        if idx:
            parent = rows[idx-2]
            break
    if not parent:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    supplier = guess_supplier(parent.get("Задача","")) or "Поставка"
    add_task(ds, parent.get("Категория",""), parent.get("Подкатегория",""),
             f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(parent.get('Подкатегория','')) or '—'})",
             "10:00", uid, status="", repeat="", source=f"subtask:{task_id}")
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
    # найдём задачу и пропишем remind:HH:MM в Источник
    for cat, ws in WS.items():
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(task_id, rows)
        if not idx: continue
        r = rows[idx-2]
        cur_source = r.get("Источник","") or ""
        new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
        ws.update_cell(idx, TASKS_HEADERS.index("Источник")+1, new_source)
        bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu())
        clear_state(uid)
        return
    bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu())
    clear_state(uid)

# =============== ЕЖЕДНЕВНЫЕ ДАЙДЖЕСТЫ И НАПОМИНАНИЯ ===============
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
        rows = get_all_task_rows()
        for r in rows:
            if r.get("Дата") != today: 
                continue
            src = (r.get("Источник") or "")
            if "remind:" not in src:
                continue
            matches = re.findall(r"remind:(\d{1,2}:\d{2})", src)
            for tm in matches:
                key = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                                  r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн","")) + "|" + today + "|" + tm
                if key in NOTIFIED:
                    continue
                try:
                    hh, mm = map(int, tm.split(":"))
                    nowt = now_local().time()
                    if (nowt.hour, nowt.minute) >= (hh, mm):
                        bot.send_message(r.get("User ID"),
                                         f"⏰ Напоминание: {r.get('Категория','')}/{r.get('Подкатегория','')} — {r.get('Задача','')} (до {r.get('Дедлайн') or '—'})")
                        NOTIFIED.add(key)
                except Exception:
                    pass
    except Exception as e:
        log.error("job_reminders error %s", e)

def scheduler_thread():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# =============== WEBHOOK / FLASK ===============
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

# =============== START ===============
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
