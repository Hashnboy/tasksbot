# -*- coding: utf-8 -*-
"""
Финальная версия ассистента задач без листа «Все задачи».
Поддержка: категории-листы (Кофейня, Табачка, Личное, WB), повторяемость, автопоставки,
пагинация, подзадачи, массовые действия, напоминания, аналитика, NLP на OPENAI_API_KEY.
"""

import os, re, json, time, pytz, hmac, hashlib, logging, schedule, threading
from datetime import datetime, timedelta, date
from typing import List, Dict, Tuple

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

# ===================== БОТ / SHEETS =====================
bot = TeleBot(API_TOKEN, parse_mode="HTML")

# Листы категорий, которые ИЗНАЧАЛЬНО ДОЛЖНЫ существовать
CATEGORY_SHEETS = ["Кофейня","Табачка","Личное","WB"]
SHEETS: Dict[str, gspread.Worksheet] = {}
ws_suppliers = ws_users = ws_logs = None

def connect_sheets():
    global SHEETS, ws_suppliers, ws_users, ws_logs
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_url(TABLE_URL)

    # Обязательные вспомогательные листы
    for title in ["Поставщики", "Пользователи", "Логи"]:
        try:
            ws = sh.worksheet(title)
            if title == "Поставщики": ws_suppliers = ws
            elif title == "Пользователи": ws_users = ws
            else: ws_logs = ws
        except gspread.exceptions.WorksheetNotFound:
            log.error("Не найден обязательный лист: %s (создавать автоматически НЕ будем)", title)
            raise

    # Категорийные листы — только существующие
    SHEETS.clear()
    for title in CATEGORY_SHEETS:
        try:
            SHEETS[title] = sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            log.warning("Не найден категорийный лист: %s (пропускаем, не создаём)", title)

    if not SHEETS:
        raise RuntimeError("Нет ни одного доступного категорийного листа. Проверь названия и регистр.")

    log.info("Google Sheets подключены. Доступные листы: %s", ", ".join(SHEETS.keys()))

connect_sheets()

TASKS_HEADERS = ["Дата","Категория","Подкатегория","Задача","Дедлайн","User ID","Статус","Повторяемость","Источник"]
PAGE_SIZE = 7

# ===================== УТИЛИТЫ ДАТ/ID =====================
WEEKDAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(dt=None):
    if dt is None: dt = now_local()
    return dt.strftime("%d.%m.%Y")
def weekday_ru(dt: datetime) -> str:
    return ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][dt.weekday()]

def parse_ru_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s: return None
    try: return datetime.strptime(s, "%d.%m.%Y").date()
    except: return None

def parse_time(s: str) -> Tuple[int,int] | None:
    if not s: return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m: return None
    hh = int(m.group(1)); mm = int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59: return hh, mm
    return None

def sha_task_id(row: Dict) -> str:
    key = "|".join([str(row.get("Категория","")), str(row.get("Подкатегория","")), str(row.get("Задача","")),
                    str(row.get("Дедлайн","")), str(row.get("User ID","")), str(row.get("Источник",""))])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception:
        pass

# ===================== ЧТЕНИЕ / ЗАПИСЬ ЗАДАЧ =====================
def get_all_rows() -> List[Tuple[str, Dict]]:
    """Вернёт [(sheet_title, row_dict), ...] без заголовка."""
    acc = []
    for title, ws in SHEETS.items():
        try:
            rows = ws.get_all_records()
            for r in rows:
                # Насильная нормализация столбцов
                for h in TASKS_HEADERS:
                    r.setdefault(h, "")
                r["Категория"] = r.get("Категория") or title
                acc.append((title, r))
        except Exception as e:
            log.error("Ошибка чтения листа %s: %s", title, e)
    return acc

def append_to_sheet(category: str, row: List[str]):
    ws = SHEETS.get(category)
    if not ws:
        raise RuntimeError(f"Лист категории «{category}» недоступен (не существует или не подключён).")
    ws.append_row(row, value_input_option="USER_ENTERED")

def update_cell_by_match(category: str, match_row: Dict, col_name: str, value: str) -> bool:
    """Обновит первую совпавшую строку по задачному id (без даты)"""
    ws = SHEETS.get(category); 
    if not ws: return False
    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        probe = {"Категория":category, **r}
        if sha_task_id(probe) == sha_task_id(match_row):
            j = TASKS_HEADERS.index(col_name) + 1
            ws.update_cell(i, j, value)
            return True
    return False

# ===================== ПРАВИЛА ПОСТАВЩИКОВ =====================
SUPPLIER_RULES: Dict[str, Dict] = {}  # lower -> rule

def load_supplier_rules_from_sheet():
    SUPPLIER_RULES.clear()
    try:
        rows = ws_suppliers.get_all_records()
        for r in rows:
            name = str(r.get("Поставщик") or "").strip()
            if not name: continue
            key = name.lower()
            rule = {
                "domain": str(r.get("Правило") or "").strip().lower(),  # «кофейня»/«табачка» …
                "order_deadline": str(r.get("ДедлайнЗаказа") or r.get("Дедлайн") or "14:00").strip(),
                "delivery_offset_days": int(r.get("DeliveryOffsetDays") or 1),
                "storage_hours": int(r.get("Хранение (дни)") or r.get("Хранение_дней") or 0) * 24,
                "start_cycle": str(r.get("Старт_цикла") or "").strip(),
                "auto": str(r.get("Авто") or "").strip(),
                "active": str(r.get("Активен") or "").strip(),
                "emoji": str(r.get("Emoji") or "📦").strip(),
                "weekly": str(r.get("ДедлайнЗаказа Емодзи") or r.get("ДедлайнЗаказаСписком") or "").strip(),  # необяз.
                "areas": str(r.get("ДедлайнЗаказа КатегорияЗаказа") or r.get("ДедлайнЗаказа") or r.get("ДедлайнЗаказаКатегория") or "").strip()
            }
            SUPPLIER_RULES[key] = rule
    except Exception as e:
        log.warning("Не смог загрузить правила поставщиков: %s", e)

load_supplier_rules_from_sheet()

def is_order_task(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ["заказ","заказать","заказик"])

def guess_supplier(text: str) -> str:
    t = (text or "").lower()
    keys = {
        "к-экспро":"К-Экспро",
        "к экспро":"К-Экспро",
        "вылегжан":"ИП Вылегжанина",
        "реал":"Реал",
        "батч":"Батч",
        "лобанов":"ИП Лобанов",
        "авантаж":"Авантаж",
        "федя":"Федя"
    }
    for k,v in keys.items():
        if k in t: return v
    return ""

def normalize_tt_from_subcat(s: str) -> str:
    s = (s or "").lower()
    if "центр" in s: return "Центр"
    if "полет" in s or "полёт" in s: return "Полет"
    if "клим" in s: return "Климово"
    return s.capitalize() if s else ""

# ===================== ПОВТОРЯЕМОСТЬ: разбор и генерация =====================
def parse_repeat(rep: str):
    """
    Поддержка:
    - дни недели: 'вторник', 'четверг', 'среда' ...
    - 'каждые N дней'
    - '72 часа' (эквивалент каждые 3 дня)
    Возвращает dict вида {'type':'weekly','days':[1,3]} или {'type':'every_n','n':2}
    """
    s = (rep or "").strip().lower()
    if not s: return None
    for i, w in enumerate(WEEKDAYS_RU):
        if w in s:
            days = [j for j, name in enumerate(WEEKDAYS_RU) if name in s]
            return {"type":"weekly","days":days or [i]}
    m = re.search(r"каждые?\s+(\d+)\s*д", s)
    if m:
        return {"type":"every_n","n":max(1,int(m.group(1)))}
    m = re.search(r"(\d+)\s*час", s)
    if m:
        n = int(m.group(1))
        return {"type":"every_n","n":max(1, n//24 or 1)}
    return None

def expand_rows_for_range(rows: List[Tuple[str,Dict]], start: date, days: int, uid: str) -> List[Tuple[str,Dict]]:
    """
    На лету создаёт инстансы задач на интервал [start; start+days).
    Берёт как строки с явной датой, так и шаблоны с Повторяемость.
    """
    end = start + timedelta(days=days)
    out = []
    for cat, r in rows:
        # фильтр по владельцу
        if str(r.get("User ID") or "").strip() and str(r.get("User ID")).strip() != str(uid):
            continue
        ds = parse_ru_date(r.get("Дата"))
        if ds:
            if start <= ds < end:
                out.append((cat, r))
            continue

        # без даты: это «шаблон» с повторяемостью
        rep = parse_repeat(r.get("Повторяемость"))
        if not rep: 
            continue

        # старт цикла может быть в Источнике: start:ДД.ММ.ГГГГ
        m = re.search(r"start:(\d{2}\.\d{2}\.\d{4})", str(r.get("Источник","")))
        start_cycle = parse_ru_date(m.group(1)) if m else start

        if rep["type"] == "weekly":
            for i in range(days):
                d = start + timedelta(days=i)
                if d.weekday() in rep["days"]:
                    inst = dict(r)
                    inst["Дата"] = d.strftime("%d.%m.%Y")
                    out.append((cat, inst))
        elif rep["type"] == "every_n":
            # шаг от start_cycle
            n = rep["n"]
            # найдём первое в диапазоне
            cur = start_cycle
            while cur < start:
                cur += timedelta(days=n)
            while cur < end:
                inst = dict(r)
                inst["Дата"] = cur.strftime("%d.%m.%Y")
                out.append((cat, inst))
                cur += timedelta(days=n)
    return out

# ===================== ФОРМАТЫ / UI =====================
def build_task_line(r, i=None):
    dl = r.get("Дедлайн") or "—"
    prefix = f"{i}. " if i else ""
    return f"{prefix}{r.get('Категория','—')}/{r.get('Подкатегория','—')}: {r.get('Задача','')[:40]}… (до {dl})"

def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
    def k(r):
        dl = r.get("Дедлайн") or ""
        try: dlk = datetime.strptime(dl, "%H:%M").time()
        except: dlk = datetime.min.time()
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

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить","🔎 Найти","✅ Выполнить")
    kb.row("🧠 Ассистент","📊 Отчёт","⚙️ Настройки")
    return kb

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
    except Exception: return None

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

# ===================== GPT (NLP + ассистент) =====================
def ai_parse_to_suggest(text):
    """
    Возвращает словарь-подсказку: category, subcategory, supplier, date, time, repeat
    """
    suggestion = {"category":"","subcategory":"","supplier":"","date":"","time":"","repeat":""}
    tl = text.lower()
    suggestion["supplier"] = guess_supplier(text)
    if "кофейн" in tl or suggestion["supplier"] in ["К-Экспро","ИП Вылегжанина","Реал","Батч","ИП Лобанов","Авантаж"]:
        suggestion["category"] = "Кофейня"
    elif "табач" in tl or suggestion["supplier"] == "Федя":
        suggestion["category"] = "Табачка"
    m = re.search(r"(центр|пол[её]т|климово)", tl)
    if m: suggestion["subcategory"] = normalize_tt_from_subcat(m.group(1))
    m = re.search(r"(\d{1,2}:\d{2})", text)
    if m: suggestion["time"] = m.group(1)
    if any(w in tl for w in ["сегодня","today"]): suggestion["date"] = today_str()
    elif any(w in tl for w in ["завтра","tomorrow"]): suggestion["date"] = today_str(now_local()+timedelta(days=1))
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text); 
    if m: suggestion["date"] = m.group(1)
    # repeat
    for w in WEEKDAYS_RU:
        if w in tl: suggestion["repeat"] = w
    m = re.search(r"каждые?\s+\d+\s*д", tl)
    if m: suggestion["repeat"] = m.group(0)
    m = re.search(r"\d+\s*час", tl)
    if m: suggestion["repeat"] = m.group(0)

    # при наличии OPENAI — уточнение (без флуда)
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = ("Ты парсер задач. Верни ТОЛЬКО JSON с полями: "
                   "category, subcategory, supplier, date(ДД.ММ.ГГГГ|''), time(ЧЧ:ММ|''), repeat(''|строка описания).")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":text}],
                temperature=0.1
            )
            raw = resp.choices[0].message.content.strip()
            ai = json.loads(raw)
            for k in suggestion.keys():
                if ai.get(k): suggestion[k] = ai[k]
        except Exception as e:
            log.warning("AI parse hint failed: %s", e)
    return suggestion

def ai_assist_answer(query, user_id):
    try:
        # контекст — неделя вперёд
        start = now_local().date()
        rows = get_all_rows()
        expanded = expand_rows_for_range(rows, start, 7, str(user_id))
        brief = []
        for _, r in sorted(expanded, key=lambda x: (datetime.strptime(x[1]["Дата"], "%d.%m.%Y"), x[1].get("Дедлайн","") or "", x[1].get("Задача","") or "")):
            brief.append(f"{r['Дата']} • {r['Категория']}/{r['Подкатегория'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
        context = "\n".join(brief)[:4000]
        if not OPENAI_API_KEY:
            return "Совет: сначала срочные/важные, затем короткие победы. Блокируй время под поставки и заказы."
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = "Ты ассистент по задачам. Суммируй чётко и кратко, пунктами, на русском."
        prompt = f"Запрос: {query}\n\nЗадачи на неделю:\n{context}\n\nСформируй план действий и предложи 2-3 автодобавления."
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("AI assistant error: %s", e)
        return "Не удалось сформировать ответ ассистента."

# ===================== ДОБАВЛЕНИЕ ЗАДАЧ (мастер по кнопкам) =====================
ADD_WIZARD: Dict[int, Dict] = {}  # uid -> {title, category, subcategory, date, time, repeat}

def start_add_wizard(uid, title):
    hint = ai_parse_to_suggest(title)
    ADD_WIZARD[uid] = {"title": title, **hint}

def add_wizard_kb(uid):
    st = ADD_WIZARD.get(uid, {})
    kb = types.InlineKeyboardMarkup()
    # 1) Категория
    row = []
    for cat in CATEGORY_SHEETS:
        if cat in SHEETS:
            label = "✅ "+cat if st.get("category")==cat else cat
            row.append(types.InlineKeyboardButton(label, callback_data=mk_cb("w_cat", v=cat)))
    if row: kb.row(*row)
    # 2) Подкатегория
    for chunk in [["Центр","Полет","Климово"],["—"]]:
        row=[]
        for sub in chunk:
            sel = normalize_tt_from_subcat(sub) if sub!="—" else ""
            label = ("✅ "+(sel or "—")) if (st.get("subcategory") or "")==sel else (sel or "—")
            row.append(types.InlineKeyboardButton(label, callback_data=mk_cb("w_sub", v=sel)))
        kb.row(*row)
    # 3) Дата
    today = now_local().date()
    row=[]
    for d,cap in [(today,"Сегодня"),(today+timedelta(days=1),"Завтра"),(today+timedelta(days=2),"+2"),(today+timedelta(days=7),"+7")]:
        ds = d.strftime("%d.%m.%Y")
        label = "✅ "+cap if st.get("date")==ds else cap
        row.append(types.InlineKeyboardButton(label, callback_data=mk_cb("w_date", v=ds)))
    kb.row(*row)
    kb.row(types.InlineKeyboardButton("📅 Ввести дату", callback_data=mk_cb("w_date_manual")))
    # 4) Время
    row=[]
    for t in ["10:00","12:00","14:00","16:00"]:
        label = "✅ "+t if st.get("time")==t else t
        row.append(types.InlineKeyboardButton(label, callback_data=mk_cb("w_time", v=t)))
    kb.row(*row)
    kb.row(types.InlineKeyboardButton("⏰ Ввести время", callback_data=mk_cb("w_time_manual")))
    # 5) Повторяемость
    row=[]
    for r in ["","понедельник","вторник","среда","четверг","пятница","суббота","воскресенье","каждые 2 дня","каждые 3 дня"]:
        label = "🔁 "+(r or "нет") if st.get("repeat")==r else (r or "без повтора")
        row.append(types.InlineKeyboardButton(label, callback_data=mk_cb("w_rep", v=r)))
        if len(row)==3: kb.row(*row); row=[]
    if row: kb.row(*row)
    # 6) Сохранить
    kb.row(types.InlineKeyboardButton("✅ Добавить", callback_data=mk_cb("w_save")))
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data=mk_cb("w_cancel")))
    return kb

def finish_wizard(uid):
    st = ADD_WIZARD.get(uid, {})
    cat = st.get("category") or "Личное"
    sub = st.get("subcategory") or ""
    title = st.get("title") or ""
    date_s = st.get("date") or today_str()
    time_s = st.get("time") or ""
    repeat = st.get("repeat") or ""
    row = [date_s, cat, sub, title, time_s, str(uid), "", repeat, "wizard"]
    append_to_sheet(cat, row)
    if is_order_task(title):
        # предложим «Принять поставку»
        supplier = guess_supplier(title) or "Поставка"
        # по умолчанию — завтра 10:00
        ds = parse_ru_date(date_s) or now_local().date()
        accept_day = (ds + timedelta(days=1)).strftime("%d.%m.%Y")
        sub_row = [accept_day, cat, sub,
                   f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(sub) or '—'})",
                   "10:00", str(uid), "", "", f"subtask:{sha_task_id({'Категория':cat,'Подкатегория':sub,'Задача':title,'Дедлайн':time_s,'User ID':uid})}"]
        append_to_sheet(cat, sub_row)
    return cat

# ===================== ХЕНДЛЕРЫ =====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я готов. Выбирай действие:", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    uid = m.chat.id
    bot.send_message(uid, "Напиши название задачи одной строкой. Я подскажу параметры и дам кнопки.")
    bot.register_next_step_handler(m, add_step_title)

def add_step_title(m):
    uid = m.chat.id
    title = m.text.strip()
    start_add_wizard(uid, title)
    st = ADD_WIZARD[uid]
    bot.send_message(uid, f"Задача: <b>{st['title']}</b>\nВыбери параметры 👇", reply_markup=add_wizard_kb(uid))

@bot.callback_query_handler(func=lambda c: parse_cb(c.data) and parse_cb(c.data).get("a","").startswith("w_"))
def wizard_callbacks(c):
    uid = c.message.chat.id
    data = parse_cb(c.data)
    a = data.get("a"); v = data.get("v","")
    st = ADD_WIZARD.get(uid, {})
    if not st: 
        bot.answer_callback_query(c.id,"Мастер закрыт"); return
    if a == "w_cat": st["category"] = v
    elif a == "w_sub": st["subcategory"] = v
    elif a == "w_date": st["date"] = v
    elif a == "w_time": st["time"] = v
    elif a == "w_rep": st["repeat"] = v
    elif a == "w_date_manual":
        bot.answer_callback_query(c.id); bot.send_message(uid,"Введи дату ДД.ММ.ГГГГ:")
        bot.register_next_step_handler(c.message, wizard_date_manual); return
    elif a == "w_time_manual":
        bot.answer_callback_query(c.id); bot.send_message(uid,"Введи время ЧЧ:ММ:")
        bot.register_next_step_handler(c.message, wizard_time_manual); return
    elif a == "w_save":
        cat = finish_wizard(uid)
        ADD_WIZARD.pop(uid, None)
        bot.answer_callback_query(c.id,"Добавил!")
        bot.edit_message_text("✅ Задача добавлена.", uid, c.message.message_id)
        log_event(uid, "add_task_wizard", st.get("title",""))
        return
    elif a == "w_cancel":
        ADD_WIZARD.pop(uid, None)
        bot.answer_callback_query(c.id,"Отменено")
        bot.edit_message_text("Отмена.", uid, c.message.message_id)
        return
    ADD_WIZARD[uid] = st
    try:
        bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=add_wizard_kb(uid))
    except Exception:
        pass
    bot.answer_callback_query(c.id)

def wizard_date_manual(m):
    uid = m.chat.id
    ds = m.text.strip()
    try: datetime.strptime(ds, "%d.%m.%Y")
    except: bot.send_message(uid,"Некорректная дата."); return
    ADD_WIZARD.setdefault(uid, {})["date"] = ds
    bot.send_message(uid,"Ок. Продолжай выбор кнопками.", reply_markup=add_wizard_kb(uid))

def wizard_time_manual(m):
    uid = m.chat.id
    ts = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", ts):
        bot.send_message(uid,"Некорректное время."); return
    ADD_WIZARD.setdefault(uid, {})["time"] = ts
    bot.send_message(uid,"Ок. Продолжай выбор кнопками.", reply_markup=add_wizard_kb(uid))

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    start = now_local().date()
    rows = get_all_rows()
    expanded = expand_rows_for_range(rows, start, 1, str(uid))
    tasks = [r for _, r in expanded if r["Дата"] == start.strftime("%d.%m.%Y")]
    if not tasks:
        bot.send_message(uid, f"На сегодня задач нет.", reply_markup=main_menu()); return
    by_cat = {}
    for r in tasks:
        by_cat.setdefault(r["Категория"], []).append(r)
    parts=[]
    for dcat in by_cat:
        parts.append(format_grouped(by_cat[dcat], header_date=today_str()))
        parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week(m):
    uid = m.chat.id
    start = now_local().date()
    rows = get_all_rows()
    expanded = expand_rows_for_range(rows, start, 7, str(uid))
    if not expanded:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu()); return
    by_day: Dict[str, List[Dict]] = {}
    for _, r in expanded:
        by_day.setdefault(r["Дата"], []).append(r)
    parts=[]
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        parts.append(format_grouped(by_day[d], header_date=d)); parts.append("")
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    uid = m.chat.id
    bot.send_message(uid,"Что ищем? Введи часть названия/категории/подкатегории/даты.")
    bot.register_next_step_handler(m, search_query)

def search_query(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    rows = get_all_rows()
    found=[]
    for cat, r in rows:
        if str(r.get("User ID") or "").strip() and str(r["User ID"]) != str(uid):
            continue
        hay = " ".join([str(r.get(k,"")) for k in TASKS_HEADERS]).lower()
        if q in hay:
            tid = sha_task_id({"Категория":cat, **r})
            found.append((build_task_line(r), tid))
    if not found:
        bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); return
    total_pages = (len(found)+PAGE_SIZE-1)//PAGE_SIZE
    page = 1
    slice_items = found[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    kb = page_buttons(slice_items, page, total_pages, prefix_action="open")
    bot.send_message(uid, "Найденные задачи:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "✅ Выполнить")
def handle_done_prompt(m):
    uid = m.chat.id
    bot.send_message(uid, "Напиши часть названия/поставщика. Я предложу задачи на сегодня для завершения.")
    bot.register_next_step_handler(m, done_text)

def done_text(m):
    uid = m.chat.id
    q = m.text.strip().lower()
    start = now_local().date()
    rows = get_all_rows()
    today_tasks = [r for _, r in expand_rows_for_range(rows, start, 1, str(uid))]
    cand=[]
    for cat, r in today_tasks:
        if r.get("Статус","").lower()=="выполнено": continue
        hay = " ".join([r.get("Задача",""), r.get("Категория",""), r.get("Подкатегория",""), r.get("Источник","")]).lower()
        if q in hay:
            cand.append((cat, r))
    if not cand:
        bot.send_message(uid,"Ничего не нашёл на сегодня.")
        return
    kb = types.InlineKeyboardMarkup()
    for i,(cat, r) in enumerate(cand, start=1):
        tid = sha_task_id({"Категория":cat, **r})
        kb.add(types.InlineKeyboardButton(build_task_line(r,i), callback_data=mk_cb("done", id=tid)))
    bot.send_message(uid, "Что отметить выполненным?", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    uid = m.chat.id
    bot.send_message(uid, "Что нужно? (спланировать день, выделить приоритеты, составить расписание и т.д.)")
    bot.register_next_step_handler(m, assistant_text)

def assistant_text(m):
    uid = m.chat.id
    answer = ai_assist_answer(m.text.strip(), uid)
    bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📊 Отчёт")
def handle_report(m):
    uid = m.chat.id
    start = now_local().date()
    rows = get_all_rows()
    expanded = expand_rows_for_range(rows, start - timedelta(days=7), 14, str(uid))
    total = len(expanded)
    done = sum(1 for _,r in expanded if (r.get("Статус","").lower()=="выполнено"))
    bot.send_message(uid, f"За последние 2 недели:\nВсего: {total}\nВыполнено: {done}\nОткрыто: {total-done}")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nУтренний дайджест: <b>08:00</b>", reply_markup=main_menu())

# ===================== CALLBACKS: открыть/выполнить/подзадачи/пагинация =====================
def find_row_by_tid(tid) -> Tuple[str, Dict] | None:
    rows = get_all_rows()
    for cat, r in rows:
        if sha_task_id({"Категория":cat, **r}) == tid:
            return cat, r
    return None

def render_task_card(cat, r):
    date_s = r.get("Дата") or "—"
    header = (f"<b>{r.get('Задача','')}</b>\n"
              f"📅 {date_s if date_s=='—' else (weekday_ru(datetime.strptime(date_s,'%d.%m.%Y'))+' — '+date_s)}\n"
              f"📁 {r.get('Категория','—')} / {r.get('Подкатегория','—')}\n"
              f"⏰ Дедлайн: {r.get('Дедлайн') or '—'}\n"
              f"📝 Статус: {r.get('Статус') or '—'}\n"
              f"🔁 Повторяемость: {r.get('Повторяемость') or '—'}")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=sha_task_id({"Категория":cat, **r}))))
    if is_order_task(r.get("Задача","")):
        kb.add(types.InlineKeyboardButton("🚚 Принять поставку", callback_data=mk_cb("accept_delivery", id=sha_task_id({"Категория":cat, **r}))))
    kb.add(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("add_sub", id=sha_task_id({"Категория":cat, **r}))))
    kb.add(types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("remind_set", id=sha_task_id({"Категория":cat, **r}))))
    kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data=mk_cb("close_card")))
    return header, kb

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid = c.message.chat.id
    data = parse_cb(c.data) if c.data and c.data!="noop" else None
    if not data: 
        bot.answer_callback_query(c.id); return
    a = data.get("a")

    if a == "page":
        # В этом релизе пагинация ограниченно обновляется (перерисовывать текущий список не будем)
        bot.answer_callback_query(c.id); return

    if a == "open":
        tid = data.get("id")
        found = find_row_by_tid(tid)
        bot.answer_callback_query(c.id)
        if not found:
            bot.send_message(uid,"Задача не найдена."); return
        cat, r = found
        text, kb = render_task_card(cat, r)
        bot.send_message(uid, text, reply_markup=kb)
        return

    if a == "close_card":
        bot.answer_callback_query(c.id, "Закрыто")
        try: bot.delete_message(uid, c.message.message_id)
        except Exception: pass
        return

    if a == "done":
        tid = data.get("id")
        found = find_row_by_tid(tid)
        if not found:
            bot.answer_callback_query(c.id,"Не нашёл", show_alert=True); return
        cat, r = found
        ok = update_cell_by_match(cat, r, "Статус", "выполнено")
        if not ok:
            bot.answer_callback_query(c.id,"Не смог обновить", show_alert=True); return
        msg = "✅ Готово."
        supplier = guess_supplier(r.get("Задача",""))
        if supplier:
            # автопланирование (простой сценарий: приемка завтра, новый заказ через 2-3 дня по поставщику)
            ds = parse_ru_date(r.get("Дата")) or now_local().date()
            accept_day = (ds + timedelta(days=1)).strftime("%d.%m.%Y")
            add_row = [accept_day, cat, r.get("Подкатегория",""),
                       f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                       "10:00", str(uid), "", "", f"auto:delivery:{supplier}"]
            append_to_sheet(cat, add_row)
            msg += " Запланирована приемка."
        bot.answer_callback_query(c.id, msg, show_alert=True)
        return

    if a == "accept_delivery":
        tid = data.get("id")
        found = find_row_by_tid(tid)
        if not found:
            bot.answer_callback_query(c.id,"Не нашёл", show_alert=True); return
        cat, r = found
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("Сегодня", callback_data=mk_cb("accept_delivery_date", id=tid, d="today")),
            types.InlineKeyboardButton("Завтра", callback_data=mk_cb("accept_delivery_date", id=tid, d="tomorrow")),
        )
        kb.row(types.InlineKeyboardButton("📅 Другая дата", callback_data=mk_cb("accept_delivery_pick", id=tid)))
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда принять поставку?", reply_markup=kb)
        return

    if a == "accept_delivery_pick":
        set_state(uid, "pick_delivery_date", {"task_id": data.get("id")})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")
        return

    if a == "accept_delivery_date":
        tid = data.get("id"); when = data.get("d")
        found = find_row_by_tid(tid)
        if not found:
            bot.answer_callback_query(c.id,"Не нашёл", show_alert=True); return
        cat, r = found
        ds = today_str() if when=="today" else today_str(now_local()+timedelta(days=1))
        supplier = guess_supplier(r.get("Задача","")) or "Поставка"
        append_to_sheet(cat, [ds, cat, r.get("Подкатегория",""),
                              f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                              "10:00", str(uid), "", "", f"subtask:{tid}"])
        bot.answer_callback_query(c.id, f"Создана задача на {ds}", show_alert=True)
        return

    if a == "add_sub":
        set_state(uid, "add_subtask_text", {"task_id": data.get("id")})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Введи текст подзадачи:")
        return

    if a == "remind_set":
        set_state(uid, "set_reminder", {"task_id": data.get("id")})
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Когда напомнить? ЧЧ:ММ (сегодня, локальное время).")
        return

# ======= Текстовые состояния для подзадач/дат/напоминаний =======
USER_STATE: Dict[int,str] = {}
USER_DATA: Dict[int,Dict] = {}
def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None: USER_DATA[uid] = data
def get_state(uid): return USER_STATE.get(uid)
def get_data(uid): return USER_DATA.get(uid, {})
def clear_state(uid): USER_STATE.pop(uid, None); USER_DATA.pop(uid, None)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_subtask_text")
def add_subtask_text(m):
    uid = m.chat.id
    data = get_data(uid); tid = data.get("task_id")
    found = find_row_by_tid(tid)
    if not found:
        bot.send_message(uid,"Родительская задача не найдена."); clear_state(uid); return
    cat, parent = found
    append_to_sheet(cat, [parent.get("Дата",""), cat, parent.get("Подкатегория",""),
                          f"• {m.text.strip()}", parent.get("Дедлайн",""), str(uid), "", "", f"subtask:{tid}"])
    bot.send_message(uid,"Подзадача добавлена.", reply_markup=main_menu()); clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "pick_delivery_date")
def pick_delivery_date(m):
    uid = m.chat.id
    data = get_data(uid); tid = data.get("task_id")
    ds = m.text.strip()
    try: datetime.strptime(ds, "%d.%m.%Y")
    except: bot.send_message(uid,"Дата некорректна."); clear_state(uid); return
    found = find_row_by_tid(tid)
    if not found: bot.send_message(uid,"Задача не найдена."); clear_state(uid); return
    cat, r = found
    supplier = guess_supplier(r.get("Задача","")) or "Поставка"
    append_to_sheet(cat, [ds, cat, r.get("Подкатегория",""),
                          f"🚚 Принять поставку {supplier} ({normalize_tt_from_subcat(r.get('Подкатегория','')) or '—'})",
                          "10:00", str(uid), "", "", f"subtask:{tid}"])
    bot.send_message(uid, f"Создана задача на {ds}.", reply_markup=main_menu()); clear_state(uid)

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder(m):
    uid = m.chat.id
    data = get_data(uid); tid = data.get("task_id")
    t = m.text.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", t):
        bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
    found = find_row_by_tid(tid)
    if not found: bot.send_message(uid,"Задача не найдена."); clear_state(uid); return
    cat, r = found
    cur_source = r.get("Источник","") or ""
    new_source = (cur_source + "; " if cur_source else "") + f"remind:{t}"
    update_cell_by_match(cat, r, "Источник", new_source)
    bot.send_message(uid, f"Напоминание установлено на {t}.", reply_markup=main_menu()); clear_state(uid)

# ===================== ДАЙДЖЕСТ/НАПОМИНАНИЯ =====================
NOTIFIED = set()

def job_daily_digest():
    try:
        users = ws_users.get_all_records()
        today = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid: continue
            start = now_local().date()
            rows = get_all_rows()
            expanded = expand_rows_for_range(rows, start, 1, uid)
            tasks = [r for _, r in expanded if r["Дата"] == today]
            if not tasks: continue
            text = f"📅 План на {today}\n\n" + format_grouped(tasks, header_date=today)
            try: bot.send_message(uid, text)
            except Exception as e: log.error("send digest error %s", e)
    except Exception as e:
        log.error("job_daily_digest error %s", e)

def job_reminders():
    try:
        today = today_str()
        rows = get_all_rows()
        for cat, r in rows:
            if r.get("Дата") != today: continue
            src = (r.get("Источник") or "")
            if "remind:" not in src: continue
            matches = re.findall(r"remind:(\d{1,2}:\d{2})", src)
            for tm in matches:
                key = sha_task_id({"Категория":cat, **r}) + "|" + today + "|" + tm
                if key in NOTIFIED: continue
                try:
                    hh, mm = map(int, tm.split(":"))
                    nowt = now_local().time()
                    if (nowt.hour, nowt.minute) >= (hh, mm):
                        bot.send_message(r.get("User ID"), f"⏰ Напоминание: {r.get('Категория','')}/{r.get('Подкатегория','')} — {r.get('Задача','')} (до {r.get('Дедлайн') or '—'})")
                        NOTIFIED.add(key)
                except Exception: pass
    except Exception as e:
        log.error("job_reminders error %s", e)

def scheduler_thread():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_reminders)
    while True:
        schedule.run_pending(); time.sleep(1)

# ===================== WEBHOOK / RUN =====================
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

if __name__ == "__main__":
    if not API_TOKEN or not WEBHOOK_URL:
        raise RuntimeError("Не заданы TELEGRAM_TOKEN или WEBHOOK_BASE")

    try: bot.remove_webhook()
    except Exception: pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)

    threading.Thread(target=scheduler_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")))
