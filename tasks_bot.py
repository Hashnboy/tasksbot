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
WEBHOOK_BASE     = os.getenv("WEBHOOK_BASE")  # https://<app>.onrender.com
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
TZ_NAME          = os.getenv("TZ", "Europe/Moscow")
WEBHOOK_URL      = f"{WEBHOOK_BASE}/{API_TOKEN}" if API_TOKEN and WEBHOOK_BASE else None

LOCAL_TZ = pytz.timezone(TZ_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

REQUIRED_ENVS = ["TELEGRAM_TOKEN", "GOOGLE_SHEETS_URL"]
missing = [v for v in REQUIRED_ENVS if not os.getenv(v)]
if missing:
    log.warning("Не заданы переменные окружения: %s", ", ".join(missing))

# ===================== ИНИЦИАЛИЗАЦИЯ БОТА / SHEETS =====================
bot = TeleBot(API_TOKEN, parse_mode="HTML")

SHEET_TASK_HEADERS = [
    "Дата","Категория","Подкатегория","Задача","Дедлайн",
    "User ID","Статус","Повторяемость","Источник"
]

# маппинг категорий → листов
CATEGORY_TO_SHEET = {
    "Кофейня": "Кофейня",
    "Табачка": "Табачка",
    "Личное": "Личное",
    "WB": "WB",
}
TASK_SHEETS = list(CATEGORY_TO_SHEET.values())  # листы с задачами
SYSTEM_SHEETS = ["Поставщики", "Пользователи", "Логи"]

def ensure_headers(ws, headers):
    first_row = ws.row_values(1)
    if [h.strip() for h in first_row] != headers:
        ws.resize(rows=max(1, ws.row_count))
        ws.update("A1", [headers])

def open_or_create_worksheet(sh, title, headers=None):
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=12)
    if headers:
        ensure_headers(ws, headers)
    return ws

try:
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)

    # откроем/создадим все листы задач
    ws_by_title = {}
    for t in TASK_SHEETS:
        ws_by_title[t] = open_or_create_worksheet(sh, t, SHEET_TASK_HEADERS)

    # системные листы
    ws_suppliers = open_or_create_worksheet(sh, "Поставщики", ["Поставщик","Правило","ДедлайнЗаказа","Emoji","DeliveryOffsetDays"])
    ws_users     = open_or_create_worksheet(sh, "Пользователи", ["Имя","Telegram ID"])
    ws_logs      = open_or_create_worksheet(sh, "Логи", ["ts","user_id","action","payload"])

    log.info("Google Sheets подключены.")
except Exception:
    log.error("Ошибка подключения к Google Sheets", exc_info=True)
    raise

# ===================== УТИЛИТЫ =====================
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
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception:
        pass

def row_to_dict_list(ws):
    return ws.get_all_records()

def sha_task_id(sheet_title, user_id, date_s, cat, subcat, text, deadline):
    key = f"{sheet_title}|{user_id}|{date_s}|{cat}|{subcat}|{text}|{deadline}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def find_row_index_by_id(task_id):
    # ищем по всем листам задач
    for sheet_title, ws in ws_by_title.items():
        rows = row_to_dict_list(ws)
        for i, r in enumerate(rows, start=2):
            rid = sha_task_id(sheet_title, str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                              r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
            if rid == task_id:
                return sheet_title, ws, i, r
    return None, None, None, None

def infer_category_from_text(text: str) -> str:
    t = (text or "").lower()
    if "табач" in t or "сигар" in t: return "Табачка"
    if "кофе" in t or "к-экспро" in t or "вылегжан" in t: return "Кофейня"
    if "wb" in t or "wildberries" in t: return "WB"
    return "Личное"

def normalize_subcat_hint(subcat: str) -> str:
    s = (subcat or "").strip().lower()
    if "центр" in s: return "Центр"
    if "полет" in s or "полёт" in s: return "Полет"
    if "клим" in s: return "Климово"
    return subcat or ""

def add_task(date_s, category, subcategory, text, deadline, user_id, status="", repeat="", source=""):
    cat = category or infer_category_from_text(text)
    sheet_title = CATEGORY_TO_SHEET.get(cat, "Личное")
    ws = ws_by_title[sheet_title]
    row = [date_s, cat, subcategory, text, deadline, str(user_id), status, repeat, source]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return sheet_title

def mark_done_by_id(task_id, user_id):
    sheet_title, ws, idx, r = find_row_index_by_id(task_id)
    if not idx: return False, None
    if str(r.get("User ID")) != str(user_id): return False, None
    ws.update_cell(idx, SHEET_TASK_HEADERS.index("Статус")+1, "выполнено")
    return True, {"sheet": sheet_title, **r}

def read_all_tasks_for_user(user_id):
    res = []
    for sheet_title, ws in ws_by_title.items():
        for r in row_to_dict_list(ws):
            if str(r.get("User ID")) == str(user_id):
                rid = sha_task_id(sheet_title, str(r.get("User ID")), r.get("Дата",""), r.get("Категория",""),
                                  r.get("Подкатегория",""), r.get("Задача",""), r.get("Дедлайн",""))
                r["_sheet"] = sheet_title
                r["_id"] = rid
                res.append(r)
    return res

def tasks_on_date(user_id, date_s):
    return [r for r in read_all_tasks_for_user(user_id) if r.get("Дата")==date_s]

def tasks_between(user_id, start_dt, days=7):
    dates = {(start_dt + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days)}
    return [r for r in read_all_tasks_for_user(user_id) if r.get("Дата") in dates]

def is_order_task(text: str) -> bool:
    t = (text or "").lower()
    return "заказ" in t or "заказать" in t

def guess_supplier(text: str) -> str:
    t = (text or "").lower()
    if "к-экспро" in t or "к экспро" in t or "k-exp" in t: return "К-Экспро"
    if "вылегжан" in t: return "ИП Вылегжанина"
    return ""

def ensure_user_in_sheet(uid: int, name: str):
    try:
        rows = row_to_dict_list(ws_users)
        for r in rows:
            if str(r.get("Telegram ID")) == str(uid):
                return
        ws_users.append_row([name or "—", str(uid)], value_input_option="USER_ENTERED")
    except Exception as e:
        log.warning("Не смог записать пользователя: %s", e)

def list_registered_users():
    try:
        return [int(str(r.get("Telegram ID")).strip()) for r in row_to_dict_list(ws_users) if str(r.get("Telegram ID")).strip().isdigit()]
    except Exception:
        return []

# ===================== ПРАВИЛА ПОСТАВЩИКОВ =====================
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
}

def load_supplier_rules_from_sheet():
    try:
        rows = row_to_dict_list(ws_suppliers)
        for r in rows:
            name = (r.get("Поставщик") or "").strip().lower()
            if not name: continue
            kind = (r.get("Правило") or "").strip().lower()
            emoji = (r.get("Emoji") or "📦").strip()
            d_off = int(str(r.get("DeliveryOffsetDays") or 1))
            deadline = (r.get("ДедлайнЗаказа") or "14:00").strip()
            if "каждые" in kind:
                n = int(re.findall(r"\d+", kind)[0]) if re.findall(r"\d+", kind) else 2
                SUPPLIER_RULES[name] = {
                    "kind":"cycle_every_n_days","order_every_days":n,
                    "delivery_offset_days": d_off, "order_deadline":deadline,"emoji":emoji
                }
            elif "shelf" in kind or "72" in kind or "час" in kind:
                shelf = int(re.findall(r"\d+", kind)[0]) if re.findall(r"\d+", kind) else 72
                SUPPLIER_RULES[name] = {
                    "kind":"delivery_shelf_then_order","delivery_offset_days":d_off,
                    "shelf_hours":shelf,"order_deadline":deadline,"emoji":emoji
                }
    except Exception as e:
        log.warning("Не смог загрузить правила поставщиков: %s", e)

load_supplier_rules_from_sheet()

def plan_next_by_supplier_rule(user_id, supplier_name, category, subcategory, base_task_text):
    key = (supplier_name or "").strip().lower()
    rule = SUPPLIER_RULES.get(key)
    if not rule: return []
    created = []
    today = now_local().date()
    cat = category or "Кофейня"
    sub = normalize_subcat_hint(subcategory)

    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = today + timedelta(days=rule["order_every_days"])
        add_task(delivery_day.strftime("%d.%m.%Y"), cat, sub,
                 f"{rule['emoji']} Принять поставку {supplier_name} ({sub or '—'})",
                 "10:00", user_id, source=f"auto:delivery:{supplier_name}")
        add_task(next_order_day.strftime("%d.%m.%Y"), cat, sub,
                 f"{rule['emoji']} Заказать {supplier_name} ({sub or '—'})",
                 rule["order_deadline"], user_id, source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))

    elif rule["kind"] == "delivery_shelf_then_order":
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = delivery_day + timedelta(days=2)  # ~72ч
        add_task(delivery_day.strftime("%d.%m.%Y"), cat, sub,
                 f"{rule['emoji']} Принять поставку {supplier_name} ({sub or '—'})",
                 "11:00", user_id, source=f"auto:delivery:{supplier_name}")
        add_task(next_order_day.strftime("%d.%m.%Y"), cat, sub,
                 f"{rule['emoji']} Заказать {supplier_name} ({sub or '—'})",
                 rule["order_deadline"], user_id, source=f"auto:order:{supplier_name}")
        created.append((delivery_day, next_order_day))
    return created

# ===================== ФОРМАТИРОВАНИЕ =====================
def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
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
    pref = f"{i}. " if i is not None else ""
    return f"{pref}{r.get('Категория','—')}/{r.get('Подкатегория','—')}: {r.get('Задача','')[:40]}… (до {dl})"

# ===================== INLINE-ПОЛЕЗНОЕ =====================
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
    kb.row("🚚 Поставка","🧠 AI ассистент","🤖 AI команды","⚙️ Настройки")
    return kb

def supply_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🆕 Добавить поставщика","📦 Заказы сегодня")
    kb.row("⬅ Назад")
    return kb

# ===================== GPT: ПАРСИНГ И КОМАНДЫ =====================
def ai_parse_to_tasks(text, fallback_user_id):
    """
    Возвращает список dict {date, category, subcategory, task, deadline, user_id, repeat, source}
    """
    items = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов.\n"
                "Объект: {date:'ДД.ММ.ГГГГ'|'', time:'ЧЧ:ММ'|'', category:'Кофейня'|'Табачка'|'Личное'|'WB'|'', "
                "subcategory:'', task:'', repeat:'', supplier:''}."
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
                    "category": it.get("category") or infer_category_from_text(text),
                    "subcategory": normalize_subcat_hint(it.get("subcategory") or ""),
                    "task": it.get("task") or text.strip(),
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
        cat = infer_category_from_text(text)
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

ALLOWED_OPS = {"add","done","move","delete","subtask","remind"}

def ai_command_mode(user_text, user_id):
    """
    GPT возвращает JSON с действиями, мы их исполняем.
    Примеры действий:
    {"op":"add","date":"ДД.ММ.ГГГГ","time":"ЧЧ:ММ","category":"Кофейня","subcategory":"Центр","text":"..."}
    {"op":"done","match":"к-экспро центр"}  # отметим выполненной ближайшую подходящую
    {"op":"move","match":"оплатить свет","date":"14.08.2025"}
    {"op":"delete","match":"встреча"}
    {"op":"subtask","match":"заказ к-экспро","text":"проверить счёт"}
    {"op":"remind","match":"заказ","time":"13:30"}
    """
    summary = []
    used_ai = False
    if OPENAI_API_KEY:
        try:
            all_tasks = read_all_tasks_for_user(user_id)
            ctx = []
            for r in sorted(all_tasks, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн") or "")):
                ctx.append(f"{r['Дата']}|{r['Категория']}/{r['Подкатегория'] or '—'}|{r['Задача']}|{r.get('Дедлайн') or '—'}")
            ctx = "\n".join(ctx)[:6000]

            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты исполнитель команд для ToDo. Верни ТОЛЬКО JSON-массив действий (ALLOWED_OPS: add,done,move,delete,subtask,remind).\n"
                "Используй поля, описанные в docstring. 'match' — подстрока для поиска задачи."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content":sys},
                    {"role":"user","content":f"Текущие задачи:\n{ctx}\n\nКоманда:\n{user_text}"}
                ],
                temperature=0.2
            )
            raw = resp.choices[0].message.content.strip()
            actions = json.loads(raw)
            if isinstance(actions, dict): actions = [actions]
            used_ai = True
        except Exception as e:
            log.error("AI command parse failed: %s", e)
            actions = []
    else:
        actions = []

    # исполняем
    def find_first_by_match(match):
        m = (match or "").lower()
        if not m: return None
        for r in read_all_tasks_for_user(user_id):
            line = " ".join([str(r.get(k,"")) for k in SHEET_TASK_HEADERS]).lower()
            if m in line: return r
        return None

    for a in actions:
        op = (a.get("op") or "").lower()
        if op not in ALLOWED_OPS: continue

        if op == "add":
            ds = a.get("date") or today_str()
            tm = a.get("time") or ""
            cat = a.get("category") or "Личное"
            sub = a.get("subcategory") or ""
            txt = a.get("text") or ""
            add_task(ds, cat, sub, txt, tm, user_id, source="ai:add")
            summary.append(f"➕ Добавлена: {ds} {cat}/{sub} — {txt}")

        elif op == "done":
            r = find_first_by_match(a.get("match"))
            if not r:
                summary.append("⚠️ Не нашёл задачу для done")
                continue
            tid = r["_id"]
            ok, r2 = mark_done_by_id(tid, user_id)
            if ok:
                summary.append(f"✅ Выполнено: {r['Задача']}")
                sup = guess_supplier(r['Задача'])
                if sup:
                    plan_next_by_supplier_rule(user_id, sup, r.get("Категория","Кофейня"), r.get("Подкатегория",""), r['Задача'])
            else:
                summary.append("⚠️ Не смог отметить выполненной")

        elif op == "move":
            r = find_first_by_match(a.get("match"))
            if not r:
                summary.append("⚠️ Не нашёл задачу для move")
                continue
            new_date = a.get("date") or today_str()
            sheet_title, ws, idx, _ = find_row_index_by_id(r["_id"])
            ws.update_cell(idx, SHEET_TASK_HEADERS.index("Дата")+1, new_date)
            summary.append(f"📅 Перенёс: {r['Задача']} → {new_date}")

        elif op == "delete":
            r = find_first_by_match(a.get("match"))
            if not r:
                summary.append("⚠️ Не нашёл задачу для delete")
                continue
            sheet_title, ws, idx, _ = find_row_index_by_id(r["_id"])
            ws.delete_rows(idx)
            summary.append(f"🗑 Удалил: {r['Задача']}")

        elif op == "subtask":
            r = find_first_by_match(a.get("match"))
            if not r:
                summary.append("⚠️ Не нашёл родительскую задачу для subtask")
                continue
            add_task(r["Дата"], r["Категория"], r["Подкатегория"], f"• {a.get('text')}", r.get("Дедлайн") or "", user_id, source=f"subtask:{r['_id']}")
            summary.append(f"➕ Подзадача к «{r['Задача']}»")

        elif op == "remind":
            r = find_first_by_match(a.get("match"))
            if not r:
                summary.append("⚠️ Не нашёл задачу для remind")
                continue
            tm = a.get("time") or ""
            if not re.fullmatch(r"\d{1,2}:\d{2}", tm):
                summary.append("⚠️ Неверное время для remind")
                continue
            sheet_title, ws, idx, _ = find_row_index_by_id(r["_id"])
            cur_source = (r.get("Источник") or "")
            new_source = (cur_source + "; " if cur_source else "") + f"remind:{tm}"
            ws.update_cell(idx, SHEET_TASK_HEADERS.index("Источник")+1, new_source)
            summary.append(f"⏰ Напоминание {tm} для «{r['Задача']}»")

    if not used_ai and not actions:
        return "AI-команды недоступны (нет OPENAI_API_KEY)."
    return "\n".join(summary) if summary else "Команды распознаны, но ничего не выполнено."

# ===================== СОСТОЯНИЯ =====================
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

# ===================== ХЕНДЛЕРЫ =====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    ensure_user_in_sheet(m.chat.id, m.from_user.full_name if m.from_user else "")
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам. Что делаем?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    date_s = today_str()
    rows = tasks_on_date(uid, date_s)
    if not rows:
        bot.send_message(uid, f"📅 Задачи на {date_s}\n\nЗадач нет.", reply_markup=main_menu()); return
    items = []
    for r in rows:
        tid = r["_id"]
        items.append((build_task_line(r), tid))
    page = 1
    total_pages = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
    slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    header = f"📅 Задачи на {date_s}\n\n" + format_grouped(rows, header_date=date_s)
    bot.send_message(uid, header+"\n\nОткрой карточку задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week(m):
    uid = m.chat.id
    ts = tasks_between(uid, now_local().date(), 7)
    if not ts:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu()); return
    by_day = {}
    for r in ts:
        by_day.setdefault(r["Дата"], []).append(r)
    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        parts.append(format_grouped(by_day[d], header_date=d))
        parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    uid = m.chat.id
    set_state(uid, "adding_text")
    bot.send_message(uid, "Опиши задачу одним сообщением (я распаршу дату/время/категорию).")

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    set_state(uid, "search_text")
    bot.send_message(uid, "Что ищем? Введи часть названия/категории/подкатегории/даты.")

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_menu(m):
    uid = m.chat.id
    set_state(uid, "done_text")
    bot.send_message(uid, "Напиши, что выполнено. Пример: «сделал заказ к-экспро центр».")

@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставка")
def handle_supply(m):
    bot.send_message(m.chat.id, "Меню поставок:", reply_markup=supply_menu())

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    uid = m.chat.id
    set_state(uid, "add_supplier")
    bot.send_message(uid, "Формат: <b>Название; Правило; Дедлайн(опц.); Emoji(опц.); DeliveryOffsetDays(опц.)</b>\n"
                          "Примеры:\nК-Экспро; каждые 2 дня; 14:00; 📦; 1\nИП Вылегжанина; shelf 72h; 14:00; 🥘; 1")

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def handle_today_orders(m):
    uid = m.chat.id
    date_s = today_str()
    rows = tasks_on_date(uid, date_s)
    orders = [r for r in rows if is_order_task(r.get("Задача",""))]
    if not orders:
        bot.send_message(uid, "Сегодня заказов нет.", reply_markup=supply_menu()); return
    kb = types.InlineKeyboardMarkup()
    for i,r in enumerate(orders, start=1):
        kb.add(types.InlineKeyboardButton(build_task_line(r, i), callback_data=mk_cb("open", id=r["_id"])))
    bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🧠 AI ассистент")
def handle_ai(m):
    uid = m.chat.id
    tasks = tasks_between(uid, now_local().date(), 7)
    brief = []
    for r in sorted(tasks, key=lambda x: (datetime.strptime(x["Дата"], "%d.%м.%Y"), x.get("Дедлайн","") or "", x.get("Задача","") or "")):
        brief.append(f"{r['Дата']} • {r['Категория']}/{r['Подкатегория'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
    context = "\n".join(brief)[:4000]
    if not OPENAI_API_KEY:
        bot.send_message(uid, "Совет: начни с задач с ближайшим дедлайном. Крупные — разбей на подзадачи.", reply_markup=main_menu()); return
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = "Ты ассистент по планированию. Кратко, по делу, буллетами, на русском."
        prompt = f"Составь план на сегодня и ближайшие 2 дня. Учти дедлайны. Задачи:\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        bot.send_message(uid, f"🧠 {resp.choices[0].message.content.strip()}", reply_markup=main_menu())
    except Exception as e:
        log.error("AI assistant error: %s", e)
        bot.send_message(uid, "Не удалось получить рекомендации.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🤖 AI команды")
def handle_ai_cmd(m):
    uid = m.chat.id
    set_state(uid, "ai_cmd")
    bot.send_message(uid, "Сформулируй команду: «перенеси заказ к-экспро на завтра», «добавь встречу завтра 15:00 в личное», «закрой заказ вылегжанина центр» и т.п.")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
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
            date_s   = it["date"] or today_str()
            category = it["category"]
            subcat   = it["subcategory"]
            task     = it["task"]
            deadline = it["deadline"]
            repeat   = it["repeat"]
            source   = it["source"]
            add_task(date_s, category, subcat, task, deadline, uid, status="", repeat=repeat, source=source)
            created += 1
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=main_menu())
        log_event(uid, "add_task_nlp", txt)
    except Exception as e:
        log.error("adding_text error: %s", e)
        bot.send_message(uid, "Не смог добавить. Попробуй иначе.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "search_text")
def search_text(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    found = []
    for r in read_all_tasks_for_user(uid):
        hay = " ".join([str(r.get(k,"")) for k in SHEET_TASK_HEADERS]).lower()
        if q in hay:
            found.append((build_task_line(r), r["_id"]))
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
    rows = tasks_on_date(uid, today_str())
    changed = 0
    last_closed = None
    for r in rows:
        if (r.get("Статус","") or "").lower() == "выполнено": continue
        t = (r.get("Задача","") or "").lower()
        if supplier and supplier.lower() not in t: continue
        if not supplier and not any(w in t for w in ["к-экспро","вылегжан","заказ","сделал","сделали"]): continue
        ok, _ = mark_done_by_id(r["_id"], uid)
        if ok:
            changed += 1
            last_closed = r
    msg = f"✅ Отмечено выполненным: {changed}."
    if changed and last_closed and supplier:
        created = plan_next_by_supplier_rule(uid, supplier, last_closed.get("Категория","Кофейня"), last_closed.get("Подкатегория",""), last_closed.get("Задача",""))
        if created:
            msg += "\n🔮 Запланировано авто: " + ", ".join([f"приемка {d1.strftime('%d.%m')} → новый заказ {d2.strftime('%d.%m')}" for d1,d2 in created])
    bot.send_message(uid, msg, reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "ai_cmd")
def ai_cmd_text(m):
    uid = m.chat.id
    res = ai_command_mode(m.text.strip(), uid)
    bot.send_message(uid, f"🤖 {res}", reply_markup=main_menu())
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
        emoji = parts[3] if len(parts) > 3 else "📦"
        d_off = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        ws_suppliers.append_row([name, rule, deadline, emoji, d_off], value_input_option="USER_ENTERED")
        load_supplier_rules_from_sheet()
        bot.send_message(uid, f"✅ Поставщик «{name}» добавлен.", reply_markup=supply_menu())
    except Exception as e:
        log.error("add_supplier error: %s", e)
        bot.send_message(uid, "Не получилось добавить поставщика.", reply_markup=supply_menu())
    finally:
        clear_state(uid)

# ===================== КАРТОЧКИ / CALLBACKS =====================
def render_task_card(task_id):
    sheet_title, ws, idx, r = find_row_index_by_id(task_id)
    if not idx: return "Задача не найдена.", None
    date_s = r.get("Дата","")
    header = (
        f"<b>{r.get('Задача','')}</b>\n"
        f"📅 {weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))} — {date_s}\n"
        f"📁 {r.get('Категория','—')} / {r.get('Подкатегория','—')}  [{sheet_title}]\n"
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
        rows = tasks_on_date(uid, date_s)
        items = [(build_task_line(r), r["_id"]) for r in rows]
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
        text, kb = render_task_card(task_id)
        bot.answer_callback_query(c.id)
        bot.send_message(uid, text, reply_markup=kb)
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
            if created:
                msg += " Запланирована приемка/следующий заказ."
        bot.answer_callback_query(c.id, msg, show_alert=True)
        text, kb = render_task_card(task_id)
        bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
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
        sheet_title, ws, idx, r = find_row_index_by_id(task_id)
        if not idx:
            bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
        date_s = today_str() if when=="today" else today_str(now_local()+timedelta(days=1))
        supplier = guess_supplier(r.get("Задача","")) or "Поставка"
        add_task(date_s, r.get("Категория",""), r.get("Подкатегория",""),
                 f"🚚 Принять поставку {supplier} ({normalize_subcat_hint(r.get('Подкатегория','')) or '—'})",
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
        bot.send_message(uid, "Когда напомнить? ЧЧ:ММ")
        return

# ===================== ТЕКСТ: подзадачи/даты/напоминания =====================
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid); task_id = data.get("task_id")
    text = m.text.strip()
    sheet_title, ws, idx, parent = find_row_index_by_id(task_id)
    if not idx:
        bot.send_message(uid, "Родительская задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    add_task(parent.get("Дата",""), parent.get("Категория",""), parent.get("Подкатегория",""),
             f"• {text}", parent.get("Дедлайн",""), uid, source=f"subtask:{task_id}")
    bot.send_message(uid, "Подзадача добавлена.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "pick_delivery_date")
def pick_delivery_date(m):
    uid = m.chat.id
    data = get_data(uid); task_id = data.get("task_id")
    ds = m.text.strip()
    try:
        datetime.strptime(ds, "%d.%m.%Y")
    except Exception:
        bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
    sheet_title, ws, idx, r = find_row_index_by_id(task_id)
    if not idx:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    supplier = guess_supplier(r.get("Задача","")) or "Поставка"
    add_task(ds, r.get("Категория",""), r.get("Подкатегория",""),
             f"🚚 Принять поставку {supplier} ({normalize_subcat_hint(r.get('Подкатегория','')) or '—'})",
             "10:00", uid, source=f"subtask:{task_id}")
    bot.send_message(uid, f"Создана задача на {ds}.", reply_markup=main_menu())
    clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder(m):
    uid = m.chat.id
    data = get_data(uid); task_id = data.get("task_id")
    t = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
    sheet_title, ws, idx, r = find_row_index_by_id(task_id)
    if not idx:
        bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
    cur_source = r.get("Источник","") or ""
    new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
    ws.update_cell(idx, SHEET_TASK_HEADERS.index("Источник")+1, new_source)
    bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu())
    clear_state(uid)

# ===================== ОБРАБОТКА СЫРОГО ТЕКСТА (если не попало в состояния) =====================
@bot.message_handler(func=lambda msg: True)
def fallback_text(m):
    uid = m.chat.id
    st = get_state(uid)
    if st: 
        return  # состояния уже обрабатываются выше

    text = (m.text or "").strip()
    if not text:
        bot.send_message(uid, "Пустое сообщение. Напиши задачу или выбери пункт меню.", reply_markup=main_menu())
        return

    # 1) если это похоже на команду управления — отдать в AI-команды
    heur = any(w in text.lower() for w in [
        "перенес", "перенеси", "перенести", "перемести", "переместить",
        "закрой", "закрыть", "удали", "удалить", "сделай", "добавь", "напомни",
        "move", "done", "delete", "remind"
    ])
    if heur and OPENAI_API_KEY:
        res = ai_command_mode(text, uid)
        bot.send_message(uid, f"🤖 {res}", reply_markup=main_menu())
        return

    # 2) иначе — пытаемся распарсить как добавление задачи(-задач)
    try:
        items = ai_parse_to_tasks(text, uid)
        if not items:
            bot.send_message(uid, "Не понял. Нажми «Меню» или опиши задачу проще.", reply_markup=main_menu())
            return
        for it in items:
            add_task(
                it["date"] or today_str(),
                it["category"],
                it["subcategory"],
                it["task"],
                it["deadline"],
                uid,
                repeat=it["repeat"],
                source=it["source"]
            )
        bot.send_message(uid, f"📝 Добавил {len(items)}.", reply_markup=main_menu())
        log_event(uid, "free_text_add", text)
    except Exception as e:
        log.error("fallback_text error: %s", e)
        bot.send_message(uid, "Не смог обработать. Попробуй через «➕ Добавить».", reply_markup=main_menu())

# ===================== ДАЙДЖЕСТЫ И НАПОМИНАНИЯ =====================
def build_daily_digest(uid: int) -> str:
    date_s = today_str()
    rows = tasks_on_date(uid, date_s)
    head = f"🌅 Утренний дайджест — {weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))}, {date_s}\n"
    if not rows:
        return head + "\nНа сегодня задач нет."
    return head + "\n" + format_grouped(rows, header_date=date_s)

def job_morning_digest():
    for uid in list_registered_users():
        try:
            bot.send_message(uid, build_daily_digest(uid), reply_markup=main_menu())
        except Exception as e:
            log.warning("digest send fail user %s: %s", uid, e)

def job_reminders_tick():
    now = now_local()
    tnow = now.strftime("%H:%M")
    date_s = today_str(now)
    ymd = now.strftime("%Y%m%d")
    for uid in list_registered_users():
        try:
            rows = tasks_on_date(uid, date_s)
            for r in rows:
                src = (r.get("Источник") or "")
                if f"remind:{tnow}" in src and f"reminded:{tnow}:{ymd}" not in src:
                    # напоминание
                    try:
                        kb = types.InlineKeyboardMarkup()
                        kb.add(types.InlineKeyboardButton("Открыть", callback_data=mk_cb("open", id=r["_id"])))
                        bot.send_message(uid, f"⏰ Напоминание: {r.get('Задача','')} (до {r.get('Дедлайн') or '—'})", reply_markup=kb)
                    except Exception as e:
                        log.warning("remind send fail %s: %s", uid, e)
                    # пометить как отправленное
                    sheet_title, ws, idx, rfull = find_row_index_by_id(r["_id"])
                    if idx:
                        new_source = src + ( "; " if src else "" ) + f"reminded:{tnow}:{ymd}"
                        try:
                            ws.update_cell(idx, SHEET_TASK_HEADERS.index("Источник")+1, new_source)
                        except Exception:
                            pass
        except Exception as e:
            log.warning("reminder loop fail user %s: %s", uid, e)

def scheduler_loop():
    # ежедневный дайджест в 08:00
    schedule.every().day.at("08:00").do(job_morning_digest)
    # напоминания проверяем каждую минуту
    schedule.every().minute.do(job_reminders_tick)
    log.info("Scheduler started: digest 08:00; reminders every minute")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error("schedule error: %s", e)
        time.sleep(1)

def start_scheduler_thread():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    return t

# ===================== FLASK ВЕБХУК / ПОЛЛИНГ =====================
app = Flask(__name__)

@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok"

@app.route(f"/{API_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        json_str = request.get_data(as_text=True)
        update = types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.error("webhook error: %s", e)
    return "ok"

def setup_webhook():
    if not WEBHOOK_URL:
        return False
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        log.info("Webhook set to %s", WEBHOOK_URL)
        return True
    except Exception as e:
        log.error("Failed to set webhook: %s", e)
        return False

# ===================== MAIN =====================
if __name__ == "__main__":
    start_scheduler_thread()
    if setup_webhook():
        # Запуск Flask в веб-среде
        port = int(os.getenv("PORT", "8080"))
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        # Локально/без вебхука — polling
        log.info("Starting polling…")
        ensure_user_in_sheet  # no-op hint for linter
        bot.infinity_polling(timeout=60, skip_pending=True)
