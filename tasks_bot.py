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

# ========= НАСТРОЙКИ И ОКРУЖЕНИЕ =========
# Все чувствительные данные берем из ENV
API_TOKEN       = os.getenv("TELEGRAM_TOKEN")              # токен бота
TABLE_URL       = os.getenv("GOOGLE_SHEETS_URL")           # URL таблицы
CREDENTIALS_FILE= os.getenv("GOOGLE_CREDENTIALS_JSON", "/etc/secrets/credentials.json")
WEBHOOK_BASE    = os.getenv("WEBHOOK_BASE")                 # например: https://tasksbot-hy3t.onrender.com
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")               # ключ OpenAI

# Проверки окружения (мягкие: логируем, но не падаем)
missing_env = []
for var in ["TELEGRAM_TOKEN", "GOOGLE_SHEETS_URL", "WEBHOOK_BASE"]:
    if not os.getenv(var):
        missing_env.append(var)
if missing_env:
    print(f"[WARN] Не заданы переменные: {', '.join(missing_env)}")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}" if API_TOKEN and WEBHOOK_BASE else None

# Часовой пояс (подстрои под себя)
LOCAL_TZ = pytz.timezone(os.getenv("TZ", "Europe/Moscow"))

# Логирование (в stdout для Render)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# ========= ИНИЦИАЛИЗАЦИЯ БОТА / SHEETS =========
bot = TeleBot(API_TOKEN, parse_mode="HTML")

gc = gspread.service_account(filename=CREDENTIALS_FILE)
sh = gc.open_by_url(TABLE_URL)

# Ожидаем, что у тебя есть листы:
# "Все задачи", "Повторяющиеся задачи", "Поставщики", "Словарь ТТ", "Пользователи", "Логи"
ws_tasks       = sh.worksheet("Все задачи")
ws_repeating   = sh.worksheet("Повторяющиеся задачи")
ws_suppliers   = sh.worksheet("Поставщики")
ws_tt_dict     = sh.worksheet("Словарь ТТ")
ws_users       = sh.worksheet("Пользователи")
ws_logs        = sh.worksheet("Логи")

# Колонки для "Все задачи"
# Дата | Направление | Категория | ТТ | Задача | Дедлайн | Статус | Тип | Повтор ID | User ID | Метки
TASKS_HEADERS = ["Дата","Направление","Категория","ТТ","Задача","Дедлайн","Статус","Тип","Повтор ID","User ID","Метки"]

# Колонки для "Повторяющиеся задачи"
# Правило | Направление | Категория | ТТ | Задача | Время | Поставщик | User ID | Активна
REPEAT_HEADERS = ["Правило","Направление","Категория","ТТ","Задача","Время","Поставщик","User ID","Активна"]

# ========= УТИЛИТЫ =========
def now_local():
    return datetime.now(LOCAL_TZ)

def today_str():
    return now_local().strftime("%d.%m.%Y")

def weekday_ru(dt: datetime) -> str:
    mapping = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return mapping[dt.weekday()]

def log_event(user_id, action, payload=""):
    try:
        ws_logs.append_row([datetime.utcnow().isoformat(), str(user_id), action, payload], value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Логи недоступны: {e}")

def read_all(ws):
    rows = ws.get_all_records()
    return rows

def ensure_headers(ws, expected_headers):
    try:
        headers = ws.row_values(1)
        if headers != expected_headers:
            log.warning(f"Заголовки листа {ws.title} не совпадают с ожидаемыми.\nОжидались: {expected_headers}\nФактически: {headers}")
    except Exception as e:
        log.error(f"Не смогли проверить заголовки {ws.title}: {e}")

ensure_headers(ws_tasks, TASKS_HEADERS)
ensure_headers(ws_repeating, REPEAT_HEADERS)

# Направления/ТТ из словаря
def load_tt():
    tt_rows = read_all(ws_tt_dict)
    # Структура строк предполагается: Направление | ТТ
    tt_map = {}
    for r in tt_rows:
        direction = (r.get("Направление") or "").strip()
        tt = (r.get("ТТ") or "").strip()
        if direction and tt:
            tt_map.setdefault(direction.lower(), set()).add(tt)
    return tt_map

TT_MAP = load_tt()

def direction_for_supplier(supplier_name: str) -> str:
    """Ищем направление поставщика в листе Поставщики."""
    name = (supplier_name or "").strip().lower()
    for r in read_all(ws_suppliers):
        if (r.get("Поставщик") or "").strip().lower() == name:
            return (r.get("Направление") or "").strip()
    return ""

# ========= ПРАВИЛА ПОСТАВЩИКОВ =========
# К-Экспро: бесплатная "качель": сегодня заказ → завтра поставка → послезавтра снова заказ (каждые 2 дня)
# ИП Вылегжанина (Кухня): заказ сегодня → поставка завтра → срок 72ч → новый заказ через 2 дня после поставки
SUPPLIER_RULES = {
    "к-экспро": {
        "kind": "cycle_every_n_days",
        "order_every_days": 2,      # каждые 2 дня ставить заказ
        "delivery_offset_days": 1,  # поставка на следующий день после заказа
        "order_deadline": "14:00",  # дедлайн по умолчанию
        "emoji": "📦"
    },
    "ип вылегжанина": {
        "kind": "delivery_shelf_then_order",
        "delivery_offset_days": 1,   # поставка завтра
        "shelf_hours": 72,           # хранение 72ч
        "order_deadline": "14:00",
        "emoji": "🥘"
    }
}

def normalize_supplier(name: str) -> str:
    s = (name or "").strip().lower()
    # частые варианты
    s = s.replace("к-экспро","к-экспро").replace("k-exp", "к-экспро")
    s = s.replace("ип вылегжанина","ип вылегжанина")
    return s

# ========= ДОБАВЛЕНИЕ ЗАДАЧ =========
def add_task_row(date_str, direction, category, tt, text, deadline, status, ttype, repeat_id, user_id, tags=""):
    row = [date_str, direction, category, tt, text, deadline, status, ttype, repeat_id, str(user_id), tags]
    ws_tasks.append_row(row, value_input_option="USER_ENTERED")

def mark_done_by_index(user_id, date_str, index_1based):
    rows = ws_tasks.get_all_records()
    same_day = [r for r in rows if (r.get("User ID") and str(r.get("User ID")) == str(user_id)) and (r.get("Дата") == date_str)]
    if 1 <= index_1based <= len(same_day):
        target = same_day[index_1based-1]
        # Найдем строку по уникальному набору: дата+текст+user (надежней — find, но ок)
        cell = ws_tasks.find(target["Задача"])
        if cell:
            ws_tasks.update_cell(cell.row, TASKS_HEADERS.index("Статус")+1, "выполнено")
            return target
    return None

def mark_done_by_text(user_id, supplier=None, tt=None, match_all=False):
    """Помечает задачи сегодня по текстовому вводу, например:
       'я сделал заказы к-экспро центр', 'я сделал все заказы к-экспро'
    """
    supplier_key = normalize_supplier(supplier or "")
    today = today_str()
    rows = ws_tasks.get_all_records()
    changed = []

    for r in rows:
        if str(r.get("User ID")) != str(user_id):
            continue
        if r.get("Дата") != today:
            continue
        text = (r.get("Задача") or "").lower()
        tt_row = (r.get("ТТ") or "").strip().lower()
        if supplier_key and supplier_key not in text:
            continue
        if tt and (tt_row != tt.strip().lower()):
            continue
        if r.get("Статус","").lower() == "выполнено":
            continue
        # апдейтим статус
        cell = ws_tasks.find(r["Задача"])
        if cell:
            ws_tasks.update_cell(cell.row, TASKS_HEADERS.index("Статус")+1, "выполнено")
            changed.append(r)
            if not match_all:
                break

    return changed

# ========= ГЕНЕРАЦИЯ ПО ПРАВИЛАМ ПОСТАВЩИКОВ =========
def plan_next_by_supplier_rule(user_id, supplier_name, direction, tt_list):
    key = normalize_supplier(supplier_name)
    rule = SUPPLIER_RULES.get(key)
    if not rule:
        return []

    created = []
    today = now_local().date()
    today_s = today.strftime("%d.%m.%Y")

    if rule["kind"] == "cycle_every_n_days":
        # Сегодня заказ (уже отмечен done) → ставим приемку на завтра, и новый заказ через 2 дня
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        new_order_day = today + timedelta(days=rule["order_every_days"])
        for tt in tt_list:
            # приемка
            add_task_row(delivery_day.strftime("%d.%m.%Y"), direction, "Поставка", tt,
                         f"{rule['emoji']} Приемка {supplier_name} ({tt})", "10:00", "", "разовая", "", user_id, "приемка")
            # новый заказ
            add_task_row(new_order_day.strftime("%d.%m.%Y"), direction, "Закупка", tt,
                         f"{rule['emoji']} Заказ {supplier_name} ({tt})", rule["order_deadline"], "", "разовая", "", user_id, "авто")
            created.append((tt, delivery_day, new_order_day))

    elif rule["kind"] == "delivery_shelf_then_order":
        # Сегодня заказ (done) → завтра приемка → через 2 дня после приемки новый заказ
        delivery_day = today + timedelta(days=rule["delivery_offset_days"])
        next_order_day = delivery_day + timedelta(days=2)  # 48ч после доставки (из 72ч логика: заказ за день до конца)
        for tt in tt_list:
            add_task_row(delivery_day.strftime("%d.%m.%Y"), direction, "Поставка", tt,
                         f"{rule['emoji']} Приемка {supplier_name} ({tt})", "11:00", "", "разовая", "", user_id, "приемка")
            add_task_row(next_order_day.strftime("%d.%m.%Y"), direction, "Закупка", tt,
                         f"{rule['emoji']} Заказ {supplier_name} ({tt})", rule["order_deadline"], "", "разовая", "", user_id, "авто")
            created.append((tt, delivery_day, next_order_day))
    return created

# ========= GPT ПАРСИНГ ВВОДА =========
def ai_parse_free_text(text, fallback_user_id):
    """
    Возвращает список задач (обычных и/или повторяющихся) в унифицированном виде:
    {
      "kind": "single" | "repeat",
      "date": "ДД.ММ.ГГГГ" (для single) или null,
      "time": "ЧЧ:ММ" | "",
      "direction": "Кофейня" | "Табачка" | "WB" | "Личное",
      "category": "Закупка" | "Поставка" | "...",
      "tt": "Центр" | "Полет" | "Климово" | "" (может быть список через ',' для множественных ТТ),
      "text": "Описание задачи",
      "repeat_rule": "каждые 2 дня / каждый вторник 12:00 / по пн,ср" (для repeat),
      "supplier": "К-Экспро" | "ИП Вылегжанина" | "",
      "user_id": fallback_user_id
    }
    """
    items = []

    # 1) Попробуем OpenAI (если доступен)
    used_ai = False
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            sys = (
                "Ты парсер задач. Верни JSON-массив объектов (без текста вне JSON). "
                "Каждый объект со схемой: "
                "{kind, date, time, direction, category, tt, text, repeat_rule, supplier}. "
                "kind: 'single' или 'repeat'. date = 'ДД.ММ.ГГГГ' для single, иначе ''. "
                "time = 'ЧЧ:ММ' или ''. direction из {Кофейня, Табачка, WB, Личное}. "
                "tt из {Центр, Полет, Климово, ''}. supplier укажи если узнаешь. "
                "repeat_rule в человекочитаемом виде (например 'каждые 2 дня 14:00' или 'каждый вторник 12:00'). "
                "Если встречаются обе точки, сделай 2 объекта: для Центр и для Полет. "
                "Если текст про К-Экспро или ИП Вылегжанина, проставь supplier и category='Закупка'. "
                "Если из текста неясно дата/время — ставь пустые строки."
            )
            prompt = f"Текст: {text}"
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system", "content": sys},
                    {"role":"user", "content": prompt}
                ],
                temperature=0.2
            )
            raw = resp.choices[0].message.content.strip()
            # Ожидаем чистый JSON массив
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                for it in parsed:
                    it["user_id"] = fallback_user_id
                items = parsed
                used_ai = True
        except Exception as e:
            log.error(f"AI parse failed: {e}")

    # 2) Фоллбек-простой разбор, если AI недоступен/ошибся
    if not used_ai:
        # Простые эвристики:
        text_l = text.lower()
        direction = "Кофейня" if any(w in text_l for w in ["кофей", "к-экспро", "вылегжанин"]) else ("Табачка" if "табач" in text_l else "Личное")
        supplier = ""
        if "к-экспро" in text_l or "k-exp" in text_l:
            supplier = "К-Экспро"
        if "вылегжан" in text_l:
            supplier = "ИП Вылегжанина"

        # ТТ
        tts = []
        if "центр" in text_l: tts.append("Центр")
        if "полет" in text_l or "полёт" in text_l: tts.append("Полет")
        if "климов" in text_l: tts.append("Климово")
        if not tts and direction in ("Кофейня","Табачка"):
            tts = ["Центр"]

        # время
        m = re.search(r"(\d{1,2}:\d{2})", text)
        time_s = m.group(1) if m else ""

        # повтор
        repeat_rule = ""
        if "каждые 2 дня" in text_l or "каждый второй день" in text_l:
            repeat_rule = f"каждые 2 дня {time_s}".strip()

        # дата (если есть сегодня/завтра/конкретная)
        date_s = ""
        if "сегодня" in text_l:
            date_s = now_local().strftime("%d.%m.%Y")
        elif "завтра" in text_l:
            date_s = (now_local()+timedelta(days=1)).strftime("%d.%m.%Y")
        else:
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if m: date_s = m.group(1)

        base = {
            "kind": "repeat" if repeat_rule else "single",
            "date": date_s,
            "time": time_s,
            "direction": direction,
            "category": "Закупка" if supplier else "Общее",
            "tt": ",".join(tts),
            "text": text.strip(),
            "repeat_rule": repeat_rule,
            "supplier": supplier,
            "user_id": fallback_user_id
        }

        # если в тексте упомянуты обе точки — разложим
        if len(tts) > 1:
            for t in tts:
                x = dict(base)
                x["tt"] = t
                items.append(x)
        else:
            items.append(base)

    return items

# ========= ОБРАБОТКА ПОВТОРЯЮЩИХСЯ ЗАДАЧ КАЖДЫЙ ДЕНЬ =========
def expand_repeats_for_date(date_dt: datetime):
    """На указанную дату создать задачи из листа 'Повторяющиеся задачи' по их правилам."""
    date_s = date_dt.strftime("%d.%m.%Y")
    weekday_map = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    weekday_s = weekday_map[date_dt.weekday()]
    rep_rows = read_all(ws_repeating)

    # Уже существующие задачи на дату для защиты от дублей
    existing = {(r.get("User ID"), r.get("Дата"), r.get("Задача")) for r in ws_tasks.get_all_records()}

    for r in rep_rows:
        if (r.get("Активна") or "").strip().lower() not in ("1","да","true","y","on"):
            continue
        rule = (r.get("Правило") or "").strip().lower()
        direction = (r.get("Направление") or "").strip()
        category = (r.get("Категория") or "").strip()
        tt = (r.get("ТТ") or "").strip()
        text = (r.get("Задача") or "").strip()
        time_s = (r.get("Время") or "").strip()
        supplier = (r.get("Поставщик") or "").strip()
        user_id = str(r.get("User ID") or "").strip()

        should_create = False

        # Примеры правил: "каждые 2 дня 14:00", "каждый вторник 12:00", "по пн,ср"
        if rule.startswith("каждые "):
            # каждые N дней
            m = re.search(r"каждые\s+(\d+)\s+дн", rule)
            if m:
                n = int(m.group(1))
                # базовый старт — из "с момента занесения". Будем делить от известной эпохи:
                epoch = datetime(2025,1,1, tzinfo=LOCAL_TZ).date()
                delta = (date_dt.date() - epoch).days
                if delta % n == 0:
                    should_create = True
        elif rule.startswith("каждый "):
            # каждый вторник, каждый четверг 14:00
            for i,wd in enumerate(weekday_map):
                if wd in rule and wd == weekday_s:
                    should_create = True
                    break
        elif rule.startswith("по "):
            # по пн,ср
            days = [d.strip() for d in rule.replace("по","").split(",")]
            # сопоставим русские сокращения
            short = {"пн":"понедельник","вт":"вторник","ср":"среда","чт":"четверг","пт":"пятница","сб":"суббота","вс":"воскресенье"}
            expanded = [short.get(d, d) for d in days]
            if weekday_s in expanded:
                should_create = True

        if should_create:
            key = (user_id, date_s, text)
            if key not in existing:
                add_task_row(date_s, direction, category, tt, text, time_s, "", "повтор", "", user_id, "повтор")
                existing.add(key)

# ========= ФОРМАТИРОВАННЫЙ ВЫВОД =========
def format_tasks_grouped(tasks, header_date=None):
    """Группировка: Направление → Категория → ТТ → пункты"""
    if not tasks:
        return "Задач нет."

    # сортируем: направление, категория, ТТ, дедлайн
    def k(r):
        dl = r.get("Дедлайн") or ""
        try:
            dl_k = datetime.strptime(dl, "%H:%M").time()
        except:
            dl_k = datetime.min.time()
        return (r.get("Направление",""), r.get("Категория",""), r.get("ТТ",""), dl_k, r.get("Задача",""))

    tasks = sorted(tasks, key=k)

    out = []
    if header_date:
        # В заголовке показываем день недели слева от даты
        dt = datetime.strptime(header_date, "%d.%m.%Y")
        out.append(f"• {weekday_ru(dt)} — {header_date}\n")

    # группировка
    cur_dir = cur_cat = cur_tt = None
    for r in tasks:
        d = r.get("Направление","")
        c = r.get("Категория","")
        t = r.get("ТТ","")
        txt = r.get("Задача","")
        dl = r.get("Дедлайн","") or ""
        st = (r.get("Статус","") or "").lower()
        rep = (r.get("Тип","") or "").lower() == "повтор"
        icon = "✅" if st == "выполнено" else ("🔁" if rep else "⬜")

        if d != cur_dir:
            out.append(f"📂 <b>{d}</b>")
            cur_dir = d; cur_cat = cur_tt = None
        if c != cur_cat:
            out.append(f"  └ <b>{c}</b>")
            cur_cat = c; cur_tt = None
        if t != cur_tt:
            out.append(f"    └ <i>{t or '—'}</i>")
            cur_tt = t
        line = f"      {icon} {txt}"
        if dl: line += f"  <i>(до {dl})</i>"
        out.append(line)
    return "\n".join(out)

def get_tasks_for_date(user_id, date_str):
    rows = ws_tasks.get_all_records()
    return [r for r in rows if str(r.get("User ID")) == str(user_id) and r.get("Дата") == date_str]

def get_tasks_for_week(user_id, start_dt=None):
    if not start_dt:
        start_dt = now_local().date()
    days = [(start_dt + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    rows = ws_tasks.get_all_records()
    return [r for r in rows if str(r.get("User ID")) == str(user_id) and r.get("Дата") in days]

# ========= КЛАВИАТУРЫ =========
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня", "📆 Неделя", "🗓 Вся неделя")
    kb.row("➕ Добавить", "✅ Я сделал…", "🧠 Ассистент")
    return kb

def week_days_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    base = now_local().date()
    for i in range(7):
        d = base + timedelta(days=i)
        kb.add(f"{weekday_ru(datetime(d.year,d.month,d.day))} ({d.strftime('%d.%m.%Y')})")
    kb.add("⬅ Назад")
    return kb

# ========= СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ (простой FSM) =========
USER_STATE = {}         # user_id -> state
USER_BUFFER = {}        # user_id -> temp payload

def set_state(uid, state, data=None):
    USER_STATE[uid] = state
    if data is not None:
        USER_BUFFER[uid] = data

def clear_state(uid):
    USER_STATE.pop(uid, None)
    USER_BUFFER.pop(uid, None)

# ========= ХЕНДЛЕРЫ =========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам. Чем займёмся?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    uid = m.chat.id
    # на всякий: каждый раз перед выводом убеждаемся, что повторящиеся задачи расширены на сегодня
    expand_repeats_for_date(now_local())
    date_s = today_str()
    tasks = get_tasks_for_date(uid, date_s)
    text = f"📅 Задачи на {date_s}\n\n" + format_tasks_grouped(tasks, header_date=date_s)
    bot.send_message(uid, text, reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week_menu(m):
    bot.send_message(m.chat.id, "Выбери день:", reply_markup=week_days_menu())

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def handle_all_week(m):
    uid = m.chat.id
    # расширим повторяющиеся задачи на неделю вперёд (при первом открытии)
    for i in range(7):
        expand_repeats_for_date(now_local() + timedelta(days=i))
    tasks = get_tasks_for_week(uid)
    # сгруппируем по дням, с заголовками и отступами
    if not tasks:
        bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu())
        return

    by_day = {}
    for r in tasks:
        by_day.setdefault(r["Дата"], []).append(r)

    parts = []
    for d in sorted(by_day.keys(), key=lambda s: datetime.strptime(s, "%d.%m.%Y")):
        parts.append(format_tasks_grouped(by_day[d], header_date=d))
        parts.append("")  # пустая строка между днями
    bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())

@bot.message_handler(func=lambda msg: "(" in msg.text and ")" in msg.text)
def handle_specific_day(m):
    uid = m.chat.id
    try:
        date_s = m.text.split("(")[1].strip(")")
    except:
        bot.send_message(uid, "Не понял дату, попробуй ещё раз.", reply_markup=main_menu())
        return
    # расширим повторяющиеся на конкретный день на всякий
    dt = datetime.strptime(date_s, "%d.%m.%Y")
    expand_repeats_for_date(LOCAL_TZ.localize(dt))
    tasks = get_tasks_for_date(uid, date_s)
    text = f"📅 Задачи на {date_s}\n\n" + format_tasks_grouped(tasks, header_date=date_s)
    bot.send_message(uid, text, reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# --- Добавление задач (умный ввод) ---
@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    set_state(m.chat.id, "adding_wait_text", {})
    bot.send_message(m.chat.id, "Опиши задачу одним сообщением.\nНапример:\n<b>Заказать К-Экспро Центр и Полет каждые 2 дня в 14:00</b>")

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def handle_done_free(m):
    set_state(m.chat.id, "done_wait_text", {})
    bot.send_message(m.chat.id, "Напиши что сделал. Примеры:\n<b>я сделал заказы к-экспро центр</b>\n<b>я сделал все заказы к-экспро</b>")

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai_assistant(m):
    set_state(m.chat.id, "assistant_wait_text", {})
    bot.send_message(m.chat.id, "Сформулируй, что нужно: составить расписание, приоритизировать задачи, напомнить о дедлайнах и т.п.")

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "adding_wait_text")
def adding_free_text(m):
    uid = m.chat.id
    txt = m.text.strip()
    try:
        items = ai_parse_free_text(txt, uid)
        if not items:
            bot.send_message(uid, "Не получилось распознать. Попробуй иначе.", reply_markup=main_menu())
            clear_state(uid); return

        created = []
        repeats_added = 0
        for it in items:
            direction = it.get("direction") or "Личное"
            category  = it.get("category") or "Общее"
            text_val  = it.get("text") or "Задача"
            tt       = it.get("tt") or ""
            time_s   = it.get("time") or ""
            supplier = it.get("supplier") or ""
            kind     = it.get("kind") or "single"
            user_id  = it.get("user_id") or uid

            # если tt — список, добавим на каждую ТТ
            tt_list = [t.strip() for t in (tt.split(",") if isinstance(tt,str) else [tt]) if t.strip()] or [""]

            if kind == "repeat":
                rule = it.get("repeat_rule") or ""
                for t in tt_list:
                    ws_repeating.append_row([
                        rule, direction, category, t, text_val, time_s, supplier, str(user_id), "1"
                    ], value_input_option="USER_ENTERED")
                    repeats_added += 1
            else:
                d_str = it.get("date") or today_str()
                for t in tt_list:
                    add_task_row(d_str, direction, category, t, text_val, time_s, "", "разовая", "", user_id, supplier)
                    created.append((d_str, direction, category, t, text_val, time_s))

        msg_parts = []
        if created:
            msg_parts.append("✅ Добавил задачи:\n" + "\n".join([f"• {c[0]} [{c[1]}→{c[2]}→{c[3]}] {c[4]} (до {c[5] or '—'})" for c in created]))
        if repeats_added:
            msg_parts.append(f"🔁 Добавил повторяющихся правил: {repeats_added}")
        bot.send_message(uid, "\n\n".join(msg_parts) or "Готово", reply_markup=main_menu())
        log_event(uid, "add_free_text", txt)
    except Exception as e:
        log.error(f"Add parse error: {e}")
        bot.send_message(uid, "Произошла ошибка при разборе. Попробуй заново.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "done_wait_text")
def done_free_text(m):
    uid = m.chat.id
    txt = m.text.strip().lower()

    # очень простой парсинг
    sup = None
    if "к-экспро" in txt or "k-exp" in txt:
        sup = "К-Экспро"
    elif "вылегжан" in txt:
        sup = "ИП Вылегжанина"

    tt = None
    if "центр" in txt: tt = "центр"
    if "полет" in txt or "полёт" in txt: tt = "полет"
    if "климов" in txt: tt = "климово"

    match_all = "все" in txt or "всё" in txt

    try:
        changed = mark_done_by_text(uid, supplier=sup, tt=tt, match_all=match_all)
        if not changed:
            bot.send_message(uid, "Не нашёл подходящих задач на сегодня. Уточни формулировку.", reply_markup=main_menu())
            clear_state(uid); return

        # Если это поставщик с правилами — создадим приемку/следующий заказ
        if sup:
            direction = direction_for_supplier(sup) or "Кофейня"
            # определим ТТ-лист из измененных задач
            tt_list = list({ r.get("ТТ") for r in changed if r.get("ТТ") })
            created = plan_next_by_supplier_rule(uid, sup, direction, tt_list)
            extra = ""
            if created:
                extra = "\n\n🔮 Запланировал по правилам:\n" + "\n".join([f"• {tt}: приемка {d1.strftime('%d.%m')} → новый заказ {d2.strftime('%d.%m')}" for tt,d1,d2 in created])
            bot.send_message(uid, f"✅ Отметил выполненным: {len(changed)}.\n{extra}".strip(), reply_markup=main_menu())
        else:
            bot.send_message(uid, f"✅ Отметил выполненным: {len(changed)}.", reply_markup=main_menu())

        log_event(uid, "done_free_text", txt)
    except Exception as e:
        log.error(f"Done free error: {e}")
        bot.send_message(uid, "Ошибка при отметке. Попробуй ещё раз.", reply_markup=main_menu())
    finally:
        clear_state(uid)

@bot.message_handler(func=lambda msg: USER_STATE.get(msg.chat.id) == "assistant_wait_text")
def ai_answer(m):
    uid = m.chat.id
    query = m.text.strip()
    try:
        # соберём контекст задач на 7 дней для ассистента
        tasks = get_tasks_for_week(uid)
        brief = []
        for r in sorted(tasks, key=lambda x: (datetime.strptime(x["Дата"], "%d.%m.%Y"), x.get("Дедлайн",""), x.get("Задача",""))):
            brief.append(f"{r['Дата']} • {r['Направление']} / {r['Категория']} / {r['ТТ'] or '—'} — {r['Задача']} (до {r['Дедлайн'] or '—'}) [{r.get('Статус','')}]")
        context = "\n".join(brief)[:4000]

        if not OPENAI_API_KEY:
            # Фоллбек: простая эвристика
            bot.send_message(uid, "🧠 (Без GPT) Совет: начни с задач с ближайшим дедлайном и высокой важностью.", reply_markup=main_menu())
            clear_state(uid); return

        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys = (
            "Ты личный ассистент по задачам. На основе списка задач пользователя помоги спланировать, приоритизировать, "
            "напомнить о важных дедлайнах. Пиши кратко и по делу, на русском, с буллетами."
        )
        prompt = f"Запрос: {query}\n\nМои задачи на неделю:\n{context}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        answer = resp.choices[0].message.content.strip()
        bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())
        log_event(uid, "assistant_query", query)
    except Exception as e:
        log.error(f"AI error: {e}", exc_info=True)
        bot.send_message(uid, "Не удалось получить ответ ассистента.", reply_markup=main_menu())
    finally:
        clear_state(uid)

# ========= ПЛАНИРОВЩИК (ЕЖЕДНЕВНАЯ РАССЫЛКА) =========
def job_daily_digest():
    try:
        # На всякий — расширим повторяющиеся на сегодня
        expand_repeats_for_date(now_local())
        users = read_all(ws_users)
        today = today_str()
        for u in users:
            uid = str(u.get("Telegram ID") or "").strip()
            if not uid:
                continue
            tasks = get_tasks_for_date(uid, today)
            if not tasks:
                continue
            text = f"📅 План на {today}\n\n" + format_tasks_grouped(tasks, header_date=today)
            try:
                bot.send_message(uid, text)
            except Exception as e:
                log.error(f"Не смог отправить дайджест {uid}: {e}")
    except Exception as e:
        log.error(f"job_daily_digest error: {e}")

def scheduler_thread():
    # 09:00 по локальному TZ
    schedule.clear()
    schedule.every().day.at("09:00").do(job_daily_digest)
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

    # только webhook, НИКАКОГО polling — иначе 409
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)

    # Запускаем планировщик в фоне
    threading.Thread(target=scheduler_thread, daemon=True).start()

    # Flask-сервер для Telegram Webhook и здоровья
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
