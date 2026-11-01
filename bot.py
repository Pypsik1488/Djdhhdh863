#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот (aiogram 3) с обязательной подпиской, массовой отправкой, 
скриншотами только при ошибках и подробной отчетностью.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

BOT_TOKEN = "8949252988:AAHwB5upEkrz_o3wSJUEmVEq7EnOOtZNrw8"
HEADLESS = True

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "support_bot.db"
SHOT_DIR = BASE_DIR / "screenshots"
PW_MARKER = BASE_DIR / ".playwright_chromium_ok"

def _pip_install(spec):
    cmd = [sys.executable, "-m", "pip", "install", "-q", spec]
    if subprocess.run(cmd).returncode != 0:
        subprocess.check_call(cmd + ["--break-system-packages"])

def ensure_dependencies():
    required = [
        ("aiogram", "aiogram>=3.4"),
        ("aiosqlite", "aiosqlite"),
        ("playwright", "playwright"),
    ]
    for module, spec in required:
        try:
            importlib.import_module(module)
        except ImportError:
            _pip_install(spec)
    importlib.invalidate_caches()

    if not PW_MARKER.exists():
        cmd = [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"]
        res = subprocess.run(cmd)
        if res.returncode != 0:
            res = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if res.returncode == 0:
            PW_MARKER.write_text("ok")

ensure_dependencies()

import asyncio
import html
import logging
import random
import re
import time

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("support_bot")

SHOT_DIR.mkdir(exist_ok=True)
esc = html.escape

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULTS = {
    "target_url": (os.getenv("TARGET_URL", "https://telegram.org/support?setln=ru"), "str", "URL страницы с формой"),
    "sel_problem": ('textarea, textarea[name*="message" i], textarea[name*="text" i]', "str", "селектор поля «проблема»"),
    "sel_name": ('input[name*="name" i], input[name*="fio" i], input[placeholder*="ФИО" i], input[placeholder*="имя" i]', "str", "селектор поля ФИО"),
    "sel_email": ('input[type="email"], input[name*="mail" i]', "str", "селектор поля email"),
    "sel_phone": ('input[type="tel"], input[name*="phone" i], input[name*="tel" i]', "str", "селектор поля телефона"),
    "sel_submit": ('button[type="submit"], input[type="submit"], button, .button, div[role="button"]', "str", "селектор кнопки отправки"),
    "sel_captcha": (
        'iframe[title*="recaptcha challenge" i], iframe[src*="bframe"], .g-recaptcha:visible, '
        '.h-captcha:visible, .cf-turnstile:visible, input[name*="captcha" i]:visible, '
        'img[src*="captcha" i]:visible',
        "str",
        "селекторы капчи",
    ),
    "captcha_text": ("я не робот,i'm not a robot,verify you are human,unusual traffic,подтвердите, что вы не робот", "str", "маркеры капчи"),
    "sel_success": ("", "str", "селектор блока успеха"),
    "success_text": ("Спасибо за сообщение,спасибо за обращение,ваше обращение,успешно отправлен,успешно отправлено,thank you,successfully", "str", "маркеры успеха"),
    "headless": (HEADLESS, "bool", "браузер без окна"),
    "step_screens": (False, "bool", "слать скриншот каждого шага"),
    "track_chat_id": ("", "str", "ID чата для отчётов"),
    "poll_interval": (2, "int", "пауза между опросами, сек"),
    "retry_delay": (60, "int", "задержка перед повтором, сек"),
    "max_attempts": (2, "int", "максимум попыток"),
    "ping_timeout": (10, "int", "таймаут ping, сек"),
    "nav_timeout": (30, "int", "таймаут браузера, сек"),
    "success_timeout": (15, "int", "время ожидания успеха, сек"),
    "type_delay_min": (10, "int", "минимальная задержка ввода"),
    "type_delay_max": (40, "int", "максимальная задержка ввода"),
    "user_agent": (DEFAULT_UA, "str", "User-Agent"),
}

BOOL_TRUE = {"1", "true", "yes", "on", "да", "y"}
BOOL_FALSE = {"0", "false", "no", "off", "нет", "n"}

def cast_value(raw, typ):
    if typ == "bool":
        low = str(raw).strip().lower()
        if low in BOOL_TRUE: return True
        if low in BOOL_FALSE: return False
        raise ValueError("ожидается true/false")
    if typ == "int":
        val = int(str(raw).strip())
        if val < 0: raise ValueError("число >= 0")
        return val
    if typ == "float":
        return float(str(raw).strip())
    return str(raw)

DB = None

async def db_init():
    global DB
    DB = await aiosqlite.connect(DB_PATH)
    DB.row_factory = aiosqlite.Row
    await DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            sub_until INTEGER DEFAULT 0,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS ticket_batches (
            batch_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            total_count INTEGER NOT NULL,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'processing',
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT DEFAULT '',
            batch_idx INTEGER DEFAULT 1,
            user_id INTEGER NOT NULL,
            problem TEXT NOT NULL, fio TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '', next_try_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, next_try_at);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    await DB.execute("UPDATE tickets SET status='pending' WHERE status='processing'")
    await DB.commit()
    await DB.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('admin_id', '8404647058')")
    await DB.commit()

async def db_exec(sql, params=()):
    await DB.execute(sql, params)
    await DB.commit()

async def db_one(sql, params=()):
    cur = await DB.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def db_all(sql, params=()):
    cur = await DB.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

async def get_cfg():
    rows = await db_all("SELECT key, value FROM settings")
    raw = {r["key"]: r["value"] for r in rows}
    cfg = {}
    for key, (default, typ, _desc) in DEFAULTS.items():
        if key in raw:
            try:
                cfg[key] = cast_value(raw[key], typ)
                continue
            except Exception:
                pass
        cfg[key] = default
    return cfg

async def set_setting(key, value):
    await db_exec("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

async def get_admin_id():
    row = await db_one("SELECT value FROM settings WHERE key='admin_id'")
    if row and str(row["value"]).isdigit():
        return int(row["value"])
    return 8404647058

async def is_admin(user_id):
    return user_id == 8404647058 or (await get_admin_id()) == user_id

async def has_active_sub(user_id):
    if await is_admin(user_id):
        return True
    row = await db_one("SELECT sub_until FROM users WHERE user_id=?", (user_id,))
    if row and row["sub_until"] > time.time():
        return True
    return False

async def set_ticket(tid, **fields):
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    await db_exec(f"UPDATE tickets SET {cols} WHERE id=?", (*fields.values(), tid))

async def safe_send(bot, chat_id, text, **kw):
    for _ in range(2):
        try:
            return await bot.send_message(chat_id, text, **kw)
        except TelegramRetryAfter as ex:
            await asyncio.sleep(ex.retry_after + 1)
        except Exception:
            return None
    return None

async def send_long(bot, chat_id, text, **kw):
    chunk = ""
    for line in text.split("
"):
        if len(chunk) + len(line) + 1 > 3800:
            await safe_send(bot, chat_id, chunk, **kw)
            chunk = ""
        chunk += line + "
"
    if chunk.strip():
        await safe_send(bot, chat_id, chunk, **kw)

async def send_shot(bot, chat_id, path, caption):
    caption = caption[:1000]
    for _ in range(2):
        try:
            await bot.send_photo(chat_id, FSInputFile(path), caption=caption)
            return True
        except TelegramBadRequest:
            try:
                await bot.send_document(chat_id, FSInputFile(path), caption=caption)
                return True
            except Exception:
                return False
        except Exception:
            return False
    return False

def short_err(ex):
    first = (str(ex).split("
")[0] or "").strip()
    return f"{type(ex).__name__}: {first}"[:300]

router = Router()
BTN_NEW = "🚀 Отправить жалобы"
BTN_STATS = "📊 Моя статистика"
BTN_SUB = "💎 Купить подписку"
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")

class Form(StatesGroup):
    problem = State()
    fio = State()
    email = State()
    phone = State()
    count = State()

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_SUB)]
    ], resize_keyboard=True)

def stats_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить статус", callback_data="stats_refresh")]])

async def register_user(m: Message):
    u = m.from_user
    await db_exec("INSERT OR IGNORE INTO users(user_id, username, full_name, sub_until, created_at) VALUES(?,?,?,?,?)", (u.id, u.username or "", u.full_name or "", 0, int(time.time())))

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await register_user(m)
    sub_ok = await has_active_sub(m.from_user.id)
    sub_text = "🟢 Активна" if sub_ok else "🔴 Отсутствует"
    text = (
        f"👋 Здравствуйте, <b>{esc(m.from_user.full_name or 'друг')}</b>!

"
        f"Статус вашей подписки: <b>{sub_text}</b>

"
        "Для работы с ботом необходима активная подписка."
    )
    if not sub_ok:
        text += "

💎 Приобретите доступ у администратора: <b>@Yamayk_a</b>"
    if await is_admin(m.from_user.id):
        text += "

👑 <b>Админ-панель:</b>
/sub ID дни — выдать подписку
/users — список юзеров"
    await m.answer(text, reply_markup=main_menu())

@router.message(F.text == BTN_SUB)
async def sub_info(m: Message):
    await m.answer(
        "💎 <b>Покупка подписки</b>

"
        "Для доступа к боту и массовой отправке жалоб приобретите подписку у администратора:
"
        "👉 <b>@Yamayk_a</b>"
    )

@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_menu())

async def build_stats_text(user_id):
    rows = await db_all("SELECT status, COUNT(*) AS c FROM tickets WHERE user_id=? GROUP BY status", (user_id,))
    by = {r["status"]: r["c"] for r in rows}
    total = sum(by.values())
    sub_row = await db_one("SELECT sub_until FROM users WHERE user_id=?", (user_id,))
    sub_until = sub_row["sub_until"] if sub_row else 0
    if await is_admin(user_id) or sub_until > time.time():
        sub_str = "Безлимит (Админ)" if await is_admin(user_id) else time.strftime("%Y-%m-%d %H:%M", time.gmtime(sub_until))
    else:
        sub_str = "Отсутствует"
    return (
        f"📊 <b>Ваша статистика</b>
"
        f"💎 Подписка: <b>{sub_str}</b>
"
        f"━━━━━━━━━━━━━━━
"
        f"📨 Всего заявок: <b>{total}</b>
"
        f"✅ Отправлено: <b>{by.get('sent', 0)}</b>
"
        f"⏳ В очереди: <b>{by.get('pending', 0) + by.get('processing', 0)}</b>
"
        f"❌ Ошибки: <b>{by.get('failed', 0)}</b>"
    )

@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def cmd_stats(m: Message):
    await register_user(m)
    await m.answer(await build_stats_text(m.from_user.id), reply_markup=stats_kb())

@router.callback_query(F.data == "stats_refresh")
async def cb_stats_refresh(c: CallbackQuery):
    try:
        await c.message.edit_text(await build_stats_text(c.from_user.id), reply_markup=stats_kb())
    except TelegramBadRequest: pass
    await c.answer("Обновлено")

@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def start_form(m: Message, state: FSMContext):
    await register_user(m)
    if not await has_active_sub(m.from_user.id):
        return await m.answer("⛔ <b>Доступ заблокирован!</b>
У вас нет активной подписки. Купите её у @Yamayk_a", reply_markup=main_menu())
    await state.clear()
    await state.set_state(Form.problem)
    await m.answer("1️⃣ Опишите текст жалобы/проблемы (от 5 символов):", reply_markup=main_menu())

@router.message(Command("sub"))
async def cmd_sub(m: Message, command: CommandObject):
    if not await is_admin(m.from_user.id): return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        return await m.answer("Использование: <code>/sub USER_ID ДНИ</code>")
    uid = int(args[0])
    days = int(args[1])
    user_row = await db_one("SELECT sub_until FROM users WHERE user_id=?", (uid,))
    current = user_row["sub_until"] if user_row and user_row["sub_until"] > time.time() else time.time()
    new_sub = int(current + days * 86400)
    await db_exec("UPDATE users SET sub_until=? WHERE user_id=?", (new_sub, uid))
    date_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(new_sub))
    await m.answer(f"✅ Пользователю <code>{uid}</code> выдана подписка до <b>{date_str} UTC</b>")
    await safe_send(m.bot, uid, f"💎 Вам выдана/продлена подписка до <b>{date_str} UTC</b>!")

@router.message(Command("users"))
async def cmd_users(m: Message):
    if not await is_admin(m.from_user.id): return
    rows = await db_all("SELECT user_id, username, full_name, sub_until FROM users ORDER BY created_at DESC LIMIT 20")
    lines = ["👥 <b>Последние пользователи:</b>
"]
    for r in rows:
        sub = "🟢 Активна" if r["sub_until"] > time.time() else "🔴 Нет"
        lines.append(f"• <code>{r['user_id']}</code> (@{r['username'] or '—'}) — {sub}")
    await send_long(m.bot, m.chat.id, "
".join(lines))

@router.message(Form.problem, F.text)
async def f_problem(m: Message, state: FSMContext):
    if not await has_active_sub(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Подписка истекла!", reply_markup=main_menu())
    if len(m.text.strip()) < 5: return await m.answer("Слишком короткий текст.")
    await state.update_data(problem=m.text.strip())
    await state.set_state(Form.fio)
    await m.answer("2️⃣ Введите ФИО заявителя:")

@router.message(Form.fio, F.text)
async def f_fio(m: Message, state: FSMContext):
    if not await has_active_sub(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Подписка истекла!", reply_markup=main_menu())
    await state.update_data(fio=m.text.strip())
    await state.set_state(Form.email)
    await m.answer("3️⃣ Введите контактный email:")

@router.message(Form.email, F.text)
async def f_email(m: Message, state: FSMContext):
    if not await has_active_sub(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Подписка истекла!", reply_markup=main_menu())
    if not EMAIL_RE.match(m.text.strip()): return await m.answer("Неверный email.")
    await state.update_data(email=m.text.strip())
    await state.set_state(Form.phone)
    await m.answer("4️⃣ Введите номер телефона:")

@router.message(Form.phone, F.text)
async def f_phone(m: Message, state: FSMContext):
    if not await has_active_sub(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Подписка истекла!", reply_markup=main_menu())
    await state.update_data(phone=m.text.strip())
    await state.set_state(Form.count)
    await m.answer("5️⃣ Сколько отправить жалоб? (введите число, например: <b>50</b>)")

@router.message(Form.count, F.text)
async def f_count(m: Message, state: FSMContext):
    if not await has_active_sub(m.from_user.id):
        await state.clear()
        return await m.answer("⛔ Подписка истекла!", reply_markup=main_menu())
    raw = m.text.strip()
    if not raw.isdigit() or int(raw) < 1 or int(raw) > 500:
        return await m.answer("Введите число от 1 до 500.")
    count = int(raw)
    data = await state.get_data()
    await state.clear()

    now = int(time.time())
    batch_id = f"b_{m.from_user.id}_{now}"
    
    await DB.execute(
        "INSERT INTO ticket_batches(batch_id, user_id, total_count, sent_count, failed_count, status, created_at) VALUES(?,?,?,0,0,'processing',?)",
        (batch_id, m.from_user.id, count, now)
    )

    for i in range(1, count + 1):
        await DB.execute(
            "INSERT INTO tickets(batch_id, batch_idx, user_id, problem, fio, email, phone, status, attempts, next_try_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,'pending',0,?,?,?)",
            (batch_id, i, m.from_user.id, data["problem"], data["fio"], data["email"], data["phone"], now, now, now)
        )
    await DB.commit()

    await m.answer(
        f"🚀 <b>Принято в работу!</b>
"
        f"Запущена отправка пакета из <b>{count} жб</b>.
"
        f"Вы будете получать уведомления о ходе отправки.",
        reply_markup=main_menu()
    )

async def ping_url(url, timeout):
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers={"User-Agent": DEFAULT_UA}, allow_redirects=True) as resp:
                return True, f"HTTP {resp.status}"
    except Exception as ex:
        return False, short_err(ex)

STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]

class Run:
    def __init__(self, bot, ticket, cfg):
        self.bot, self.t, self.cfg = bot, ticket, cfg
        self.logs, self.n, self.page, self.last_shot = [], 0, None, None

    async def snap(self, tag, text, force=False):
        if self.page is None: return None
        self.n += 1
        path = SHOT_DIR / f"ticket{self.t['id']}_{time.strftime('%Y%m%d_%H%M%S')}_{self.n:02d}_{tag}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True, timeout=8000)
        except Exception:
            return None
        self.last_shot = str(path)
        if force:
            caption = (
                f"❌ <b>Ошибка при отправке заявки</b> #{self.t['id']}
"
                f"<b>Причина:</b> {text}

"
                f"⚠️ Пожалуйста, сообщите @Yamayk_a"
            )
            await send_shot(self.bot, self.t['user_id'], str(path), caption)
        return str(path)

async def safe_body(page):
    try: return (await page.inner_text("body")).lower()
    except Exception: return ""

def split_markers(raw):
    return [x.strip().lower() for x in str(raw).split(",") if x.strip()]

async def detect_captcha(page, cfg):
    if cfg["sel_captcha"].strip():
        try:
            if await page.locator(cfg["sel_captcha"]).count() > 0: return True
        except Exception: pass
    body = await safe_body(page)
    return any(mk in body for mk in split_markers(cfg["captcha_text"]))

async def _human_type(page, selector, text, cfg):
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=10000)
    await loc.click()
    await loc.fill("")
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(cfg["type_delay_min"], cfg["type_delay_max"]) / 1000.0)

async def submit_form(run):
    cfg, t = run.cfg, run.t
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=bool(cfg["headless"]), args=LAUNCH_ARGS)
            try:
                ctx = await browser.new_context(user_agent=cfg["user_agent"] or DEFAULT_UA, viewport={"width": 1366, "height": 850})
                await ctx.add_init_script(STEALTH_JS)
                page = await ctx.new_page()
                run.page = page
                page.set_default_timeout(cfg["nav_timeout"] * 1000)

                await page.goto(cfg["target_url"], wait_until="domcontentloaded")
                await asyncio.sleep(1)

                if await detect_captcha(page, cfg):
                    await run.snap("captcha_open", "Обнаружена капча при открытии страницы", force=True)
                    return "captcha", "капча при открытии"

                fields = [
                    ("problem", "Проблема", t["problem"]),
                    ("name", "ФИО", t["fio"]),
                    ("email", "Email", t["email"]),
                    ("phone", "Телефон", t["phone"])
                ]
                for key, label, val in fields:
                    sel = cfg["sel_" + key].strip()
                    if sel:
                        await _human_type(page, sel, val, cfg)

                if await detect_captcha(page, cfg):
                    await run.snap("captcha_before_submit", "Обнаружена капча перед отправкой", force=True)
                    return "captcha", "капча перед отправкой"

                btn = None
                for sel in cfg["sel_submit"].split(","):
                    sel = sel.strip()
                    if sel and await page.locator(sel).count() > 0:
                        btn = page.locator(sel).first
                        break
                if not btn:
                    try:
                        btn = page.get_by_role("button", name=re.compile(r"submit|отправить", re.I)).first
                        if await btn.count() == 0: btn = None
                    except Exception: pass

                if not btn:
                    err_msg = "Кнопка отправки не найдена на странице"
                    await run.snap("btn_not_found", err_msg, force=True)
                    raise RuntimeError(err_msg)

                await btn.scroll_into_view_if_needed()
                await btn.click()

                deadline = time.monotonic() + cfg["success_timeout"]
                markers = split_markers(cfg["success_text"])
                while time.monotonic() < deadline:
                    body = await safe_body(page)
                    if any(mk in body for mk in markers):
                        return "sent", "успешно отправлено"
                    await asyncio.sleep(0.4)

                return "sent", "отправлено (по таймауту клика)"
            except Exception as ex:
                err_msg = short_err(ex)
                await run.snap("exec_error", err_msg, force=True)
                return "error", err_msg
            finally:
                await browser.close()
    except Exception as ex:
        err_msg = f"ошибка браузера: {short_err(ex)}"
        return "error", err_msg

async def check_and_send_batch_summary(bot, batch_id):
    batch = await db_one("SELECT * FROM ticket_batches WHERE batch_id=?", (batch_id,))
    if not batch: return
    
    total = batch["total_count"]
    sent = batch["sent_count"]
    failed = batch["failed_count"]
    
    if (sent + failed) >= total and batch["status"] != "completed":
        await db_exec("UPDATE ticket_batches SET status='completed' WHERE batch_id=?", (batch_id,))
        
        summary_text = (
            f"🏁 <b>Рассылка завершена!</b>

"
            f"📦 Сколько всего жб: <b>{total}</b>
"
            f"✅ Успешно отправлено: <b>{sent}</b>
"
            f"❌ Не отправились: <b>{failed}</b>"
        )
        await safe_send(bot, batch["user_id"], summary_text)

async def process_ticket(bot, t, cfg):
    run = Run(bot, t, cfg)
    ok, info = await ping_url(cfg["target_url"], cfg["ping_timeout"])
    if not ok:
        await db_exec("UPDATE tickets SET next_try_at=?, last_error=? WHERE status='pending'", (int(time.time()) + cfg["retry_delay"], f"сайт недоступен: {info}"[:300]))
        return

    await set_ticket(t["id"], status="processing")
    status, detail = await submit_form(run)

    batch_id = t["batch_id"]
    batch_idx = t["batch_idx"]
    
    batch = await db_one("SELECT total_count FROM ticket_batches WHERE batch_id=?", (batch_id,)) if batch_id else None
    total_in_batch = batch["total_count"] if batch else 1

    if status == "sent":
        await set_ticket(t["id"], status="sent", attempts=t["attempts"] + 1, last_error="")
        if batch_id:
            await db_exec("UPDATE ticket_batches SET sent_count = sent_count + 1 WHERE batch_id=?", (batch_id,))
        await safe_send(bot, t["user_id"], f"✅ Отправлено {batch_idx}/{total_in_batch} жб")
    elif status == "captcha":
        await set_ticket(t["id"], status="manual", attempts=t["attempts"] + 1, last_error="капча")
        if batch_id:
            await db_exec("UPDATE ticket_batches SET failed_count = failed_count + 1 WHERE batch_id=?", (batch_id,))
        await safe_send(
            bot, t["user_id"],
            f"❌ Не удалось отправить {batch_idx}/{total_in_batch} жб
"
            f"<b>Причина:</b> Требуется капча
"
            f"⚠️ Сообщите @Yamayk_a"
        )
    else:
        max_att = cfg["max_attempts"]
        att = t["attempts"] + 1
        if att >= max_att:
            await set_ticket(t["id"], status="failed", attempts=att, last_error=detail[:300])
            if batch_id:
                await db_exec("UPDATE ticket_batches SET failed_count = failed_count + 1 WHERE batch_id=?", (batch_id,))
            await safe_send(
                bot, t["user_id"],
                f"❌ Не удалось отправить {batch_idx}/{total_in_batch} жб
"
                f"<b>Причина:</b> {detail}
"
                f"⚠️ Сообщите @Yamayk_a"
            )
        else:
            await set_ticket(t["id"], status="pending", attempts=att, last_error=detail[:300], next_try_at=int(time.time()) + cfg["retry_delay"])

    if batch_id:
        await check_and_send_batch_summary(bot, batch_id)

async def worker(bot):
    while True:
        try:
            cfg = await get_cfg()
            if not cfg["target_url"]:
                await asyncio.sleep(3)
                continue
            ticket = await db_one("SELECT * FROM tickets WHERE status='pending' AND next_try_at<=? ORDER BY id LIMIT 1", (int(time.time()),))
            if not ticket:
                await asyncio.sleep(cfg["poll_interval"])
                continue
            await process_ticket(bot, ticket, cfg)
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)

async def main():
    token = os.getenv("BOT_TOKEN") or BOT_TOKEN
    if not token: sys.exit(1)
    await db_init()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    worker_task = asyncio.create_task(worker(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()
        await DB.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
