# =========================================================
#  SUMSKAYA LINE SL — корпоративный бот
#  v0.3 — приём документов из групп + очередь + статусы
# =========================================================

import os
import json
import asyncio
import logging
from time import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
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

DEPTS = {
    "ops":   {"name": "Операционные вопросы",   "emoji": "⚙️"},
    "fin":   {"name": "Финансы и платежи",      "emoji": "💶"},
    "stock": {"name": "Склад и товарный учёт",  "emoji": "📦"},
    "mkt":   {"name": "Маркетинг",              "emoji": "📣"},
    "hr":    {"name": "HR",                     "emoji": "👥"},
    "it":    {"name": "IT-суппорт программ",    "emoji": "🖥"},
}

LOCALES = {
    "reina":     {"name": "P+S Reina",   "emoji": "🅿️", "tag": "REINA"},
    "fransia":   {"name": "P+S Fransia", "emoji": "🅿️", "tag": "FRANSIA"},
    "panaderia": {"name": "Panadería",   "emoji": "🥖", "tag": "PANADERIA"},
    "boiboi":    {"name": "Boi Boi",     "emoji": "🍣", "tag": "BOIBOI"},
}

DOCTYPES = {
    "factura": {"name": "Фактура",  "emoji": "🧾"},
    "baja":    {"name": "Списание", "emoji": "📉"},
}

ROLE_PATRON = "Патрон"
ROLE_MANAGER = "Менеджер"
ROLE_STAFF = "Сотрудник"

# ---------------- GOOGLE SHEETS ----------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_gc = gspread.authorize(Credentials.from_service_account_info(GOOGLE_CREDS, scopes=SCOPES))
_sh = _gc.open_by_key(SHEET_ID)

USERS_WS = "Users"
CONTENT_WS = "Content"
GROUPS_WS = "Groups"
DOCS_WS = "Docs"

HEADERS = {
    USERS_WS:   ["ID", "Имя", "Роль", "Отделы", "Статус"],
    CONTENT_WS: ["Отдел", "Локаль", "Порядок", "Раздел", "Текст"],
    GROUPS_WS:  ["ChatID", "Название группы", "Локаль", "Тип"],
    DOCS_WS:    ["Дата", "Время", "Локаль", "Тип", "Автор",
                 "ChatID", "MessageID", "FileID", "Текст", "Статус"],
}

_cache = {}
CACHE_TTL = 45


def ws(name: str):
    try:
        return _sh.worksheet(name)
    except gspread.WorksheetNotFound:
        w = _sh.add_worksheet(title=name, rows=1000, cols=14)
        w.append_row(HEADERS.get(name, []))
        return w


def rows(name: str, force=False):
    ts, data = _cache.get(name, (0, []))
    if force or time() - ts > CACHE_TTL:
        data = ws(name).get_all_records()
        _cache[name] = (time(), data)
    return data


def drop_cache(name: str):
    _cache[name] = (0, [])


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
    drop_cache(USERS_WS)
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


# ---------------- ГРУППЫ ----------------

def group_map(chat_id: int):
    """Возвращает (локаль, тип) для зарегистрированной группы."""
    for r in rows(GROUPS_WS):
        if str(r.get("ChatID")).strip() == str(chat_id):
            loc = str(r.get("Локаль")).strip()
            typ = str(r.get("Тип")).strip()
            if loc in LOCALES and typ in DOCTYPES:
                return loc, typ
    return None


# ---------------- ДОКУМЕНТЫ ----------------

def save_doc(loc, typ, author, chat_id, msg_id, file_id, text) -> int:
    now = datetime.now()
    w = ws(DOCS_WS)
    w.append_row([
        now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
        loc, typ, author,
        str(chat_id), str(msg_id), file_id or "", (text or "")[:2000],
        "новый",
    ], value_input_option="RAW")
    drop_cache(DOCS_WS)
    return len(w.col_values(1))  # номер строки


def set_doc_status(row_idx: int, status: str):
    ws(DOCS_WS).update_cell(row_idx, 10, status)
    drop_cache(DOCS_WS)


def pending_docs():
    out = []
    for i, r in enumerate(rows(DOCS_WS, force=True), start=2):
        if str(r.get("Статус")).strip() == "новый":
            out.append((i, r))
    return out


# ---------------- БОТ ----------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


async def take_over(event, text: str, kb: InlineKeyboardMarkup | None = None):
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
        n = len(pending_docs())
        kb.append([InlineKeyboardButton(text=f"📥 Очередь документов ({n})", callback_data="q")])
        kb.append([InlineKeyboardButton(text="👤 Персонал", callback_data="staff")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def kb_locales(dept: str) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:{dept}:{code}")]
          for code, l in LOCALES.items()]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def sections(dept: str, loc: str) -> list:
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


def home_text(u) -> str:
    return (f"<b>{COMPANY}</b>\n"
            f"{u.get('Имя')} · {u.get('Роль')}\n\nВыберите отдел:")


def crumb(dept: str, loc: str = None) -> str:
    s = f"{DEPTS[dept]['emoji']} <b>{DEPTS[dept]['name']}</b>"
    if loc:
        s += f"\n{LOCALES[loc]['emoji']} {LOCALES[loc]['name']}"
    return s


# ---------------- ПРИЁМ ИЗ ГРУПП ----------------

@dp.my_chat_member()
async def on_added(ev: ChatMemberUpdated):
    """Бот добавлен в группу — сообщает свой ChatID."""
    if ev.chat.type not in ("group", "supergroup"):
        return
    if ev.new_chat_member.status in ("member", "administrator"):
        try:
            await bot.send_message(
                ev.chat.id,
                f"Бот подключён.\nChatID: <code>{ev.chat.id}</code>\n"
                f"Название: {ev.chat.title}\n\n"
                f"Внесите ChatID в лист Groups, чтобы начать приём документов."
            )
        except Exception as e:
            log.warning("greet failed: %s", e)
        for p in patrons():
            try:
                await bot.send_message(
                    p, f"➕ Бот добавлен в группу\n<b>{ev.chat.title}</b>\n"
                       f"ChatID: <code>{ev.chat.id}</code>")
            except Exception:
                pass


@dp.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.answer(f"ChatID: <code>{m.chat.id}</code>\nТип: {m.chat.type}")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_intake(m: Message):
    mapped = group_map(m.chat.id)
    if not mapped:
        return
    loc, typ = mapped

    file_id, text = None, None
    if m.photo:
        file_id = m.photo[-1].file_id
        text = m.caption
    elif m.document:
        file_id = m.document.file_id
        text = m.caption
    elif m.text:
        text = m.text
    else:
        return

    author = m.from_user.full_name if m.from_user else "—"
    row_idx = save_doc(loc, typ, author, m.chat.id, m.message_id, file_id, text)

    L, T = LOCALES[loc], DOCTYPES[typ]
    card = (f"{T['emoji']} <b>{T['name']}</b> · {L['emoji']} {L['name']}\n"
            f"От: {author}\n"
            f"{datetime.now().strftime('%d.%m.%Y %H:%M')}")
    if text:
        card += f"\n\n{text[:600]}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправлено в bilz", callback_data=f"sent:{row_idx}")
    ]])

    for p in patrons():
        try:
            if file_id:
                await bot.send_photo(p, file_id, caption=card, reply_markup=kb) if m.photo \
                    else await bot.send_document(p, file_id, caption=card, reply_markup=kb)
            else:
                await bot.send_message(p, card, reply_markup=kb)
        except Exception as e:
            log.warning("deliver failed: %s", e)


@dp.callback_query(F.data.startswith("sent:"))
async def cb_sent(c: CallbackQuery):
    if not is_patron(get_user(c.from_user.id)):
        await c.answer("Только Патрон", show_alert=True)
        return
    row_idx = int(c.data.split(":")[1])
    set_doc_status(row_idx, "отправлено")
    try:
        await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Отправлено", callback_data="noop")]]))
    except Exception:
        pass
    await c.answer("Отмечено")


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "q")
async def cb_queue(c: CallbackQuery):
    if not is_patron(get_user(c.from_user.id)):
        await c.answer("Только Патрон", show_alert=True)
        return
    items = pending_docs()
    if not items:
        text = "📥 <b>Очередь документов</b>\n\nВсё обработано."
    else:
        lines = [f"📥 <b>Очередь документов</b> — {len(items)}\n"]
        for _, r in items[:30]:
            L = LOCALES.get(str(r.get("Локаль")).strip(), {}).get("name", r.get("Локаль"))
            T = DOCTYPES.get(str(r.get("Тип")).strip(), {}).get("name", r.get("Тип"))
            lines.append(f"• {r.get('Дата')} {r.get('Время')} · {L} · {T} · {r.get('Автор')}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="q")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    await take_over(c, text[:4000], kb)
    await c.answer()


# ---------------- ЛИЧНЫЕ ХЕНДЛЕРЫ ----------------

@dp.message(CommandStart())
async def start(m: Message):
    if m.chat.type != "private":
        return
    uid, name = m.from_user.id, m.from_user.full_name
    u = get_user(uid)

    if not rows(USERS_WS, force=True):
        ws(USERS_WS).append_row([str(uid), name, ROLE_PATRON, "all", "active"])
        drop_cache(USERS_WS)
        u = get_user(uid)
        await m.answer(f"<b>{COMPANY}</b>\nВы зарегистрированы как <b>{ROLE_PATRON}</b>.",
                       reply_markup=kb_home(u))
        return

    if not u:
        ws(USERS_WS).append_row([str(uid), name, "", "", "pending"])
        drop_cache(USERS_WS)
        await m.answer("Запрос на доступ отправлен. Ожидайте подтверждения.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ok:{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no:{uid}"),
        ]])
        for p in patrons():
            try:
                await bot.send_message(p, f"🔔 Запрос доступа\n<b>{name}</b>\nID: <code>{uid}</code>",
                                       reply_markup=kb)
            except Exception:
                pass
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
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=str(r["Раздел"])[:60], callback_data=f"s:{dept}:{loc}:{i}")]
              for i, r in enumerate(items)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"d:{dept}")]])
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
        [InlineKeyboardButton(text="🏠 В начало", callback_data="home")]])
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    await take_over(c, "\n".join(lines)[:4000], kb)
    await c.answer()


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


async def main():
    for name in (USERS_WS, CONTENT_WS, GROUPS_WS, DOCS_WS):
        ws(name)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
