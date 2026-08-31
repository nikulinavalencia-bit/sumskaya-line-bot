# =========================================================
#  SUMSKAYA LINE SL — корпоративный бот
#  v0.2 — 6 отделов → 4 локаля → разделы → текст
#  Python + aiogram3 + gspread (Google Sheets)
# =========================================================

import os
import json
import asyncio
import logging
from time import time

import gspread
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sumskaya")

# ---------------- КОНФИГ ----------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDS = json.loads(os.environ["GOOGLE_CREDS"])

COMPANY = "SUMSKAYA LINE SL"

# Отделы. Ключ — служебный код, после запуска не менять.
DEPTS = {
    "ops":   {"name": "Операционные вопросы",   "emoji": "⚙️"},
    "fin":   {"name": "Финансы и платежи",      "emoji": "💶"},
    "stock": {"name": "Склад и товарный учёт",  "emoji": "📦"},
    "mkt":   {"name": "Маркетинг",              "emoji": "📣"},
    "hr":    {"name": "HR",                     "emoji": "👥"},
    "it":    {"name": "IT-суппорт программ",    "emoji": "🖥"},
}

# Локали. Ключ — служебный код, после запуска не менять.
LOCALES = {
    "reina":     {"name": "P+S Reina",   "emoji": "🅿️"},
    "fransia":   {"name": "P+S Fransia", "emoji": "🅿️"},
    "panaderia": {"name": "Panadería",   "emoji": "🥖"},
    "boiboi":    {"name": "Boi Boi",     "emoji": "🍣"},
}

# Роли
ROLE_PATRON = "Патрон"       # главный редактор, полный доступ
ROLE_MANAGER = "Менеджер"    # редактирование своих отделов (задел на будущее)
ROLE_STAFF = "Сотрудник"     # чтение всего

# ---------------- GOOGLE SHEETS ----------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_gc = gspread.authorize(Credentials.from_service_account_info(GOOGLE_CREDS, scopes=SCOPES))
_sh = _gc.open_by_key(SHEET_ID)

USERS_WS = "Users"       # ID | Имя | Роль | Отделы | Статус
CONTENT_WS = "Content"   # Отдел | Локаль | Порядок | Раздел | Текст

HEADERS = {
    USERS_WS: ["ID", "Имя", "Роль", "Отделы", "Статус"],
    CONTENT_WS: ["Отдел", "Локаль", "Порядок", "Раздел", "Текст"],
}

_cache = {USERS_WS: (0, []), CONTENT_WS: (0, [])}
CACHE_TTL = 45


def ws(name: str):
    try:
        return _sh.worksheet(name)
    except gspread.WorksheetNotFound:
        w = _sh.add_worksheet(title=name, rows=500, cols=12)
        w.append_row(HEADERS.get(name, []))
        return w


def rows(name: str, force=False):
    ts, data = _cache.get(name, (0, []))
    if force or time() - ts > CACHE_TTL:
        data = ws(name).get_all_records()
        _cache[name] = (time(), data)
    return data


# ---------------- ПОЛЬЗОВАТЕЛИ ----------------

def get_user(uid: int):
    for r in rows(USERS_WS):
        if str(r.get("ID")).strip() == str(uid):
            return r
    return None


def is_patron(u) -> bool:
    return bool(u) and u.get("Роль") == ROLE_PATRON


def set_user(uid: int, role=None, depts=None, status=None):
    w = ws(USERS_WS)
    cell = w.find(str(uid), in_column=1)
    if not cell:
        return False
    if role is not None:
        w.update_cell(cell.row, 3, role)
    if depts is not None:
        w.update_cell(cell.row, 4, depts)
    if status is not None:
        w.update_cell(cell.row, 5, status)
    _cache[USERS_WS] = (0, [])
    return True


def patrons() -> list:
    out = []
    for r in rows(USERS_WS):
        if r.get("Роль") == ROLE_PATRON and r.get("Статус") == "active":
            try:
                out.append(int(str(r.get("ID")).strip()))
            except ValueError:
                pass
    return out


# ---------------- КОНТЕНТ ----------------

def sections(dept: str, loc: str) -> list:
    """Разделы конкретного отдела в конкретном локале, по порядку."""
    out = [r for r in rows(CONTENT_WS)
           if str(r.get("Отдел")).strip() == dept
           and str(r.get("Локаль")).strip() == loc
           and str(r.get("Раздел")).strip()]
    def key(r):
        try:
            return int(r.get("Порядок") or 999)
        except (TypeError, ValueError):
            return 999
    return sorted(out, key=key)


# ---------------- БОТ ----------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


async def take_over(event, text: str, kb: InlineKeyboardMarkup | None = None):
    """Единый экран: редактируем сообщение, в котором нажали кнопку."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except Exception:
            await event.message.answer(text, reply_markup=kb)
        return
    await event.answer(text, reply_markup=kb)


def kb_home(u) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=f"{d['emoji']} {d['name']}", callback_data=f"d:{code}")]
          for code, d in DEPTS.items()]
    if is_patron(u):
        kb.append([InlineKeyboardButton(text="👤 Персонал", callback_data="staff")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def kb_locales(dept: str) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:{dept}:{code}")]
          for code, l in LOCALES.items()]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def kb_sections(dept: str, loc: str, items: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=str(r["Раздел"])[:60], callback_data=f"s:{dept}:{loc}:{i}")]
          for i, r in enumerate(items)]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"d:{dept}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def home_text(u) -> str:
    return (f"<b>{COMPANY}</b>\n"
            f"{u.get('Имя')} · {u.get('Роль')}\n\n"
            f"Выберите отдел:")


def crumb(dept: str, loc: str = None) -> str:
    s = f"{DEPTS[dept]['emoji']} <b>{DEPTS[dept]['name']}</b>"
    if loc:
        s += f"\n{LOCALES[loc]['emoji']} {LOCALES[loc]['name']}"
    return s


# ---------------- ХЕНДЛЕРЫ ----------------

@dp.message(CommandStart())
async def start(m: Message):
    uid, name = m.from_user.id, m.from_user.full_name
    u = get_user(uid)

    # первый пользователь становится Патроном
    if not rows(USERS_WS, force=True):
        ws(USERS_WS).append_row([str(uid), name, ROLE_PATRON, "all", "active"])
        _cache[USERS_WS] = (0, [])
        u = get_user(uid)
        await m.answer(f"<b>{COMPANY}</b>\nВы зарегистрированы как <b>{ROLE_PATRON}</b>.",
                       reply_markup=kb_home(u))
        return

    if not u:
        ws(USERS_WS).append_row([str(uid), name, "", "", "pending"])
        _cache[USERS_WS] = (0, [])
        await m.answer("Запрос на доступ отправлен. Ожидайте подтверждения.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ok:{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no:{uid}"),
        ]])
        for p in patrons():
            try:
                await bot.send_message(p, f"🔔 Запрос доступа\n<b>{name}</b>\nID: <code>{uid}</code>",
                                       reply_markup=kb)
            except Exception as e:
                log.warning("notify failed: %s", e)
        return

    if u.get("Статус") != "active":
        await m.answer("Доступ ещё не подтверждён.")
        return

    await m.answer(home_text(u), reply_markup=kb_home(u))


def guard(c: CallbackQuery):
    u = get_user(c.from_user.id)
    return u if u and u.get("Статус") == "active" else None


@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer("Нет доступа", show_alert=True)
        return
    await take_over(c, home_text(u), kb_home(u))
    await c.answer()


@dp.callback_query(F.data.startswith("d:"))
async def cb_dept(c: CallbackQuery):
    if not guard(c):
        await c.answer("Нет доступа", show_alert=True)
        return
    dept = c.data.split(":", 1)[1]
    if dept not in DEPTS:
        await c.answer()
        return
    await take_over(c, f"{crumb(dept)}\n\nВыберите локаль:", kb_locales(dept))
    await c.answer()


@dp.callback_query(F.data.startswith("l:"))
async def cb_loc(c: CallbackQuery):
    if not guard(c):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, dept, loc = c.data.split(":")
    if dept not in DEPTS or loc not in LOCALES:
        await c.answer()
        return
    items = sections(dept, loc)
    if not items:
        text = f"{crumb(dept, loc)}\n\n<i>Раздел пока не наполнен.</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"d:{dept}")]])
    else:
        text = f"{crumb(dept, loc)}\n\nВыберите пункт:"
        kb = kb_sections(dept, loc, items)
    await take_over(c, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_section(c: CallbackQuery):
    if not guard(c):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, dept, loc, idx = c.data.split(":")
    items = sections(dept, loc)
    try:
        r = items[int(idx)]
    except (ValueError, IndexError):
        await c.answer("Пункт не найден", show_alert=True)
        return
    body = str(r.get("Текст") or "").strip() or "<i>Пусто.</i>"
    text = f"{crumb(dept, loc)}\n\n<b>{r.get('Раздел')}</b>\n\n{body}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"l:{dept}:{loc}")],
        [InlineKeyboardButton(text="🏠 В начало", callback_data="home")],
    ])
    await take_over(c, text[:4000], kb)
    await c.answer()


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def cb_approve(c: CallbackQuery):
    if not is_patron(get_user(c.from_user.id)):
        await c.answer("Только Патрон", show_alert=True)
        return
    action, uid = c.data.split(":", 1)
    if action == "no":
        set_user(int(uid), status="denied")
        await take_over(c, "❌ Запрос отклонён.")
        try:
            await bot.send_message(int(uid), "Доступ отклонён.")
        except Exception:
            pass
    else:
        set_user(int(uid), role=ROLE_STAFF, depts="all", status="active")
        await take_over(c, f"✅ Доступ выдан.\nID: <code>{uid}</code>")
        try:
            await bot.send_message(int(uid), "Доступ подтверждён. Нажмите /start")
        except Exception:
            pass
    await c.answer()


@dp.callback_query(F.data == "staff")
async def cb_staff(c: CallbackQuery):
    if not is_patron(get_user(c.from_user.id)):
        await c.answer("Только Патрон", show_alert=True)
        return
    lines = ["<b>Персонал</b>\n"]
    for r in rows(USERS_WS, force=True):
        lines.append(f"• {r.get('Имя')} — {r.get('Роль') or '—'} · {r.get('Статус')}")
    lines.append("\n<i>Роли правятся в листе Users.</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    await take_over(c, "\n".join(lines), kb)
    await c.answer()


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
