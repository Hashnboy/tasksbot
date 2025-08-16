# -*- coding: utf-8 -*-
"""
TasksBot — Telegram-бот с PostgreSQL, webhook, GPT-помощником и автоматизацией закупок/поставок.

Ключевые фичи:
- PostgreSQL (SQLAlchemy ORM)
- Вебхук (Flask) — НИКАКОГО polling -> нет 409
- Задачи/подзадачи/поставщики/повторяемость
- Кнопки: меню по темам, пагинация, карточка задачи (done/приёмка/подзадача/дедлайн/напоминание/удаление)
- Автопланирование поставок (К-Экспро, ИП Вылегжанина и любые из листа поставщиков)
- Умный парсинг через GPT (если задан OPENAI_API_KEY) + фоллбек-эвристики
- Ежедневный дайджест в 08:00 (по TZ) + минута-в-минуту напоминания

ENV:
  TELEGRAM_TOKEN     — токен бота
  WEBHOOK_BASE       — базовый URL твоего сервера, напр. https://your-app.vkcloud.ru
  DATABASE_URL       — строка подключения к PostgreSQL, напр. postgresql+psycopg2://user:pass@host:5432/db
  OPENAI_API_KEY     — ключ OpenAI (опционально, для NLP/ассистента)
  TZ                 — таймзона, напр. Europe/Moscow (по умолчанию)
  PORT               — порт Flask (по умолчанию 10000)
"""

import os
import re
import hmac
import json
import pytz
import time
import math
import uuid
import hashlib
import logging
import schedule
import threading
from datetime import datetime, timedelta

from flask import Flask, request
from telebot import TeleBot, types

# ---- SQLAlchemy ----
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Date, Time, DateTime, Boolean, func, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# ---- OpenAI (опционально) ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        openai_client = None
else:
    openai_client = None

# ========= НАСТРОЙКИ ОКРУЖЕНИЯ =========
API_TOKEN   = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_BASE= os.getenv("WEBHOOK_BASE")
DB_URL      = os.getenv("DATABASE_URL")
TZ_NAME     = os.getenv("TZ", "Europe/Moscow")
PORT        = int(os.getenv("PORT", "10000"))

if not API_TOKEN or not WEBHOOK_BASE or not DB_URL:
    raise RuntimeError("Нужны ENV: TELEGRAM_TOKEN, WEBHOOK_BASE, DATABASE_URL")

WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}"
LOCAL_TZ    = pytz.timezone(TZ_NAME)

# ========= ЛОГИ =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# ========= БОТ =========
bot = TeleBot(API_TOKEN, parse_mode="HTML")

# ========= БАЗА ДАННЫХ =========
Base = declarative_base()
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# ========= МОДЕЛИ =========
class User(Base):
    __tablename__ = "users"
    id          = Column(Integer, primary_key=True)            # tg chat id
    name        = Column(String(255), default="")
    created_at  = Column(DateTime, server_default=func.now())

class Task(Base):
    __tablename__ = "tasks"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, index=True, nullable=False)
    date         = Column(Date, index=True, nullable=False)
    category     = Column(String(120), default="Личное", index=True)
    subcategory  = Column(String(120), default="", index=True)  # ТТ и т.п.
    text         = Column(Text, nullable=False)
    deadline     = Column(Time, nullable=True)
    status       = Column(String(40), default="")               # "", "выполнено"
    repeat_rule  = Column(String(255), default="")              # свободный вид (каждые 2 дня, вторник 12:00 и т.п.)
    source       = Column(String(255), default="")              # supplier/auto/remind/subtask:...
    is_repeating = Column(Boolean, default=False)               # пометка что порождено по шаблону
    created_at   = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_tasks_uid_date", "user_id", "date"),
    )

class SubTask(Base):
    __tablename__ = "subtasks"
    id          = Column(Integer, primary_key=True)
    task_id     = Column(Integer, index=True, nullable=False)
    text        = Column(Text, nullable=False)
    status      = Column(String(40), default="")
    created_at  = Column(DateTime, server_default=func.now())

class Supplier(Base):
    __tablename__ = "suppliers"
    id          = Column(Integer, primary_key=True)
    name        = Column(String(255), unique=True, nullable=False)  # "к-экспро"
    rule        = Column(String(255), default="")                   # "каждые 2 дня", "shelf 72h"
    order_deadline = Column(String(10), default="14:00")
    emoji       = Column(String(8), default="📦")
    delivery_offset_days = Column(Integer, default=1)
    shelf_days  = Column(Integer, default=0)                        # например 3 для 72ч
    start_cycle = Column(Date, nullable=True)                       # опционально
    auto        = Column(Boolean, default=True)
    active      = Column(Boolean, default=True)
    created_at  = Column(DateTime, server_default=func.now())

class Reminder(Base):
    __tablename__ = "reminders"
    id          = Column(Integer, primary_key=True)
    user_id     = Column(Integer, index=True)
    task_id     = Column(Integer, index=True)
    date        = Column(Date, nullable=False)
    time        = Column(Time, nullable=False)
    fired       = Column(Boolean, default=False)
    created_at  = Column(DateTime, server_default=func.now())

def init_db():
    Base.metadata.create_all(bind=engine)

# ========= УТИЛИТЫ =========
PAGE_SIZE = 8

def now_local():
    return datetime.now(LOCAL_TZ)

def dstr(dt):
    return dt.strftime("%d.%m.%Y")

def parse_date_str(s: str):
    return datetime.strptime(s, "%d.%m.%Y").date()

def parse_time_str(s: str):
    return datetime.strptime(s, "%H:%M").time()

def weekday_ru(dt):
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[dt.weekday()]

def sha_task_id(task: Task):
    key = f"{task.user_id}|{task.date.isoformat()}|{task.category}|{task.subcategory}|{task.text}|{task.deadline or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

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

def ensure_user(sess, uid, name=""):
    u = sess.query(User).filter_by(id=uid).first()
    if not u:
        u = User(id=uid, name=name or "")
        sess.add(u)
        sess.commit()
    return u

# ========= GPT разбор свободного текста =========
def ai_parse_to_items(text, fallback_uid):
    """
    Возвращает список объектов:
    {
      "date": "ДД.ММ.ГГГГ"|"",
      "time": "ЧЧ:ММ"|"",
      "category": "...",
      "subcategory": "...",
      "task": "...",
      "repeat": "",  # человекочитаемое
      "supplier": "",  # распознанный поставщик
      "user_id": fallback_uid
    }
    """
    # Попытка через OpenAI
    if openai_client:
        try:
            sys = (
                "Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов без текста. "
                "Схема: {date:'ДД.ММ.ГГГГ'|'' , time:'ЧЧ:ММ'|'' , category:'Кофейня|Табачка|Личное|WB', "
                "subcategory:'Центр|Полет|Климово|..' , task:'...', repeat:'', supplier:''}."
            )
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys}, {"role":"user","content":text}],
                temperature=0.2
            )
            raw = resp.choices[0].message.content.strip()
            data = json.loads(raw)
            if isinstance(data, dict): data = [data]
            out = []
            for it in data:
                out.append({
                    "date": it.get("date") or "",
                    "time": it.get("time") or "",
                    "category": it.get("category") or "Личное",
                    "subcategory": it.get("subcategory") or "",
                    "task": it.get("task") or "",
                    "repeat": it.get("repeat") or "",
                    "supplier": it.get("supplier") or "",
                    "user_id": fallback_uid
                })
            return out
        except Exception as e:
            log.error("AI parse failed: %s", e)

    # Фоллбек-эвристика
    txt = text.strip()
    tl  = txt.lower()
    cat = "Кофейня" if any(x in tl for x in ["кофейн","к-экспро","вылегжан"]) else ("Табачка" if "табач" in tl else ("WB" if "wb" in tl else "Личное"))
    sub = "Центр" if "центр" in tl else ("Полет" if ("полет" in tl or "полёт" in tl) else ("Климово" if "климов" in tl else ""))
    mtime = re.search(r"(\d{1,2}:\d{2})", txt)
    time_s = mtime.group(1) if mtime else ""
    if   "сегодня" in tl: date_s = dstr(now_local().date())
    elif "завтра"  in tl: date_s = dstr(now_local().date()+timedelta(days=1))
    else:
        mdate = re.search(r"(\d{2}\.\d{2}\.\d{4})", txt)
        date_s = mdate.group(1) if mdate else ""

    supplier = ""
    if "к-экспро" in tl or "k-exp" in tl or "к экспро" in tl: supplier = "К-Экспро"
    if "вылегжан" in tl: supplier = "ИП Вылегжанина"

    return [{
        "date": date_s, "time": time_s, "category": cat, "subcategory": sub,
        "task": txt, "repeat":"", "supplier": supplier, "user_id": fallback_uid
    }]

# ========= ПРАВИЛА ПОСТАВЩИКОВ =========
# Базовые (можно переопределять данными из таблицы suppliers)
BASE_SUP_RULES = {
    "к-экспро": {
        "kind": "cycle_every_n_days",
        "n_days": 2,
        "delivery_offset": 1,
        "deadline": "14:00",
        "emoji": "📦"
    },
    "ип вылегжанина": {
        "kind": "delivery_shelf_then_order",
        "delivery_offset": 1,
        "shelf_days": 3,
        "deadline": "14:00",
        "emoji": "🥘"
    }
}

def normalize_supplier_name(name: str) -> str:
    return (name or "").strip().lower()

def load_supplier_rule(sess, supplier_name: str):
    # 1) из БД
    s = sess.query(Supplier).filter(func.lower(Supplier.name)==normalize_supplier_name(supplier_name)).first()
    if s and s.active:
        # попытка распознать rule
        rule_l = (s.rule or "").strip().lower()
        if "каждые" in rule_l:
            num = 2
            m = re.findall(r"\d+", rule_l)
            if m: num = int(m[0])
            return {
                "kind": "cycle_every_n_days",
                "n_days": num,
                "delivery_offset": s.delivery_offset_days or 1,
                "deadline": s.order_deadline or "14:00",
                "emoji": s.emoji or "📦"
            }
        if "shelf" in rule_l or "72" in rule_l or "хранен" in rule_l:
            shelf = s.shelf_days or 3
            return {
                "kind": "delivery_shelf_then_order",
                "delivery_offset": s.delivery_offset_days or 1,
                "shelf_days": shelf,
                "deadline": s.order_deadline or "14:00",
                "emoji": s.emoji or "🥘"
            }
    # 2) базовая
    base = BASE_SUP_RULES.get(normalize_supplier_name(supplier_name))
    if base:
        return base
    return None

def plan_next_for_supplier(sess, user_id: int, supplier_name: str, category: str, subcategory: str):
    rule = load_supplier_rule(sess, supplier_name)
    if not rule:
        return []
    today = now_local().date()
    created = []
    if rule["kind"] == "cycle_every_n_days":
        delivery_day = today + timedelta(days=rule["delivery_offset"])
        next_order   = today + timedelta(days=rule["n_days"])
        # Приемка
        sess.add(Task(
            user_id=user_id,
            date=delivery_day,
            category=category,
            subcategory=subcategory,
            text=f"{rule['emoji']} Принять поставку {supplier_name} ({subcategory or '—'})",
            deadline=parse_time_str("10:00"),
            status="",
            repeat_rule="",
            source=f"auto:delivery:{supplier_name}",
            is_repeating=False
        ))
        # Следующий заказ
        sess.add(Task(
            user_id=user_id,
            date=next_order,
            category=category,
            subcategory=subcategory,
            text=f"{rule['emoji']} Заказать {supplier_name} ({subcategory or '—'})",
            deadline=parse_time_str(rule["deadline"]),
            status="",
            repeat_rule="",
            source=f"auto:order:{supplier_name}",
            is_repeating=False
        ))
        sess.commit()
        created = [("delivery", delivery_day), ("order", next_order)]
    elif rule["kind"] == "delivery_shelf_then_order":
        delivery_day = today + timedelta(days=rule["delivery_offset"])
        next_order   = delivery_day + timedelta(days=max(1, rule.get("shelf_days", 3)-1))
        sess.add(Task(
            user_id=user_id,
            date=delivery_day,
            category=category,
            subcategory=subcategory,
            text=f"{rule['emoji']} Принять поставку {supplier_name} ({subcategory or '—'})",
            deadline=parse_time_str("11:00"),
            status="",
            repeat_rule="",
            source=f"auto:delivery:{supplier_name}",
            is_repeating=False
        ))
        sess.add(Task(
            user_id=user_id,
            date=next_order,
            category=category,
            subcategory=subcategory,
            text=f"{rule['emoji']} Заказать {supplier_name} ({subcategory or '—'})",
            deadline=parse_time_str(rule["deadline"]),
            status="",
            repeat_rule="",
            source=f"auto:order:{supplier_name}",
            is_repeating=False
        ))
        sess.commit()
        created = [("delivery", delivery_day), ("order", next_order)]
    return created

# ========= ДОСТУП К ДАННЫМ =========
def add_task(sess, *, user_id:int, date:datetime.date, category:str, subcategory:str, text:str, deadline=None, repeat_rule:str="", source:str="", is_repeating:bool=False):
    t = Task(
        user_id=user_id,
        date=date,
        category=category or "Личное",
        subcategory=subcategory or "",
        text=text.strip(),
        deadline=deadline,
        status="",
        repeat_rule=repeat_rule.strip(),
        source=source.strip(),
        is_repeating=is_repeating
    )
    sess.add(t)
    sess.commit()
    return t

def get_tasks_for_date(sess, user_id:int, date:datetime.date):
    return (sess.query(Task)
            .filter(Task.user_id==user_id, Task.date==date)
            .order_by(Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()

def get_tasks_for_week(sess, user_id:int, base_date:datetime.date):
    days = [base_date + timedelta(days=i) for i in range(7)]
    rows = (sess.query(Task)
            .filter(Task.user_id==user_id, Task.date.in_(days))
            .order_by(Task.date.asc(), Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()
    return rows

def complete_task(sess, task_id:int, user_id:int):
    t = sess.query(Task).filter(Task.id==task_id, Task.user_id==user_id).first()
    if not t: return None
    t.status = "выполнено"
    sess.commit()
    return t

def delete_task(sess, task_id:int, user_id:int):
    t = sess.query(Task).filter(Task.id==task_id, Task.user_id==user_id).first()
    if not t: return False
    sess.delete(t)
    sess.commit()
    return True

def create_reminder(sess, task_id:int, user_id:int, date_s:str, time_s:str):
    date = parse_date_str(date_s)
    tm   = parse_time_str(time_s)
    r = Reminder(user_id=user_id, task_id=task_id, date=date, time=tm, fired=False)
    sess.add(r)
    sess.commit()
    return r

# ========= ПОВТОРЯЮЩИЕСЯ ЗАДАЧИ (по simple правилам) =========
def expand_repeats_for_date(sess, user_id:int, date:datetime.date):
    """
    Пример supported правил в Task.repeat_rule:
      - "каждые 2 дня"
      - "каждый вторник 12:00"
      - "по пн,ср"
    Правило хранится на самой задаче-шаблоне (is_repeating=True), и мы на их основе создаем инстансы на дату.
    """
    # найдём шаблоны (is_repeating=True) для этого юзера
    templates = (sess.query(Task)
                 .filter(Task.user_id==user_id, Task.is_repeating==True)
                 .all())

    existing = {(t.text, t.category, t.subcategory) for t in get_tasks_for_date(sess, user_id, date)}

    weekday_map = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    weekday_s = weekday_map[date.weekday()]

    for tp in templates:
        rule = (tp.repeat_rule or "").strip().lower()
        if not rule: continue
        should = False
        when_time = tp.deadline

        if rule.startswith("каждые "):
            m = re.search(r"каждые\s+(\d+)\s+дн", rule)
            if m:
                n = int(m.group(1))
                # считаем от стартовой точки (либо created_at, либо 2025-01-01)
                epoch = tp.created_at.date() if tp.created_at else datetime(2025,1,1).date()
                if ((date - epoch).days % n) == 0:
                    should = True

        elif rule.startswith("каждый "):
            # "каждый вторник 12:00"
            for i,wd in enumerate(weekday_map):
                if wd in rule and wd == weekday_s:
                    should = True
                    m = re.search(r"(\d{1,2}:\d{2})", rule)
                    if m: when_time = parse_time_str(m.group(1))
                    break

        elif rule.startswith("по "):
            # "по пн,ср"
            short = {"пн":"понедельник","вт":"вторник","ср":"среда",
                     "чт":"четверг","пт":"пятница","сб":"суббота","вс":"воскресенье"}
            parts = [p.strip() for p in rule.replace("по","").split(",") if p.strip()]
            expanded = [short.get(p, p) for p in parts]
            if weekday_s in expanded:
                should = True

        if should:
            key = (tp.text, tp.category, tp.subcategory)
            if key not in existing:
                add_task(sess,
                         user_id=user_id, date=date,
                         category=tp.category, subcategory=tp.subcategory,
                         text=tp.text, deadline=when_time,
                         repeat_rule="", source="repeat-instance", is_repeating=False)
                existing.add(key)

# ========= ФОРМАТИРОВАНИЕ =========
def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
    out = []
    if header_date:
        dt = parse_date_str(header_date)
        out.append(f"• {weekday_ru(dt)} — {header_date}\n")
    cur_cat = cur_sub = None
    for t in sorted(tasks, key=lambda x: (x.category or "", x.subcategory or "", x.deadline or parse_time_str("00:00"), x.text)):
        icon = "✅" if t.status=="выполнено" else ("🔁" if t.is_repeating else "⬜")
        if t.category != cur_cat:
            out.append(f"📂 <b>{t.category or '—'}</b>"); cur_cat = t.category; cur_sub = None
        if t.subcategory != cur_sub:
            out.append(f"  └ <b>{t.subcategory or '—'}</b>"); cur_sub = t.subcategory
        line = f"    └ {icon} {t.text}"
        if t.deadline: line += f"  <i>(до {t.deadline.strftime('%H:%M')})</i>"
        out.append(line)
    return "\n".join(out)

def short_task_line(t: Task, i=None):
    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
    p  = f"{i}. " if i is not None else ""
    return f"{p}{t.category}/{t.subcategory}: {t.text[:40]}… (до {dl})"

def paginate_buttons(items, page, total_pages, action_prefix):
    kb = types.InlineKeyboardMarkup()
    for label, task_id in items:
        kb.add(types.InlineKeyboardButton(label, callback_data=mk_cb(action_prefix, id=task_id)))
    nav = []
    if page>1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa=action_prefix)))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page<total_pages: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa=action_prefix)))
    if nav: kb.row(*nav)
    return kb

# ========= КЛАВИАТУРЫ =========
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя","🗓 Вся неделя")
    kb.row("➕ Добавить","🔎 Найти","✅ Я сделал…")
    kb.row("🚚 Поставки","🧠 Ассистент","⚙️ Настройки")
    return kb

def supplies_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📦 Заказы сегодня","🆕 Добавить поставщика")
    kb.row("⬅ Назад")
    return kb

# ========= СОСТОЯНИЯ =========
STATE = {}
BUF   = {}
def set_state(uid, s, data=None):
    STATE[uid] = s
    if data is not None: BUF[uid] = data
def get_state(uid): return STATE.get(uid)
def get_buf(uid): return BUF.get(uid, {})
def clear_state(uid):
    STATE.pop(uid, None)
    BUF.pop(uid, None)

# ========= ХЕНДЛЕРЫ =========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    sess = SessionLocal()
    try:
        ensure_user(sess, m.chat.id, name=m.from_user.full_name if m.from_user else "")
    finally:
        sess.close()
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам. Что делаем?", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def handle_today(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        ensure_user(sess, uid)
        # ДО каждого показа расширяем повторяемость на сегодня
        expand_repeats_for_date(sess, uid, now_local().date())
        rows = get_tasks_for_date(sess, uid, now_local().date())
        if not rows:
            bot.send_message(uid, f"📅 Задачи на {dstr(now_local().date())}\n\nЗадач нет.", reply_markup=main_menu()); return
        # Список + пагинация для открытия карточек
        items = [(short_task_line(t), t.id) for t in rows]
        page = 1
        total = (len(items)+PAGE_SIZE-1)//PAGE_SIZE
        slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
        kb = paginate_buttons(slice_items, page, total, "open")
        header = f"📅 Задачи на {dstr(now_local().date())}\n\n{format_grouped(rows, header_date=dstr(now_local().date()))}\n\nОткрой карточку:"
        bot.send_message(uid, header, reply_markup=main_menu(), reply_markup_inline=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def handle_week(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        ensure_user(sess, uid)
        # расширим шаблоны на 7 дней вперёд
        for i in range(7):
            expand_repeats_for_date(sess, uid, now_local().date()+timedelta(days=i))
        rows = get_tasks_for_week(sess, uid, now_local().date())
        if not rows:
            bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu()); return
        # группируем по датам
        by_day = {}
        for t in rows:
            by_day.setdefault(dstr(t.date), []).append(t)
        parts = []
        for d in sorted(by_day.keys(), key=lambda s: parse_date_str(s)):
            parts.append(format_grouped(by_day[d], header_date=d)); parts.append("")
        bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🗓 Вся неделя")
def handle_all_week(m):
    # дубль команды выше — оставим как есть для привычки
    handle_week(m)

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def handle_add(m):
    set_state(m.chat.id, "adding_text")
    bot.send_message(m.chat.id, "Опиши задачу одним сообщением (я распаршу дату/время/категорию/ТТ).")

@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def handle_search(m):
    set_state(m.chat.id, "search_text")
    bot.send_message(m.chat.id, "Что ищем? Введи часть текста/категории/подкатегории/даты (ДД.ММ.ГГГГ).")

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def handle_done_free(m):
    set_state(m.chat.id, "done_text")
    bot.send_message(m.chat.id, "Напиши что сделал. Примеры:\n<b>сделал заказы к-экспро центр</b>\n<b>сделал все заказы вылегжанина</b>")

@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставки")
def handle_supplies(m):
    bot.send_message(m.chat.id, "Меню поставок:", reply_markup=supplies_menu())

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def handle_today_orders(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        rows = get_tasks_for_date(sess, uid, now_local().date())
        orders = [t for t in rows if "заказ" in t.text.lower() or "заказать" in t.text.lower()]
        if not orders:
            bot.send_message(uid, "Сегодня заказов нет.", reply_markup=supplies_menu()); return
        kb = types.InlineKeyboardMarkup()
        for i,t in enumerate(orders, start=1):
            kb.add(types.InlineKeyboardButton(short_task_line(t, i), callback_data=mk_cb("open", id=t.id)))
        bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def handle_add_supplier(m):
    set_state(m.chat.id, "add_supplier")
    bot.send_message(m.chat.id, "Формат:\n<b>Название; правило; дедлайн(опц); emoji(опц); delivery_offset(опц); shelf_days(опц); auto(1/0); active(1/0)</b>\n"
                                "Примеры:\nК-Экспро; каждые 2 дня; 14:00; 📦; 1; 0; 1; 1\n"
                                "ИП Вылегжанина; shelf 72h; 14:00; 🥘; 1; 3; 1; 1")

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def handle_ai(m):
    set_state(m.chat.id, "assistant_text")
    bot.send_message(m.chat.id, "Что нужно? (спланировать день, выделить приоритеты, составить расписание и т.д.)")

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Настройки")
def handle_settings(m):
    bot.send_message(m.chat.id, f"Часовой пояс: <b>{TZ_NAME}</b>\nЕжедневный дайджест: <b>08:00</b>", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Назад")
def handle_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu())

# ========= ТЕКСТОВЫЕ СОСТОЯНИЯ =========
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "adding_text")
def adding_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        ensure_user(sess, uid)
        items = ai_parse_to_items(m.text.strip(), uid)
        created = 0
        for it in items:
            date = parse_date_str(it["date"]) if it["date"] else now_local().date()
            tm   = parse_time_str(it["time"]) if it["time"] else None
            t = add_task(sess,
                         user_id=uid, date=date,
                         category=it["category"], subcategory=it["subcategory"],
                         text=it["task"], deadline=tm,
                         repeat_rule=it["repeat"], source=it["supplier"], is_repeating=bool(it["repeat"]))
            created += 1
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=main_menu())
    except Exception as e:
        log.error("adding_text error: %s", e)
        bot.send_message(m.chat.id, "Не смог добавить. Попробуй иначе.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "search_text")
def search_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        q = m.text.strip().lower()
        rows = (sess.query(Task)
                .filter(Task.user_id==uid)
                .order_by(Task.date.desc())
                ).all()
        found = []
        for t in rows:
            hay = " ".join([
                dstr(t.date), t.category or "", t.subcategory or "",
                t.text or "", t.status or "", t.repeat_rule or "", t.source or ""
            ]).lower()
            if q in hay:
                found.append((short_task_line(t), t.id))
        if not found:
            bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); clear_state(uid); return
        page = 1
        total = (len(found)+PAGE_SIZE-1)//PAGE_SIZE
        slice_items = found[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
        kb = paginate_buttons(slice_items, page, total, "open")
        bot.send_message(uid, "Найденные задачи:", reply_markup=kb)
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "done_text")
def done_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        txt = m.text.strip().lower()
        supplier = ""
        if "к-экспро" in txt or "k-exp" in txt or "к экспро" in txt: supplier = "К-Экспро"
        if "вылегжан" in txt: supplier = "ИП Вылегжанина"

        date = now_local().date()
        rows = get_tasks_for_date(sess, uid, date)
        changed = 0
        last = None
        for t in rows:
            if t.status == "выполнено": continue
            low = (t.text or "").lower()
            if supplier and normalize_supplier_name(supplier) not in normalize_supplier_name(low):
                continue
            if not supplier and not any(w in low for w in ["заказ","сделал","закуп"]):
                continue
            t.status = "выполнено"
            last = t
            changed += 1
        sess.commit()
        msg = f"✅ Отмечено выполненным: {changed}."
        if changed and supplier and last:
            created = plan_next_for_supplier(sess, uid, supplier, last.category, last.subcategory)
            if created:
                more = ", ".join([f"{'приемка' if k=='delivery' else 'заказ'} {dstr(v)}" for k,v in created])
                msg += f"\n🔮 Запланировано: {more}"
        bot.send_message(uid, msg, reply_markup=main_menu())
    except Exception as e:
        log.error("done_text error: %s", e)
        bot.send_message(m.chat.id, "Не получилось отметить. Попробуй иначе.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "assistant_text")
def assistant_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        if not openai_client:
            bot.send_message(uid, "🧠 Совет: начни с задач с ближайшим дедлайном, потом крупные разбей на 2–3 подзадачи.", reply_markup=main_menu())
            clear_state(uid); return
        rows = get_tasks_for_week(sess, uid, now_local().date())
        brief = []
        for t in rows:
            dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
            brief.append(f"{dstr(t.date)} • {t.category}/{t.subcategory or '—'} — {t.text} (до {dl}) [{t.status or ''}]")
        prompt = f"Запрос: {m.text.strip()}\n\nМои задачи (7 дней):\n" + "\n".join(brief[:200])
        sys = "Ты личный ассистент по задачам. На русском, кратко, по делу, буллетами."
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.3
        )
        answer = resp.choices[0].message.content.strip()
        bot.send_message(uid, f"🧠 {answer}", reply_markup=main_menu())
    except Exception as e:
        log.error("assistant error: %s", e)
        bot.send_message(m.chat.id, "Не смог получить ответ ассистента.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_supplier")
def add_supplier_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        parts = [p.strip() for p in m.text.split(";")]
        name = parts[0]
        rule = parts[1] if len(parts)>1 else ""
        deadline = parts[2] if len(parts)>2 else "14:00"
        emoji = parts[3] if len(parts)>3 else "📦"
        doff  = int(parts[4]) if len(parts)>4 and parts[4].isdigit() else 1
        shelf = int(parts[5]) if len(parts)>5 and parts[5].isdigit() else 0
        auto  = (parts[6] == "1") if len(parts)>6 else True
        act   = (parts[7] == "1") if len(parts)>7 else True

        s = sess.query(Supplier).filter(func.lower(Supplier.name)==normalize_supplier_name(name)).first()
        if not s:
            s = Supplier(name=name, rule=rule, order_deadline=deadline, emoji=emoji,
                         delivery_offset_days=doff, shelf_days=shelf, auto=auto, active=act)
            sess.add(s)
        else:
            s.rule=rule; s.order_deadline=deadline; s.emoji=emoji
            s.delivery_offset_days=doff; s.shelf_days=shelf; s.auto=auto; s.active=act
        sess.commit()
        bot.send_message(uid, f"✅ Поставщик «{name}» сохранён.", reply_markup=supplies_menu())
    except Exception as e:
        log.error("add_supplier error: %s", e)
        bot.send_message(m.chat.id, "Не получилось сохранить поставщика.", reply_markup=supplies_menu())
    finally:
        clear_state(m.chat.id); sess.close()

# ========= INLINE: карточка задачи и действия =========
def render_task_card(sess, task_id:int, uid:int):
    t = sess.query(Task).filter(Task.id==task_id, Task.user_id==uid).first()
    if not t:
        return "Задача не найдена.", None
    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
    text = (
        f"<b>{t.text}</b>\n"
        f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
        f"📁 {t.category} / {t.subcategory or '—'}\n"
        f"⏰ Дедлайн: {dl}\n"
        f"📝 Статус: {t.status or '—'}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=task_id)))
    if ("заказ" in (t.text or "").lower()) or ("закуп" in (t.text or "").lower()):
        kb.add(types.InlineKeyboardButton("🚚 Принять поставку", callback_data=mk_cb("accept_delivery", id=task_id)))
    kb.add(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("add_sub", id=task_id)))
    kb.add(types.InlineKeyboardButton("✏️ Дедлайн", callback_data=mk_cb("set_deadline", id=task_id)))
    kb.add(types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("remind", id=task_id)))
    kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=mk_cb("delete", id=task_id)))
    return text, kb

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(c):
    data = parse_cb(c.data) if c.data and c.data!="noop" else None
    uid  = c.message.chat.id
    if not data:
        bot.answer_callback_query(c.id); return
    a = data.get("a")

    sess = SessionLocal()
    try:
        if a == "page":
            page = int(data.get("p", 1))
            pa   = data.get("pa")
            rows = get_tasks_for_date(sess, uid, now_local().date())
            items= [(short_task_line(t), t.id) for t in rows]
            total= (len(items)+PAGE_SIZE-1)//PAGE_SIZE
            page = max(1, min(page, total))
            slice_items = items[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
            kb = paginate_buttons(slice_items, page, total, "open")
            try:
                bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=kb)
            except Exception:
                pass
            bot.answer_callback_query(c.id); return

        if a == "open":
            tid = int(data.get("id"))
            text, kb = render_task_card(sess, tid, uid)
            bot.answer_callback_query(c.id)
            bot.send_message(uid, text, reply_markup=kb)
            return

        if a == "done":
            tid = int(data.get("id"))
            t = complete_task(sess, tid, uid)
            if not t:
                bot.answer_callback_query(c.id, "Не удалось", show_alert=True); return
            # автопланирование по supplier
            sup = ""
            tl = (t.text or "").lower()
            if "к-экспро" in tl or "k-exp" in tl or "к экспро" in tl: sup="К-Экспро"
            if "вылегжан" in tl: sup="ИП Вылегжанина"
            msg = "✅ Готово."
            if sup:
                created = plan_next_for_supplier(sess, uid, sup, t.category, t.subcategory)
                if created:
                    msg += " Запланирована приемка/следующий заказ."
            bot.answer_callback_query(c.id, msg, show_alert=True)
            text, kb = render_task_card(sess, tid, uid)
            if kb: bot.edit_message_text(text, uid, c.message.message_id, reply_markup=kb)
            else:  bot.edit_message_text(text, uid, c.message.message_id)
            return

        if a == "accept_delivery":
            tid = int(data.get("id"))
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
            tid = int(data.get("id"))
            set_state(uid, "pick_delivery_date", {"task_id": tid})
            bot.answer_callback_query(c.id)
            bot.send_message(uid, "Введи дату в формате ДД.ММ.ГГГГ:")
            return

        if a == "accept_delivery_date":
            tid = int(data.get("id"))
            when= data.get("d")
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Задача не найдена", show_alert=True); return
            if when=="today": d = now_local().date()
            else:             d = now_local().date()+timedelta(days=1)
            sup = "Поставка"
            if "к-экспро" in (t.text or "").lower(): sup = "К-Экспро"
            if "вылегжан" in (t.text or "").lower(): sup = "ИП Вылегжанина"
            add_task(sess, user_id=uid, date=d, category=t.category, subcategory=t.subcategory,
                     text=f"🚚 Принять поставку {sup} ({t.subcategory or '—'})",
                     deadline=parse_time_str("10:00"))
            bot.answer_callback_query(c.id, f"Создано на {dstr(d)}", show_alert=True)
            return

        if a == "add_sub":
            tid = int(data.get("id"))
            set_state(uid, "add_sub_text", {"task_id": tid})
            bot.answer_callback_query(c.id)
            bot.send_message(uid, "Введи текст подзадачи:")
            return

        if a == "set_deadline":
            tid = int(data.get("id"))
            set_state(uid, "set_deadline", {"task_id": tid})
            bot.answer_callback_query(c.id)
            bot.send_message(uid, "Новый дедлайн (ЧЧ:ММ):")
            return

        if a == "remind":
            tid = int(data.get("id"))
            set_state(uid, "set_reminder", {"task_id": tid})
            bot.answer_callback_query(c.id)
            bot.send_message(uid, "Когда напомнить? Дата и время: ДД.ММ.ГГГГ ЧЧ:ММ")
            return

        if a == "delete":
            tid = int(data.get("id"))
            ok = delete_task(sess, tid, uid)
            bot.answer_callback_query(c.id, "Удалено" if ok else "Не удалось", show_alert=True)
            try:
                bot.delete_message(uid, c.message.message_id)
            except Exception:
                pass
            return

    finally:
        sess.close()

# ========= ТЕКСТ: подзадача / дедлайн / напоминание / ручная дата приёмки =========
@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "add_sub_text")
def add_sub_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        data= get_buf(uid)
        tid = int(data.get("task_id"))
        txt = m.text.strip()
        parent = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not parent:
            bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
        s = SubTask(task_id=tid, text=txt, status="")
        sess.add(s); sess.commit()
        bot.send_message(uid, "Подзадача добавлена.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_deadline")
def set_deadline_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        data= get_buf(uid)
        tid = int(data.get("task_id"))
        tm  = m.text.strip()
        try:
            t = parse_time_str(tm)
        except Exception:
            bot.send_message(uid, "Нужен формат ЧЧ:ММ.", reply_markup=main_menu()); clear_state(uid); return
        task = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not task:
            bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
        task.deadline = t; sess.commit()
        bot.send_message(uid, "Дедлайн обновлён.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "set_reminder")
def set_reminder_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        data= get_buf(uid)
        tid = int(data.get("task_id"))
        raw = m.text.strip()
        # ожидаем "ДД.ММ.ГГГГ ЧЧ:ММ"
        parts = raw.split()
        if len(parts) != 2:
            bot.send_message(uid, "Формат: ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=main_menu()); clear_state(uid); return
        ds, ts = parts
        try:
            r = create_reminder(sess, tid, uid, ds, ts)
            bot.send_message(uid, f"⏰ Напоминание на {ds} {ts} установлено.", reply_markup=main_menu())
        except Exception:
            bot.send_message(uid, "Не смог установить напоминание. Проверь формат.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

@bot.message_handler(func=lambda msg: get_state(msg.chat.id) == "pick_delivery_date")
def pick_delivery_date(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        data= get_buf(uid)
        tid = int(data.get("task_id"))
        ds  = m.text.strip()
        try:
            d = parse_date_str(ds)
        except Exception:
            bot.send_message(uid, "Дата некорректна. Нужен формат ДД.ММ.ГГГГ.", reply_markup=main_menu()); clear_state(uid); return
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t:
            bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); clear_state(uid); return
        sup = "Поставка"
        if "к-экспро" in (t.text or "").lower(): sup = "К-Экспро"
        if "вылегжан" in (t.text or "").lower(): sup = "ИП Вылегжанина"
        add_task(sess, user_id=uid, date=d, category=t.category, subcategory=t.subcategory,
                 text=f"🚚 Принять поставку {sup} ({t.subcategory or '—'})", deadline=parse_time_str("10:00"))
        bot.send_message(uid, f"Создано на {ds}.", reply_markup=main_menu())
    finally:
        clear_state(m.chat.id); sess.close()

# ========= ПЛАНИРОВЩИКИ =========
def job_daily_digest():
    sess = SessionLocal()
    try:
        today = now_local().date()
        users = sess.query(User).all()
        for u in users:
            # расширяем повторяемость в момент рассылки
            expand_repeats_for_date(sess, u.id, today)
            tasks = get_tasks_for_date(sess, u.id, today)
            if not tasks: continue
            text = f"📅 План на {dstr(today)}\n\n" + format_grouped(tasks, header_date=dstr(today))
            try:
                bot.send_message(u.id, text)
            except Exception as e:
                log.error("digest send error: %s", e)
    finally:
        sess.close()

def job_reminders():
    sess = SessionLocal()
    try:
        now = now_local()
        due = (sess.query(Reminder)
               .filter(Reminder.fired==False, Reminder.date<=now.date())
               .all())
        for r in due:
            # если уже пора по времени
            if r.date < now.date() or (r.date == now.date() and r.time <= now.time().replace(second=0, microsecond=0)):
                t = sess.query(Task).filter(Task.id==r.task_id, Task.user_id==r.user_id).first()
                if t:
                    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
                    try:
                        bot.send_message(r.user_id, f"⏰ Напоминание: {t.category}/{t.subcategory or '—'} — {t.text} (до {dl})")
                    except Exception as e:
                        log.error("reminder send error: %s", e)
                r.fired = True
        sess.commit()
    finally:
        sess.close()

def scheduler_loop():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)   # утренний дайджест
    schedule.every(1).minutes.do(job_reminders)             # reminders
    while True:
        schedule.run_pending()
        time.sleep(1)

# ========= FLASK/WEBHOOK =========
app = Flask(__name__)

@app.route("/" + os.getenv("WEBHOOK_SECRET"), methods=["POST"])
def webhook():
    data = request.get_data().decode("utf-8")
    upd = types.Update.de_json(data)
    bot.process_new_updates([upd])
    return "OK", 200

@app.route("/")
def home():
    return "TasksBot is running"

# ========= START =========
if __name__ == "__main__":
    init_db()
    # webhook only
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)

    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
