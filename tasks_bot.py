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

# ========= ОКРУЖЕНИЕ =========
API_TOKEN        = os.getenv("TELEGRAM_TOKEN")
TABLE_URL        = os.getenv("GOOGLE_SHEETS_URL")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")  # https://<your-app>.onrender.com
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
TZ_NAME          = os.getenv("TZ", "Europe/Moscow")
WEBHOOK_URL      = f"{WEBHOOK_BASE}/{API_TOKEN}" if API_TOKEN and WEBHOOK_BASE else None

LOCAL_TZ = pytz.timezone(TZ_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

REQUIRED_ENVS = ["TELEGRAM_TOKEN", "GOOGLE_SHEETS_URL", "WEBHOOK_BASE"]
missing = [v for v in REQUIRED_ENVS if not os.getenv(v)]
if missing:
    log.warning("Не заданы переменные окружения: %s", ", ".join(missing))

# ========= ИНИТ БОТА / SHEETS =========
bot = TeleBot(API_TOKEN, parse_mode="HTML")

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)
    # Обязательные листы
    WS_NAMES = {
        "Кофейня": None,
        "Табачка": None,
        "WB": None,
        "Личное": None,
        "Поставщики": None,
        "Пользователи": None,
        "Логи": None,
    }
    for name in WS_NAMES.keys():
        WS_NAMES[name] = sh.worksheet(name)
    ws_suppliers = WS_NAMES["Поставщики"]
    ws_users     = WS_NAMES["Пользователи"]
    ws_logs      = WS_NAMES["Логи"]
    log.info("Google Sheets подключены.")
except Exception:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

# Единый заголовок для рабочих листов с задачами
TASKS_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]

# ========= УТИЛИТЫ ВРЕМЕНИ =========
def now_local():
    # Всегда ориентируемся на локальный TZ, как ты просил
    return datetime.now(LOCAL_TZ)

def today_str(dt=None):
    if dt is None: dt = now_local()
    return dt.strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

# ========= ПРОЧЕЕ =========
PAGE_SIZE = 7  # пагинация списков

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception:
        pass

def row_to_dict_list(ws):
    return ws.get_all_records()

def ensure_headers(ws):
    # мягкая проверка — если заголовки пустые, устанавливаем
    try:
        head = ws.row_values(1)
        if not head or len(head) < len(TASKS_HEADERS):
            ws.update("A1:I1", [TASKS_HEADERS])
    except Exception:
        pass

for nm, ws in WS_NAMES.items():
    if nm in ("Кофейня","Табачка","WB","Личное"):
        ensure_headers(ws)

def sha_task_id(user_id, date_s, cat, subcat, text, deadline):
    key = f"{user_id}|{date_s}|{cat}|{subcat}|{text}|{deadline}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def find_row_index_by_id(ws, task_id, rows):
    for i, r in enumerate(rows, start=2):  # данные с 2-й строки
        rid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        if rid == task_id:
            return i
    return None

def add_task_to_ws(ws, date_s, category, subcategory, text, deadline, user_id, status="", repeat="", source=""):
    ws.append_row([date_s, category, subcategory, text, deadline, str(user_id), status, repeat, source], value_input_option="USER_ENTERED")

def mark_done_by_id(ws, task_id, user_id):
    rows = row_to_dict_list(ws)
    idx = find_row_index_by_id(ws, task_id, rows)
    if not idx:
        return False, None
    r = rows[idx-2]
    if str(r.get("User ID")) != str(user_id):
        return False, None
    ws.update_cell(idx, TASKS_HEADERS.index("Статус")+1, "выполнено")
    return True, r

def is_order_task(text: str) -> bool:
    t = (text or "").lower()
    return any(x in t for x in ["заказ", "заказать"])

def guess_supplier(text: str) -> str:
    t = (text or "").lower()
    if any(x in t for x in ["к-экспро","к экспро","k-exp","k exp"]): return "К-Экспро"
    if "вылегжан" in t: return "ИП Вылегжанина"
    if "лобанов" in t: return "Лобанов"
    if "аванта" in t: return "Авантаж"
    return ""

def normalize_tt_from_subcat(subcat: str) -> str:
    s = (subcat or "").strip().lower()
    if "центр" in s: return "Центр"
    if "полет" in s or "полёт" in s: return "Полет"
    if "климов" in s: return "Климово"
    return subcat or ""

# ========= ПРАВИЛА ПОСТАВЩИКОВ =========
SUPPLIER_RULES = {
    "к-экспро": {
        "kind": "cycle_every_n_days",
        "order_every_days": 2,
        "delivery_offset_days": 1,
        "order_deadline": "14:00",
        "emoji": "📦",
    },
    "ип вылегжанина": {
        "kind": "delivery_shelf_then_order",
        "delivery_offset_days": 1,
        "shelf_hours": 72,
        "order_deadline": "14:00",
        "emoji": "🥘",
    },
    "лобанов": {
        "kind": "cycle_every_n_days",
        "order_every_days": 2,
        "delivery_offset_days": 1,
        "order_deadline": "12:00",
        "emoji": "🧋",
    },
    "авантаж": {
        "kind": "cycle_every_n_days",
        "order_every_days": 3,
        "delivery_offset_days": 1,
        "order_deadline": "12:00",
        "emoji": "🥐",
    },
}

def load_supplier_rules_from_sheet():
    try:
        rows = row_to_dict_list(ws_suppliers)
        for r in rows:
            name = (r.get("Поставщик") or r.get("Название") or "").strip().lower()
            if not name: continue
            kind_raw = (r.get("Правило") or r.get("Rule") or "").strip().lower()
            if not kind_raw: continue
            d_off = int(str(r.get("DeliveryOffsetDays") or r.get("СмещениеПоставкиДней") or 1))
            deadline = (r.get("ДедлайнЗаказа") or "14:00").strip()
            emoji = (r.get("Emoji") or "📦").strip()

            if "каждые" in kind_raw:
                n = int(re.findall(r"\d+", kind_raw)[0]) if re.findall(r"\d+", kind_raw) else 2
                SUPPLIER_RULES[name] = {
                    "kind": "cycle_every_n_days",
                    "order_every_days": n,
                    "delivery_offset_days": d_off,
                    "order_deadline": deadline,
                    "emoji": emoji
                }
            elif "shelf" in kind_raw or "хран" in kind_raw or "72" in kind_raw:
                shelf = int(re.findall(r"\d+", kind_raw)[0]) if re.findall(r"\d+", kind_raw) else 72
                SUPPLIER_RULES[name] = {
                    "kind": "delivery_shelf_then_order",
                    "delivery_offset_days": d_off,
                    "shelf_hours": shelf,
                    "order_deadline": deadline,
                    "emoji": emoji
                }
    except Exception as e:
        log.warning("Не смог загрузить правила поставщиков: %s", e)

load_supplier_rules_from_sheet()

def plan_next_by_supplier_rule(user_id, supplier_name, category, subcategory, base_task_text):
    key = (supplier_name or "").strip().lower()
    rule = SUPPLIER_RULES.get(key)
    if not rule:
        return []
    created = []
    today = now_local().date()

    # куда пишем (категория → лист)
    ws = WS_NAMES.get(category) or WS_NAMES["Кофейня"]

    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = today + timedelta(days=rule["order_every_days"])
        add_task_to_ws(ws, delivery_day.strftime("%d.%m.%Y"), category, subcategory,
                       f"{rule['emoji']} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                       "10:00", user_id, status="", repeat="", source=f"auto:delivery:{supplier_name}")
        add_task_to_ws(ws, next_order_day.strftime("%d.%m.%Y"), category, subcategory,
                       f"{rule['emoji']} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                       rule["order_deadline"], user_id, status="", repeat="", source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))

    elif rule["kind"] == "delivery_shelf_then_order":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = delivery_day + timedelta(days=2)  # ~72ч
        add_task_to_ws(ws, delivery_day.strftime("%d.%m.%Y"), category, subcategory,
                       f"{rule['emoji']} Принять поставку {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                       "11:00", user_id, status="", repeat="", source=f"auto:delivery:{supplier_name}")
        add_task_to_ws(ws, next_order_day.strftime("%d.%m.%Y"), category, subcategory,
                       f"{rule['emoji']} Заказать {supplier_name} ({normalize_tt_from_subcat(subcategory) or '—'})",
                       rule["order_deadline"], user_id, status="", repeat="", source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))
    return created

# ========= ЧТЕНИЕ ЗАДАЧ (по всем листам) =========
CATEGORY_SHEETS = ["Кофейня","Табачка","WB","Личное"]

def get_all_user_tasks_for_date(user_id, date_s):
    tasks = []
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID")) == str(user_id) and (r.get("Дата") == date_s):
                r["_ws_name"] = name
                tasks.append(r)
    return tasks

def get_all_user_tasks_between(user_id, start_dt, days=7):
    date_set = {(start_dt + timedelta(days=i)).strftime("%d.%м.%Y") for i in range(days)}
    tasks = []
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID")) == str(user_id) and (r.get("Дата") in date_set):
                r["_ws_name"] = name
                tasks.append(r)
    return tasks

# ========= ФОРМАТИРОВАНИЕ =========
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

# ========= КЛАВИАТУРЫ =========
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

# ========= INLINE-PAYLOAD =========
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
    if nav:
        kb.row(*nav)
    return kb

# ========= GPT (NLP) =========
def ai_parse_to_tasks(text, fallback_user_id):
    items = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов. "
                "Схема: {date:'ДД.ММ.ГГГГ'|'', time:'ЧЧ:ММ'|'', category, subcategory, task, repeat:'', supplier:''}. "
                "Если в тексте «сегодня/завтра/послезавтра», приведи к дате. "
                "Если встречаются «К-Экспро/Вылегжанина/Лобанов/Авантаж» — это заказ; category=Кофейня по умолчанию; "
                "subcategory ('Центр','Полет','Климово') — если встречаются в тексте."
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
                    "category": it.get("category") or "Личное",
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
        cat = "Кофейня" if any(x in tl for x in ["кофейн","к-экспро","вылегжан","лобанов","аванта"]) else ("Табачка" if "табач" in tl else "Личное")
        sub = "Центр" if "центр" in tl else ("Полет" if ("полет" in tl or "полёт" in tl) else ("Климово" if "климов" in tl else ""))
        dl = re.search(r"(\d{1,2}:\d{2})", text)
        deadline = dl.group(1) if dl else ""
        d = ""
        if "сегодня" in tl: d = today_str()
        elif "завтра" in tl: d = today_str(now_local()+timedelta(days=1))
        elif "послезавтра" in tl: d = today_str(now_local()+timedelta(days=2))
        else:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text); d = m.group(1) if m else ""
        items = [{
            "date": d, "category": cat, "subcategory": sub,
            "task": text.strip(), "deadline": deadline,
            "user_id": fallback_user_id, "repeat":"", "source":""
        }]
    return items

def ai_assist_answer(query, user_id):
    try:
        rows = get_all_user_tasks_between(user_id, now_local().date(), 7)
        brief = []
        for r in sorted(rows, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
            brief.append(f"{r['Дата']} • {r['Категория']}/{r['Подкатегория'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
        context = "\n".join(brief)[:4000]
        if not OPENAI_API_KEY:
            return "Совет: начни с задач с ближайшим дедлайном. Разбей крупные задачи на 2–3 подзадачи и расставь напоминания."
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = "Ты продвинутый ассистент задач. Коротко, по делу, на русском, буллетами. Можешь предлагать перераспределение по дням."
        prompt = f"Запрос: {query}\n\nМои задачи (7 дней):\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("AI assistant error: %s", e)
        return "Не удалось сформировать ответ ассистента."

# ========= СОСТОЯНИЯ =========
USER_STATE = {}     # uid -> state
USER_DATA  = {}     # uid -> payload dict

def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None: USER_DATA[uid] = data

def get_state(uid): return USER_STATE.get(uid)
def get_data(uid):  return USER_DATA.get(uid, {})
def clear_state(uid):
    USER_STATE.pop(uid, None)
    USER_DATA.pop(uid, None)

# ========= МЕНЮ И ХЕНДЛЕРЫ =========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я готов. Что делаем?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    date_s = today_str()
    rows = get_all_user_tasks_for_date(uid, date_s)
    if not rows:
        bot.send_message(uid, f"📅 Задачи на {date_s}\n\nЗадач нет.", reply_markup=main_menu()); return
    # Пагинация/карточки
    items = []
    for r in rows:
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        items.append((build_task_line(r), tid))
    page = 1
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    header = f"📅 Задачи на {date_s}\n\n" + format_grouped(rows, header_date=date_s)
    bot.send_message(uid, header, reply_markup=main_menu())
    bot.send_message(uid, "Открой карточку задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week(m):
    uid = m.chat.id
    tasks = get_all_user_tasks_between(uid, now_local().date(), 7)
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

# === Добавление задач «по кнопкам»: ты вводишь только НАЗВАНИЕ ===
@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    uid = m.chat.id
    set_state(uid, "add_title")
    bot.send_message(uid, "Введи название задачи (только текст).")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_title")
def add_title(m):
    uid = m.chat.id
    USER_DATA[uid] = {"title": m.text.strip()}
    # 1) Выбор категории
    kb = types.InlineKeyboardMarkup()
    for name in CATEGORY_SHEETS:
        kb.add(types.InlineKeyboardButton(name, callback_data=mk_cb("pick_cat", cat=name)))
    bot.send_message(uid, "Выбери категорию:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: parse_cb(c.data) and parse_cb(c.data).get("a")=="pick_cat")
def cb_pick_cat(c):
    uid = c.message.chat.id
    data = parse_cb(c.data)
    cat = data.get("cat")
    d = get_data(uid); d["category"] = cat; set_state(uid, "add_title", d)
    # 2) Выбор подкатегории (типовые)
    subs = ["Центр","Полет","Климово","—"]
    kb = types.InlineKeyboardMarkup()
    for s in subs:
        kb.add(types.InlineKeyboardButton(s, callback_data=mk_cb("pick_sub", sub=s if s!="—" else "")))
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "Выбери подкатегорию:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: parse_cb(c.data) and parse_cb(c.data).get("a")=="pick_sub")
def cb_pick_sub(c):
    uid = c.message.chat.id
    data = parse_cb(c.data)
    sub = data.get("sub","")
    d = get_data(uid); d["subcategory"] = sub; set_state(uid, "add_title", d)
    # 3) Выбор даты: Сегодня/Завтра/Другая
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Сегодня", callback_data=mk_cb("pick_date", d="today")),
        types.InlineKeyboardButton("Завтра", callback_data=mk_cb("pick_date", d="tomorrow")),
    )
    kb.add(types.InlineKeyboardButton("📅 Другая дата", callback_data=mk_cb("pick_date_other")))
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "Выбери дату:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: parse_cb(c.data) and parse_cb(c.data).get("a")=="pick_date_other")
def cb_pick_date_other(c):
    uid = c.message.chat.id
    set_state(uid, "add_pick_date")
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_pick_date")
def add_pick_date(m):
    uid = m.chat.id
    ds = m.text.strip()
    try:
        datetime.strptime(ds, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    d = get_data(uid); d["date"] = ds; set_state(uid, "add_title", d)
    # 4) Время (опционально)
    bot.send_message(uid, "Введи время дедлайна (ЧЧ:ММ) или '-' чтобы пропустить.")
    set_state(uid, "add_pick_time")

@bot.callback_query_handler(func=lambda c: parse_cb(c.data) and parse_cb(c.data).get("a")=="pick_date")
def cb_pick_date(c):
    uid = c.message.chat.id
    data = parse_cb(c.data)
    when = data.get("d")
    ds = today_str() if when=="today" else today_str(now_local()+timedelta(days=1))
    d = get_data(uid); d["date"] = ds; set_state(uid, "add_title", d)
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "Введи время дедлайна (ЧЧ:ММ) или '-' чтобы пропустить.")
    set_state(uid, "add_pick_time")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_pick_time")
def add_pick_time(m):
    uid = m.chat.id
    t = m.text.strip()
    if t != "-" and not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ или '-' чтобы пропустить.", reply_markup=main_menu()); clear_state(uid); return
    d = get_data(uid)
    d["deadline"] = "" if t == "-" else t
    # Сохранение в лист (категория = лист)
    cat = d["category"]; ws = WS_NAMES.get(cat)
    title = d["title"]; sub = d.get("subcategory","")
    date_s = d["date"]; deadline = d.get("deadline","")
    add_task_to_ws(ws, date_s, cat, sub, title, deadline, uid, status="", repeat="", source="btn:add")
    clear_state(uid)
    bot.send_message(uid, f"✅ Задача добавлена: {cat}/{sub or '—'} — {title} на {date_s} (до {deadline or '—'})", reply_markup=main_menu())

# === Поиск / Выполнить ===
@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    set_state(uid, "search_text")
    bot.send_message(uid, "Что ищем? Введи часть названия/категории/подкатегории/даты.")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "search_text")
def search_text(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    found = []
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID")) != str(uid): continue
            hay = " ".join([str(r.get(k,"")) for k in TASKS_HEADERS]).lower()
            if q in hay:
                tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
                r["_ws_name"] = name
                found.append((build_task_line(r), tid, name))
    if not found:
        bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); clear_state(uid); return
    items = [(t, tid) for (t, tid, _) in found]
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    page = 1
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    bot.send_message(uid, "Найденные задачи:", reply_markup=kb)
    clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_menu(m):
    uid = m.chat.id
    set_state(uid, "done_text")
    bot.send_message(uid, "Напиши, что выполнено (например: «закрыл заказ К-Экспро Центр»).")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "done_text")
def done_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()
    supplier = guess_supplier(txt)
    date_s = today_str()
    changed = 0
    last_closed = None
    last_ws = None
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        for r in rows:
            if r.get("Дата") != date_s: continue
            if r.get("Статус","").lower() == "выполнено": continue
            if str(r.get("User ID")) != str(uid): continue
            t = (r.get("Задача","") or "").lower()
            if supplier and supplier.lower() not in t and "заказ" not in t:
                continue
            if not supplier and not any(w in t for w in ["заказ","сделал","закрыл"]):
                continue
            tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            ok, _ = mark_done_by_id(ws, tid, uid)
            if ok:
                changed += 1
                last_closed = r
                last_ws = ws
    msg = f"✅ Отмечено выполненным: {changed}."
    if changed and last_closed and supplier:
        created = plan_next_by_supplier_rule(uid, supplier, last_closed.get("Категория","Кофейня"), last_closed.get("Подкатегория",""), last_closed.get("Задача",""))
        if created:
            msg += "\n🔮 Запланировано: " + ", ".join([f"приемка {d1.strftime('%d.%m')} → заказ {d2.strftime('%d.%m')}" for d1,d2 in created])
    bot.send_message(uid, msg, reply_markup=main_menu())
    clear_state(uid)

# === Поставки ===
@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставка")
def handle_supply(m):
    bot.send_message(m.chat.id, "Меню поставок:", reply_markup=supply_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    uid = m.chat.id
    set_state(uid, "add_supplier")
    bot.send_message(uid, "Формат: <b>Название; Правило; Дедлайн(optional)</b>\nПримеры:\nК-Экспро; каждые 2 дня; 14:00\nИП Вылегжанина; shelf 72h; 14:00")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_supplier")
def add_supplier_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    try:
        parts = [p.strip() for p in txt.split(";")]
        name = parts[0]
        rule = parts[1] if len(parts) > 1 else ""
        deadline = parts[2] if len(parts) > 2 else "14:00"
        WS_NAMES["Поставщики"].append_row([name, rule, deadline], value_input_option="USER_ENTERED")
        load_supplier_rules_from_sheet()
        bot.send_message(uid, f"✅ Поставщик «{name}» добавлен.", reply_markup=supply_menu())
    except Exception as e:
        log.error("add_supplier error: %s", e)
        bot.send_message(uid, "Не получилось добавить поставщика.", reply_markup=supply_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def handle_today_orders(m):
    uid = m.chat.id
    date_s = today_str()
    orders = []
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        for r in rows:
            if str(r.get("User ID")) == str(uid) and r.get("Дата") == date_s and is_order_task(r.get("Задача","")):
                r["_ws_name"] = name
                orders.append(r)
    if not orders:
        bot.send_message(uid, "Сегодня заказов нет.", reply_markup=supply_menu()); return
    kb = types.InlineKeyboardMarkup()
    for i,r in enumerate(orders, start=1):
        tid = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
        kb.add(types.InlineKeyboardButton(build_task_line(r, i), callback_data=mk_cb("open", id=tid)))
    bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)

# === Ассистент ===
@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    uid = m.chat.id
    set_state(uid, "assistant_text")
    bot.send_message(uid, "Что нужно? (спланировать день, выделить приоритеты, составить расписание и т.д.)")

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "assistant_text")
def assistant_text(m):
    uid = m.chat.id
    answer = ai_assist_answer(m.text.strip(), uid)
    bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# ========= КАРТОЧКА / INLINE =========
def render_task_card(uid, task_id):
    # Нужно найти задачу и лист
    for name in CATEGORY_SHEETS:
        ws = WS_NAMES[name]
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if idx:
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
            kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=task_id, ws=name)))
            if is_order_task(r.get("Задача","")):
                kb.add(types.InlineKeyboardButton("🚚 Принять поставку", callback_data=mk_cb("accept_delivery", id=task_id, ws=name)))
            kb.add(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("add_sub", id=task_id, ws=name)))
            kb.add(types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("remind_set", id=task_id, ws=name)))
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
        date_s = today_str()
        rows = get_all_user_tasks_for_date(uid, date_s)
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
        try: bot.delete_message(uid, c.message.message_id)
        except Exception: pass
        return

    if a == "done":
        task_id = data.get("id")
        ws_name = data.get("ws")
        ws = WS_NAMES.get(ws_name)
        ok, r = mark_done_by_id(ws, task_id, uid)
        if not ok:
            bot.answer_callback_query(c.id, "Не смог отметить", show_alert=True); return
        supplier = guess_supplier(r.get("Задача",""))
        msg = "✅ Готово."
        if supplier:
            created = plan_next_by_supplier_rule(uid, supplier, r.get("Категория","Кофейня"), r.get("Подкатегория",""), r.get("Задача",""))
            if created:
                msg += " Запланирована приемка/следующий заказ."
        bot.answer_callback_query(c.id, msg, show_alert=True)
        text, kb = render_task_card(uid, task_id)
        if kb: bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
        else:  bot.edit_message_text(text, uid, c.message.message_id)
        return

    if a == "accept_delivery":
        task_id = data.get("id"); ws_name = data.get("ws")
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("Сегодня", callback_data=mk_cb("accept_delivery_date", id=task_id, ws=ws_name, d="today")),
            types.InlineKeyboardButton("Завтра",  callback_data=mk_cb("accept_delivery_date", id=task_id, ws=ws_name, d="tomorrow")),
        )
        kb.add(types.InlineKeyboardButton("📅 Другая дата", callback_data=mk_cb("accept_delivery_pick", id=task_id, ws=ws_name)))
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда принять поставку?", reply_markup=kb)
        return

    if a == "accept_delivery_pick":
        task_id = data.get("id"); ws_name = data.get("ws")
        set_state(uid, "pick_delivery_date", {"task_id": task_id, "ws": ws_name})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")
        return

    if a == "accept_delivery_date":
        task_id = data.get("id"); ws_name = data.get("ws")
        when = data.get("d")
        ws = WS_NAMES.get(ws_name)
        rows = row_to_dict_list(ws)
        idx = find_row_index_by_id(ws, task_id, rows)
        if not idx:
            bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
        r = rows[idx-2]
        date_s = today_str() if when=="today" else today_str(now_local()+timedelta(days=1))
        supplier = guess_supplier(r.get("Задача","")) or "Поставка"
        add_task_to_ws(ws, date_s, r.get("Категория",""), r.get("Подкатегория",""),
                       f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                       "10:00", uid, status="", repeat="", source=f"subtask:{task_id}")
        bot.answer_callback_query(c.id, f"Создана задача на {date_s}", show_alert=True)
        return

    if a == "add_sub":
        task_id = data.get("id"); ws_name = data.get("ws")
        set_state(uid, "add_subtask_text", {"task_id": task_id, "ws": ws_name})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи текст подзадачи:")
        return

    if a == "remind_set":
        task_id = data.get("id"); ws_name = data.get("ws")
        set_state(uid, "set_reminder", {"task_id": task_id, "ws": ws_name})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда напомнить? Формат ЧЧ:ММ (локальное время).")
        return

# === Текстовые состояния карточки ===
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id"); ws_name = data.get("ws")
    ws = WS_NAMES.get(ws_name)
    text = m.text.strip()
    rows = row_to_dict_list(ws)
    idx = find_row_index_by_id(ws, task_id, rows)
    if not idx:
        bot.send_message(uid, "Родительская задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    parent = rows[idx-2]
    add_task_to_ws(ws, parent.get("Дата",""), parent.get("Категория",""), parent.get("Подкатегория",""),
                   f"• {text}", parent.get("Дедлайн",""), uid, status="", repeat="", source=f"subtask:{task_id}")
    bot.send_message(uid, "Подзадача добавлена.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "pick_delivery_date")
def pick_delivery_date(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id"); ws_name = data.get("ws")
    ws = WS_NAMES.get(ws_name)
    ds = m.text.strip()
    try:
        datetime.strptime(ds, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    rows = row_to_dict_list(ws)
    idx = find_row_index_by_id(ws, task_id, rows)
    if not idx:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    r = rows[idx-2]
    supplier = guess_supplier(r.get("Задача","")) or "Поставка"
    add_task_to_ws(ws, ds, r.get("Категория",""), r.get("Подкатегория",""),
                   f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                   "10:00", uid, status="", repeat="", source=f"subtask:{task_id}")
    bot.send_message(uid, f"Создана задача на {ds}.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder(m):
    uid = m.chat.id
    data = get_data(uid)
    task_id = data.get("task_id"); ws_name = data.get("ws")
    ws = WS_NAMES.get(ws_name)
    t = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
    rows = row_to_dict_list(ws)
    idx = find_row_index_by_id(ws, task_id, rows)
    if not idx:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    cur_source = rows[idx-2].get("Источник","") or ""
    new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
    ws.update_cell(idx, TASKS_HEADERS.index("Источник")+1, new_source)
    bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu())
    clear_state(uid)

# ========= ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ И НАПОМИНАНИЯ =========
NOTIFIED = set()  # taskId|date|time

def job_daily_digest():
    try:
        users = row_to_dict_list(ws_users)
        today = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            tasks = get_all_user_tasks_for_date(uid, today)
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
        for name in CATEGORY_SHEETS:
            ws = WS_NAMES[name]
            rows = row_to_dict_list(ws)
            for r in rows:
                if r.get("Дата") != today: continue
                src = (r.get("Источник") or "")
                if "remind:" not in src: continue
                matches = re.findall(r"remind:(\d{1,2}:\d{2})", src)
                for tm in matches:
                    key = sha_task_id(str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн","")) + "|" + today + "|" + tm
                    if key in NOTIFIED: continue
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

def scheduler_thread():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)   # твой утренний дайджест
    schedule.every(1).minutes.do(job_reminders)             # напоминания каждую минуту
    while True:
        schedule.run_pending()
        time.sleep(1)

# ========= FLASK / WEBHOOK =========
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

    threading.Thread(target=scheduler_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")))
