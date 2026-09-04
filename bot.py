# =========================================================
#  SUMSKAYA LINE SL — корпоративный бот
#  v0.4 — 3 языка + статус за сегодня + приём из групп
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
    Message, CallbackQuery, ChatMemberUpdated, InputMediaPhoto,
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
LANGS = ["es", "ru", "en"]
DEFAULT_LANG = "ru"

# Локали. Эмодзи меняются здесь одной строкой.
LOCALES = {
    "reina":     {"name": "Reina",     "emoji": "🍝", "tag": "REINA"},
    "fransia":   {"name": "Fransia",   "emoji": "🍕", "tag": "FRANSIA"},
    "panaderia": {"name": "Panadería", "emoji": "🥖", "tag": "PANADERIA"},
    "boiboi":    {"name": "Boi Boi",   "emoji": "🍣", "tag": "BOIBOI"},
}

DEPTS = {
    "ops":   {"emoji": "⚙️"},
    "fin":   {"emoji": "💶"},
    "stock": {"emoji": "📦"},
    "mkt":   {"emoji": "📣"},
    "hr":    {"emoji": "👥"},
    "it":    {"emoji": "🖥"},
}

MENU_EMOJI = "🍽"          # раздел «Меню и фуд кост» живёт внутри Склада

FC_LIMIT = 30.0  # порог фуд коста, %

DOCTYPES = {
    "factura":  {"emoji": "🧾"},
    "baja":     {"emoji": "📉"},
    "traspaso": {"emoji": "🔄"},
}

ROLE_PATRON = "Патрон"
ROLE_MANAGER = "Менеджер"
ROLE_STAFF = "Сотрудник"

# ---------------- ПЕРЕВОДЫ ----------------

T = {
    "dept_ops":   {"es": "Operaciones",        "ru": "Операционные вопросы", "en": "Operations"},
    "dept_fin":   {"es": "Finanzas y pagos",   "ru": "Финансы и платежи",    "en": "Finance & payments"},
    "dept_stock": {"es": "Almacén e inventario","ru": "Склад и товарный учёт","en": "Stock & inventory"},
    "dept_menu":  {"es": "Carta y food cost", "ru": "Меню и фуд кост",   "en": "Menu & food cost"},

    "fc_btn":     {"es": "📊 Análisis food cost", "ru": "📊 Анализ фуд коста", "en": "📊 Food cost analysis"},
    "fc_title":   {"es": "Peores por food cost",  "ru": "Худшие по фуд косту", "en": "Worst by food cost"},
    "fc_none":    {"es": "Sin datos de coste.",   "ru": "Нет данных по себестоимости.", "en": "No cost data."},
    "no_card":    {"es": "sin ficha técnica",     "ru": "без техкарты",       "en": "no tech card"},
    "zero_price": {"es": "precio 0",              "ru": "цена 0",             "en": "zero price"},
    "price":      {"es": "Precio",                "ru": "Цена",               "en": "Price"},
    "cost":       {"es": "Coste",                 "ru": "Себестоимость",      "en": "Cost"},
    "margin":     {"es": "Margen",                "ru": "Наценка",            "en": "Margin"},
    "dishes":     {"es": "platos",                "ru": "блюд",               "en": "dishes"},
    "tech":       {"es": "📋 Ficha técnica", "ru": "📋 Техкарта", "en": "📋 Tech card"},
    "tech_none":  {"es": "Sin ficha técnica.", "ru": "Техкарты нет.", "en": "No tech card."},
    "yield_w":    {"es": "Peso final",  "ru": "Выход",   "en": "Yield"},
    "gross":      {"es": "Bruto",       "ru": "Брутто",  "en": "Gross"},
    "net":        {"es": "Neto",        "ru": "Нетто",   "en": "Net"},
    "photo_view": {"es": "📷 Foto", "ru": "📷 Фото", "en": "📷 Photo"},
    "photo_add":  {"es": "➕ Añadir foto", "ru": "➕ Добавить фото", "en": "➕ Add photo"},
    "photo_wait": {"es": "Envía la foto del plato:", "ru": "Пришлите фото блюда:", "en": "Send the dish photo:"},
    "photo_saved":{"es": "Foto guardada.", "ru": "Фото сохранено.", "en": "Photo saved."},
    "dept_mkt":   {"es": "Marketing",          "ru": "Маркетинг",            "en": "Marketing"},
    "dept_hr":    {"es": "RRHH",               "ru": "HR",                   "en": "HR"},
    "dept_it":    {"es": "Soporte IT",         "ru": "IT-суппорт программ",  "en": "IT support"},

    "doc_factura":  {"es": "Factura",   "ru": "Фактура",    "en": "Invoice"},
    "doc_baja":     {"es": "Baja",      "ru": "Списание",   "en": "Write-off"},
    "doc_traspaso": {"es": "Traspaso",  "ru": "Перемещение","en": "Transfer"},

    "role_Патрон":    {"es": "Patrón",    "ru": "Патрон",    "en": "Patron"},
    "role_Менеджер":  {"es": "Encargado", "ru": "Менеджер",  "en": "Manager"},
    "role_Сотрудник": {"es": "Empleado",  "ru": "Сотрудник", "en": "Staff"},

    "choose_dept":   {"es": "Elige un departamento:", "ru": "Выберите отдел:",  "en": "Choose a department:"},
    "choose_locale": {"es": "Elige un local:",        "ru": "Выберите локаль:", "en": "Choose a location:"},
    "choose_item":   {"es": "Elige una sección:",     "ru": "Выберите пункт:",  "en": "Choose a section:"},
    "empty":         {"es": "Sección aún vacía.",     "ru": "Раздел пока не наполнен.", "en": "Section not filled yet."},
    "back":          {"es": "⬅️ Atrás",  "ru": "⬅️ Назад",    "en": "⬅️ Back"},
    "home":          {"es": "🏠 Inicio", "ru": "🏠 В начало", "en": "🏠 Home"},
    "refresh":       {"es": "🔄 Actualizar", "ru": "🔄 Обновить", "en": "🔄 Refresh"},

    "today":       {"es": "📊 Estado de hoy", "ru": "📊 Статус за сегодня", "en": "📊 Today's status"},
    "today_title": {"es": "Estado de hoy",    "ru": "Статус за сегодня",    "en": "Today's status"},
    "nothing_today": {"es": "Hoy todavía no hay documentos.", "ru": "Сегодня документов пока нет.", "en": "No documents today yet."},

    "queue":       {"es": "📥 Cola de documentos", "ru": "📥 Очередь документов", "en": "📥 Document queue"},
    "queue_empty": {"es": "Todo procesado.", "ru": "Всё обработано.", "en": "All processed."},
    "queue_hint":  {"es": "Pulsa para abrir y reenviar.", "ru": "Нажмите, чтобы открыть и переслать.", "en": "Tap to open and forward."},
    "archive":     {"es": "🗄 Archivo", "ru": "🗄 Архив", "en": "🗄 Archive"},
    "staff":       {"es": "👤 Personal", "ru": "👤 Персонал", "en": "👤 Staff"},
    "sent_btn":    {"es": "✅ Enviado a bilz", "ru": "✅ Отправлено в bilz", "en": "✅ Sent to bilz"},
    "sent_done":   {"es": "✅ Enviado", "ru": "✅ Отправлено", "en": "✅ Sent"},
    "marked":      {"es": "Marcado", "ru": "Отмечено", "en": "Marked"},

    "no_access":   {"es": "Sin acceso", "ru": "Нет доступа", "en": "No access"},
    "only_patron": {"es": "Solo el Patrón", "ru": "Только Патрон", "en": "Patron only"},
    "req_sent":    {"es": "Solicitud enviada. Espera confirmación.", "ru": "Запрос на доступ отправлен. Ожидайте подтверждения.", "en": "Access request sent. Please wait."},
    "req_wait":    {"es": "Acceso aún no confirmado.", "ru": "Доступ ещё не подтверждён.", "en": "Access not confirmed yet."},
    "granted":     {"es": "Acceso confirmado. Pulsa /start", "ru": "Доступ подтверждён. Нажмите /start", "en": "Access granted. Press /start"},
    "from":        {"es": "De", "ru": "От", "en": "From"},
    "lang_set":    {"es": "Idioma: Español", "ru": "Язык: Русский", "en": "Language: English"},
}


def t(key: str, lang: str) -> str:
    return T.get(key, {}).get(lang, T.get(key, {}).get(DEFAULT_LANG, key))


def dept_name(code: str, lang: str) -> str:
    return t(f"dept_{code}", lang)


def doc_name(code: str, lang: str) -> str:
    return t(f"doc_{code}", lang)


def role_name(role: str, lang: str) -> str:
    return t(f"role_{role}", lang) if role else "—"


# ---------------- GOOGLE SHEETS ----------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_gc = gspread.authorize(Credentials.from_service_account_info(GOOGLE_CREDS, scopes=SCOPES))
_sh = _gc.open_by_key(SHEET_ID)

USERS_WS = "Users"
CONTENT_WS = "Content"
GROUPS_WS = "Groups"
DOCS_WS = "Docs"
MENU_WS = "Menu"
TECH_WS = "TechCards"
PHOTOS_WS = "Photos"

HEADERS = {
    USERS_WS:   ["ID", "Имя", "Роль", "Отделы", "Статус", "Язык"],
    CONTENT_WS: ["Отдел", "Локаль", "Порядок", "Раздел", "Текст", "Язык"],
    GROUPS_WS:  ["ChatID", "Название группы", "Локаль", "Тип"],
    DOCS_WS:    ["Дата", "Время", "Локаль", "Тип", "Автор",
                 "ChatID", "MessageID", "FileID", "Текст", "Статус"],
    MENU_WS:    ["Локаль", "Группа", "Подгруппа", "Блюдо", "Ед",
                 "Цена", "Себестоимость", "ФК%"],
    TECH_WS:    ["Блюдо", "Карта №", "Дата", "№", "Ингредиент", "Ед",
                 "Брутто", "Нетто", "Итого вес, кг", "На выход"],
    PHOTOS_WS:  ["Блюдо", "FileID", "Кто добавил", "Дата"],
}

_cache = {}
CACHE_TTL = 45


def _norm(s: str) -> str:
    return " ".join(str(s).split()).strip().lower()


_ws_cache = {}


def ws(name: str):
    """
    Находит лист по имени, прощая мусор вокруг:
    лишние пробелы, регистр и суффиксы импорта — 'Menu 1', 'Menu (2)'.
    """
    if name in _ws_cache:
        return _ws_cache[name]
    target = _norm(name)
    sheets = _sh.worksheets()

    # точное совпадение
    for w in sheets:
        if _norm(w.title) == target:
            _ws_cache[name] = w
            return w

    # 'Menu 1', 'Menu (2)', 'Menu-копия' — берём самый свежий подходящий
    cands = []
    for w in sheets:
        n = _norm(w.title)
        if n.startswith(target) and n != target:
            tail = n[len(target):].strip(" ()-_")
            if tail.isdigit() or tail in ("copy", "копия", ""):
                cands.append(w)
    if cands:
        w = cands[-1]
        log.warning("лист '%s' не найден, использую '%s'", name, w.title)
        _ws_cache[name] = w
        return w

    w = _sh.add_worksheet(title=name, rows=1000, cols=14)
    w.append_row(HEADERS.get(name, []))
    _ws_cache[name] = w
    return w


def ensure_headers():
    """Дописывает недостающие колонки в существующие листы."""
    for name, cols in HEADERS.items():
        w = ws(name)
        cur = w.row_values(1)
        if not cur:
            w.update("A1", [cols])
            continue
        missing = [c for c in cols if c not in cur]
        if missing:
            w.update(f"{chr(65 + len(cur))}1", [missing])


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


def ulang(u) -> str:
    l = str((u or {}).get("Язык", "")).strip().lower()
    return l if l in LANGS else DEFAULT_LANG


def set_user(uid: int, role=None, depts=None, status=None, lang=None):
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
    if lang is not None:
        w.update_cell(cell.row, 6, lang)
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


# ---------------- ГРУППЫ И ДОКУМЕНТЫ ----------------

def group_map(chat_id: int):
    for r in rows(GROUPS_WS):
        if str(r.get("ChatID")).strip() == str(chat_id):
            loc = str(r.get("Локаль")).strip()
            typ = str(r.get("Тип")).strip()
            if loc in LOCALES and typ in DOCTYPES:
                return loc, typ
    return None


def save_doc(loc, typ, author, chat_id, msg_id, file_id, text) -> int:
    now = datetime.now()
    w = ws(DOCS_WS)
    w.append_row([
        now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
        loc, typ, author, str(chat_id), str(msg_id),
        file_id or "", (text or "")[:2000], "новый",
    ], value_input_option="RAW")
    drop_cache(DOCS_WS)
    return len(w.col_values(1))


def set_doc_status(row_idx: int, status: str):
    ws(DOCS_WS).update_cell(row_idx, 10, status)
    drop_cache(DOCS_WS)


def pending_docs():
    return [(i, r) for i, r in enumerate(rows(DOCS_WS, force=True), start=2)
            if str(r.get("Статус")).strip() == "новый"]


def today_stats():
    """{локаль: {тип: количество}} за сегодня."""
    today = datetime.now().strftime("%d.%m.%Y")
    stats = {loc: {typ: 0 for typ in DOCTYPES} for loc in LOCALES}
    for r in rows(DOCS_WS, force=True):
        if str(r.get("Дата")).strip() != today:
            continue
        loc, typ = str(r.get("Локаль")).strip(), str(r.get("Тип")).strip()
        if loc in stats and typ in stats[loc]:
            stats[loc][typ] += 1
    return stats


# ---------------- МЕНЮ ----------------

def _f(v):
    """Число из ячейки: терпит запятую и пробелы."""
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not s or s.lower() in ("none", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def menu_rows(loc: str):
    out = []
    for r in rows(MENU_WS):
        locs = str(r.get("Локаль", "")).replace(" ", "").lower()
        if locs and locs != "all" and loc not in locs.split(","):
            continue
        if not str(r.get("Блюдо", "")).strip():
            continue
        out.append(r)
    return out


def menu_groups(loc: str):
    seen, out = set(), []
    for r in menu_rows(loc):
        g = str(r.get("Группа", "")).strip()
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def menu_subs(loc: str, group: str):
    seen, out = set(), []
    for r in menu_rows(loc):
        if str(r.get("Группа", "")).strip() != group:
            continue
        s = str(r.get("Подгруппа", "")).strip()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def menu_dishes(loc: str, group: str, sub: str):
    return [r for r in menu_rows(loc)
            if str(r.get("Группа", "")).strip() == group
            and str(r.get("Подгруппа", "")).strip() == sub]


def tech_card(dish: str):
    """Строки техкарты по названию блюда (без учёта регистра и пробелов)."""
    key = " ".join(str(dish).split()).lower()
    out = [r for r in rows(TECH_WS)
           if " ".join(str(r.get("Блюдо", "")).split()).lower() == key]
    def k(r):
        try:
            return int(r.get("№") or 0)
        except (TypeError, ValueError):
            return 0
    return sorted(out, key=k)


# --- реестр коротких ключей: callback_data ограничен 64 байтами ---
_keys, _rev = {}, {}


def kkey(name: str) -> str:
    n = " ".join(str(name).split())
    if n in _rev:
        return _rev[n]
    k = f"k{len(_keys) + 1}"
    _keys[k], _rev[n] = n, k
    return k


def kname(k: str):
    return _keys.get(k)


def has_card(name: str) -> bool:
    return bool(tech_card(name))


# --- фото блюд ---

def dish_photo(dish: str):
    key = " ".join(str(dish).split()).lower()
    for r in reversed(rows(PHOTOS_WS)):
        if " ".join(str(r.get("Блюдо", "")).split()).lower() == key:
            fid = str(r.get("FileID") or "").strip()
            if fid:
                return fid
    return None


def save_photo(dish: str, file_id: str, who: str):
    ws(PHOTOS_WS).append_row(
        [dish, file_id, who, datetime.now().strftime("%d.%m.%Y %H:%M")],
        value_input_option="RAW")
    drop_cache(PHOTOS_WS)


# кто сейчас загружает фото: uid -> название блюда
_awaiting_photo = {}


def nc(v, nd=2) -> str:
    """Число с запятой, без хвостовых нулей где не нужно."""
    s = f"{v:.{nd}f}"
    return s.replace(".", ",")


def amount(unit: str, br, net) -> str:
    """Количество в формате BoiBoi: 40 g, 0,300 kg, 1 uds."""
    u = str(unit or "").strip().lower()
    v = net if (net is not None and net > 0) else br
    if v is None:
        return "—"
    if u in ("кг", "kg"):
        return f"{v * 1000:.0f} g" if v < 1 else f"{nc(v, 3)} kg"
    if u in ("л", "l"):
        v2 = br if br else v
        return f"{v2 * 1000:.0f} ml" if v2 < 1 else f"{nc(v2, 3)} l"
    v2 = br if br else v
    return f"{nc(v2, 3)} uds"


def render_card(dish: str, sub: str, menu_row, lang: str, emoji: str = "🍽"):
    """Возвращает (строки текста, список ПФ с собственными картами)."""
    card = tech_card(dish)
    lines = [f"{emoji} <b>{dish}</b>"]
    pf = []

    num = ""
    if card:
        num = str(card[0].get("Карта №") or "").strip()
        if num.isdigit():
            num = num.zfill(5)
    head_line = " · ".join([x for x in (sub, f"ТК № {num}" if num else "") if x])
    if head_line:
        lines.append(f"<i>{head_line}</i>")

    if card:
        lines.append("")
        lines.append("<b>СОСТАВ</b>")
        for row in card:
            ing = str(row.get("Ингредиент") or "").strip()
            a = amount(row.get("Ед"), _f(row.get("Брутто")), _f(row.get("Нетто")))
            lines.append(f"▸ {ing} — {a}")
            if has_card(ing):
                pf.append(ing)
        total = _f(card[0].get("Итого вес, кг"))
        out_s = str(card[0].get("На выход") or "").strip()
        tail = " · ".join([x for x in (out_s, f"{nc(total, 3)} kg" if total else "") if x])
        if tail:
            lines.append("")
            lines.append(f"<b>ВЫХОД:</b> {tail}")

    if menu_row is not None:
        price, cost, fc = (_f(menu_row.get("Цена")), _f(menu_row.get("Себестоимость")),
                           _f(menu_row.get("ФК%")))
        parts = []
        if fc:
            parts.append(f"{'🔴' if fc > FC_LIMIT else '🟢'} Food cost {fc:.0f}%")
        if cost is not None:
            parts.append(f"coste {nc(cost)} €")
        if price:
            parts.append(f"precio {nc(price)} €")
        if price and cost is not None:
            parts.append(f"margen {nc(price - cost)} €")
        if parts:
            lines.append("")
            lines.append(" · ".join(parts))

    if not card:
        lines.append("")
        lines.append(f"<i>{t('tech_none', lang)}</i>")
    return lines, pf


def menu_row_for(dish: str):
    key = " ".join(str(dish).split()).lower()
    for r in rows(MENU_WS):
        if " ".join(str(r.get("Блюдо", "")).split()).lower() == key:
            return r
    return None


def fc_report(loc: str):
    """Худшие блюда по фуд косту + проблемные записи."""
    worst, no_card, zero_price = [], 0, 0
    for r in menu_rows(loc):
        price, cost, fc = _f(r.get("Цена")), _f(r.get("Себестоимость")), _f(r.get("ФК%"))
        if cost is None:
            no_card += 1
            continue
        if not price:
            zero_price += 1
            continue
        if fc:
            worst.append((fc, str(r.get("Блюдо")).strip(), price, cost,
                          str(r.get("Группа")).strip()))
    worst.sort(reverse=True)
    return worst, no_card, zero_price


# ---------------- БОТ ----------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


CAPTION_LIMIT = 1000


async def take_over(event, text: str, kb: InlineKeyboardMarkup | None = None,
                    photo: str | None = None):
    """
    Один экран на весь диалог. Если у экрана есть фото — сообщение
    превращается в фото с подписью, если нет — обратно в текст.
    """
    if not isinstance(event, CallbackQuery):
        if photo:
            await event.answer_photo(photo, caption=text[:CAPTION_LIMIT], reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)
        return

    msg = event.message
    chat_id = msg.chat.id
    is_media = bool(msg.photo)

    try:
        if photo and is_media:
            await msg.edit_media(
                InputMediaPhoto(media=photo, caption=text[:CAPTION_LIMIT],
                                parse_mode=ParseMode.HTML),
                reply_markup=kb)
            return
        if not photo and not is_media:
            await msg.edit_text(text, reply_markup=kb)
            return
    except Exception as e:
        log.debug("edit failed: %s", e)

    # тип сообщения меняется — пересоздаём, старое убираем
    try:
        await msg.delete()
    except Exception:
        pass
    if photo:
        await bot.send_photo(chat_id, photo, caption=text[:CAPTION_LIMIT], reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)


def kb_home(u) -> InlineKeyboardMarkup:
    lang = ulang(u)
    kb = [[
        InlineKeyboardButton(text="🇪🇸 ES" if lang != "es" else "· ES ·", callback_data="lang:es"),
        InlineKeyboardButton(text="🇷🇺 RU" if lang != "ru" else "· RU ·", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 EN" if lang != "en" else "· EN ·", callback_data="lang:en"),
    ]]
    for code, d in DEPTS.items():
        kb.append([InlineKeyboardButton(text=f"{d['emoji']} {dept_name(code, lang)}",
                                        callback_data=f"d:{code}")])
    if is_patron(u):
        kb.append([InlineKeyboardButton(text=t("staff", lang), callback_data="staff")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def home_text(u) -> str:
    lang = ulang(u)
    return f"<b>{COMPANY}</b>\n\n{t('choose_dept', lang)}"


def crumb(dept: str, lang: str, loc: str = None) -> str:
    if dept == "menu":
        s = f"{MENU_EMOJI} <b>{dept_name('menu', lang)}</b>"
    else:
        s = f"{DEPTS[dept]['emoji']} <b>{dept_name(dept, lang)}</b>"
    if loc:
        s += f"\n{LOCALES[loc]['emoji']} {LOCALES[loc]['name']}"
    return s


def sections(dept: str, loc: str, lang: str) -> list:
    out = []
    for r in rows(CONTENT_WS):
        if str(r.get("Отдел")).strip() != dept:
            continue
        if str(r.get("Локаль")).strip() != loc:
            continue
        if not str(r.get("Раздел")).strip():
            continue
        rl = str(r.get("Язык", "")).strip().lower()
        if rl and rl != lang:
            continue
        out.append(r)
    def key(r):
        try:
            return int(r.get("Порядок") or 999)
        except (TypeError, ValueError):
            return 999
    return sorted(out, key=key)


# ---------------- ПРИЁМ ИЗ ГРУПП ----------------

@dp.my_chat_member()
async def on_added(ev: ChatMemberUpdated):
    if ev.chat.type not in ("group", "supergroup"):
        return
    if ev.new_chat_member.status in ("member", "administrator"):
        try:
            await bot.send_message(
                ev.chat.id,
                f"Бот подключён.\nChatID: <code>{ev.chat.id}</code>\n"
                f"Название: {ev.chat.title}\n\n"
                f"Внесите ChatID в лист Groups, чтобы начать приём документов.")
        except Exception as e:
            log.warning("greet failed: %s", e)
        for p in patrons():
            try:
                await bot.send_message(p, f"➕ Бот добавлен в группу\n<b>{ev.chat.title}</b>\n"
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

    file_id, text, is_photo = None, None, False
    if m.photo:
        file_id, text, is_photo = m.photo[-1].file_id, m.caption, True
    elif m.document:
        file_id, text = m.document.file_id, m.caption
    elif m.text:
        text = m.text
    else:
        return

    author = m.from_user.full_name if m.from_user else "—"
    save_doc(loc, typ, author, m.chat.id, m.message_id, file_id, text)


# ---------------- КОЛБЭКИ ----------------

def guard(c: CallbackQuery):
    u = get_user(c.from_user.id)
    return u if u and u.get("Статус") == "active" else None


@dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = c.data.split(":")[1]
    if lang not in LANGS:
        await c.answer()
        return
    set_user(c.from_user.id, lang=lang)
    u = get_user(c.from_user.id)
    await take_over(c, home_text(u), kb_home(u))
    await c.answer(t("lang_set", lang))


@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    await take_over(c, home_text(u), kb_home(u))
    await c.answer()


@dp.callback_query(F.data.startswith("d:"))
async def cb_dept(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    dept = c.data.split(":", 1)[1]
    if dept not in DEPTS:
        await c.answer()
        return
    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:{dept}:{code}")]
          for code, l in LOCALES.items()]
    if dept == "stock":
        kb.insert(0, [InlineKeyboardButton(
            text=f"{MENU_EMOJI} {dept_name('menu', lang)}", callback_data="menu")])
        if is_patron(u):
            kb.insert(0, [InlineKeyboardButton(text=t("archive", lang), callback_data="arch")])
            n = len(pending_docs())
            kb.insert(0, [InlineKeyboardButton(text=f"{t('queue', lang)} ({n})", callback_data="q")])
            kb.insert(0, [InlineKeyboardButton(text=t("today", lang), callback_data="today")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="home")])
    await take_over(c, f"{crumb(dept, lang)}\n\n{t('choose_locale', lang)}",
                    InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data == "menu")
async def cb_menu_root(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:menu:{code}")]
          for code, l in LOCALES.items()]
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="d:stock")])
    await take_over(c, f"{crumb('menu', lang)}\n\n{t('choose_locale', lang)}",
                    InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("l:"))
async def cb_loc(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    _, dept, loc = c.data.split(":")
    if (dept not in DEPTS and dept != "menu") or loc not in LOCALES:
        await c.answer()
        return

    if dept == "menu":
        groups = menu_groups(loc)
        if not groups:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t("back", lang), callback_data="menu")]])
            await take_over(c, f"{crumb(dept, lang, loc)}\n\n<i>{t('empty', lang)}</i>", kb)
            await c.answer()
            return
        kb = []
        if is_patron(u):
            kb.append([InlineKeyboardButton(text=t("fc_btn", lang), callback_data=f"fc:{loc}")])
        for i, g in enumerate(groups):
            n = len([r for r in menu_rows(loc) if str(r.get("Группа", "")).strip() == g])
            kb.append([InlineKeyboardButton(text=f"{g}  ·  {n}", callback_data=f"mg:{loc}:{i}")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="menu")])
        await take_over(c, f"{crumb(dept, lang, loc)}\n\n{t('choose_item', lang)}",
                        InlineKeyboardMarkup(inline_keyboard=kb))
        await c.answer()
        return

    items = sections(dept, loc, lang)
    if not items:
        text = f"{crumb(dept, lang, loc)}\n\n<i>{t('empty', lang)}</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("back", lang), callback_data=f"d:{dept}")]])
    else:
        text = f"{crumb(dept, lang, loc)}\n\n{t('choose_item', lang)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=str(r["Раздел"])[:60], callback_data=f"s:{dept}:{loc}:{i}")]
              for i, r in enumerate(items)],
            [InlineKeyboardButton(text=t("back", lang), callback_data=f"d:{dept}")]])
    await take_over(c, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_section(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    _, dept, loc, idx = c.data.split(":")
    items = sections(dept, loc, lang)
    try:
        r = items[int(idx)]
    except (ValueError, IndexError):
        await c.answer("—", show_alert=True)
        return
    body = str(r.get("Текст") or "").strip() or "—"
    text = f"{crumb(dept, lang, loc)}\n\n<b>{r.get('Раздел')}</b>\n\n{body}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("back", lang), callback_data=f"l:{dept}:{loc}")],
        [InlineKeyboardButton(text=t("home", lang), callback_data="home")]])
    await take_over(c, text[:4000], kb)
    await c.answer()


@dp.callback_query(F.data.startswith("mg:"))
async def cb_menu_group(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    _, loc, gi = c.data.split(":")
    groups = menu_groups(loc)
    try:
        g = groups[int(gi)]
    except (ValueError, IndexError):
        await c.answer()
        return
    subs = menu_subs(loc, g)
    kb = []
    for i, s in enumerate(subs):
        n = len(menu_dishes(loc, g, s))
        label = s if s else "—"
        kb.append([InlineKeyboardButton(text=f"{label[:45]}  ·  {n}",
                                        callback_data=f"ms:{loc}:{gi}:{i}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data=f"l:menu:{loc}")])
    head = f"{LOCALES[loc]['emoji']} {LOCALES[loc]['name']}\n🍽 <b>{g}</b>"
    await take_over(c, head, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("ms:"))
async def cb_menu_sub(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    _, loc, gi, si = c.data.split(":")
    groups = menu_groups(loc)
    try:
        g = groups[int(gi)]
        s = menu_subs(loc, g)[int(si)]
    except (ValueError, IndexError):
        await c.answer()
        return
    dishes = menu_dishes(loc, g, s)
    kb = []
    for i, r in enumerate(dishes[:80]):
        price = _f(r.get("Цена"))
        fc = _f(r.get("ФК%"))
        mark = " ⚠️" if (fc and fc > FC_LIMIT) else ""
        p = f"{price:.2f}€" if price else "—"
        kb.append([InlineKeyboardButton(
            text=f"{str(r.get('Блюдо'))[:34]}  {p}{mark}",
            callback_data=f"md:{loc}:{gi}:{si}:{i}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data=f"mg:{loc}:{gi}")])
    head = f"🍽 {g}\n<b>{s or '—'}</b>  ·  {len(dishes)} {t('dishes', lang)}"
    await take_over(c, head, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("md:"))
async def cb_menu_dish(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    _, loc, gi, si, di = c.data.split(":")
    try:
        g = menu_groups(loc)[int(gi)]
        s = menu_subs(loc, g)[int(si)]
        r = menu_dishes(loc, g, s)[int(di)]
    except (ValueError, IndexError):
        await c.answer()
        return
    dish = str(r.get("Блюдо"))
    lines, pf = render_card(dish, s, r, lang, LOCALES[loc]["emoji"])

    kb_rows = [[InlineKeyboardButton(text=f"📋 {n[:50]}", callback_data=f"tk:{kkey(n)}:-")]
               for n in pf]
    if u.get("Роль") in (ROLE_PATRON, ROLE_MANAGER):
        kb_rows.append([InlineKeyboardButton(
            text=t("photo_add", lang), callback_data=f"pha:{kkey(dish)}")])
    kb_rows.append([InlineKeyboardButton(text=t("back", lang), callback_data=f"ms:{loc}:{gi}:{si}")])
    kb_rows.append([InlineKeyboardButton(text=t("home", lang), callback_data="home")])
    await take_over(c, "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=kb_rows),
                    photo=dish_photo(dish))
    await c.answer()


@dp.callback_query(F.data.startswith("pha:"))
async def cb_photo_add(c: CallbackQuery):
    u = guard(c)
    if not u or u.get("Роль") not in (ROLE_PATRON, ROLE_MANAGER):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    dish = kname(c.data.split(":")[1])
    if not dish:
        await c.answer("Откройте блюдо заново", show_alert=True)
        return
    _awaiting_photo[c.from_user.id] = dish
    await c.answer(t("photo_wait", lang), show_alert=True)


@dp.message(F.photo, F.chat.type == "private")
async def on_private_photo(m: Message):
    dish = _awaiting_photo.pop(m.from_user.id, None)
    if not dish:
        return
    save_photo(dish, m.photo[-1].file_id, m.from_user.full_name)
    try:
        await m.delete()          # не копим фото в чате
    except Exception:
        pass
    u = get_user(m.from_user.id)
    note = await m.answer(f"{t('photo_saved', ulang(u))} <b>{dish}</b>")
    await asyncio.sleep(3)
    try:
        await note.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("tk:"))
async def cb_tech(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    parts = c.data.split(":")
    dish, parent = kname(parts[1]), parts[2]
    if not dish:
        await c.answer("Откройте заново", show_alert=True)
        return
    card = tech_card(dish)
    if not card:
        await c.answer(t("tech_none", lang), show_alert=True)
        return
    lines, pf = render_card(dish, "", menu_row_for(dish), lang, "📋")
    kb_rows = [[InlineKeyboardButton(text=f"📋 {n[:50]}", callback_data=f"tk:{kkey(n)}:{parts[1]}")]
               for n in pf]
    if u.get("Роль") in (ROLE_PATRON, ROLE_MANAGER):
        kb_rows.append([InlineKeyboardButton(
            text=t("photo_add", lang), callback_data=f"pha:{kkey(dish)}")])
    if parent and parent != "-":
        kb_rows.append([InlineKeyboardButton(
            text=t("back", lang), callback_data=f"tk:{parent}:-")])
    kb_rows.append([InlineKeyboardButton(text=t("home", lang), callback_data="home")])
    await take_over(c, "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=kb_rows),
                    photo=dish_photo(dish))
    await c.answer()


@dp.callback_query(F.data.startswith("fc:"))
async def cb_foodcost(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    loc = c.data.split(":")[1]
    worst, no_card, zero_price = fc_report(loc)
    if not worst:
        text = t("fc_none", lang)
    else:
        lines = [f"📊 <b>{t('fc_title', lang)}</b> · {LOCALES[loc]['name']}",
                 f"<i>порог {FC_LIMIT:.0f}%</i>\n"]
        for fc, name, price, cost, g in worst[:20]:
            lines.append(f"🔴 <b>{fc:5.1f}%</b>  {name[:32]}\n"
                         f"      {price:.2f} € → {cost:.2f} €  ·  {g}")
        over = len([1 for w in worst if w[0] > FC_LIMIT])
        lines.append(f"\nВыше порога: <b>{over}</b> из {len(worst)}")
        lines.append(f"{t('no_card', lang)}: {no_card}  ·  {t('zero_price', lang)}: {zero_price}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("refresh", lang), callback_data=f"fc:{loc}")],
        [InlineKeyboardButton(text=t("back", lang), callback_data=f"l:menu:{loc}")]])
    await take_over(c, text[:4000], kb)
    await c.answer()


@dp.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    stats = today_stats()
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"📊 <b>{t('today_title', lang)}</b> · {today}\n"]
    total = 0
    for code, L in LOCALES.items():
        s = stats[code]
        total += sum(s.values())
        parts = [f"{DOCTYPES[typ]['emoji']} {s[typ]}" for typ in DOCTYPES]
        mark = "" if sum(s.values()) else "  ⚠️"
        lines.append(f"{L['emoji']} <b>{L['name']}</b>   {'  '.join(parts)}{mark}")
    lines.append("")
    lines.append("  ".join(f"{DOCTYPES[typ]['emoji']} {doc_name(typ, lang)}" for typ in DOCTYPES))
    if not total:
        lines.append(f"\n<i>{t('nothing_today', lang)}</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("refresh", lang), callback_data="today")],
        [InlineKeyboardButton(text=t("back", lang), callback_data="d:stock")]])
    await take_over(c, "\n".join(lines)[:4000], kb)
    await c.answer()


@dp.callback_query(F.data == "q")
async def cb_queue(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    items = pending_docs()
    if not items:
        text = f"<b>{t('queue', lang)}</b>\n\n{t('queue_empty', lang)}"
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data="q")],
              [InlineKeyboardButton(text=t("back", lang), callback_data="d:stock")]]
    else:
        text = (f"<b>{t('queue', lang)}</b> — {len(items)}\n\n"
                f"<i>{t('queue_hint', lang)}</i>")
        kb = []
        for idx, r in items[:40]:
            loc = str(r.get("Локаль")).strip()
            typ = str(r.get("Тип")).strip()
            L = LOCALES.get(loc, {})
            label = (f"{DOCTYPES.get(typ, {}).get('emoji', '📄')} "
                     f"{L.get('emoji', '')} {L.get('name', loc)} · "
                     f"{r.get('Дата')} {r.get('Время')}")
            kb.append([InlineKeyboardButton(text=label[:60], callback_data=f"qd:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data="q")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="d:stock")])
    await take_over(c, text[:4000], InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("qd:"))
async def cb_queue_doc(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    idx = int(c.data.split(":")[1])
    data = rows(DOCS_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    loc = str(r.get("Локаль")).strip()
    typ = str(r.get("Тип")).strip()
    L = LOCALES.get(loc, {})
    done = str(r.get("Статус")).strip() != "новый"
    cap = (f"{DOCTYPES.get(typ, {}).get('emoji', '📄')} "
           f"<b>{doc_name(typ, lang) if typ in DOCTYPES else typ}</b>"
           f" · {L.get('emoji', '')} {L.get('name', loc)}\n"
           f"{t('from', lang)}: {r.get('Автор')}\n{r.get('Дата')} {r.get('Время')}")
    txt = str(r.get("Текст") or "").strip()
    if txt:
        cap += f"\n\n{txt[:600]}"
    kb = []
    if not done:
        kb.append([InlineKeyboardButton(text=t("sent_btn", lang), callback_data=f"sent:{idx}")])
    kb.append([InlineKeyboardButton(text=t("back", lang),
                                    callback_data="q" if not done else "arch")])
    fid = str(r.get("FileID") or "").strip() or None
    await take_over(c, cap, InlineKeyboardMarkup(inline_keyboard=kb), photo=fid)
    await c.answer()


@dp.callback_query(F.data == "arch")
async def cb_archive(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    done = [(i, r) for i, r in enumerate(rows(DOCS_WS, force=True), start=2)
            if str(r.get("Статус")).strip() and str(r.get("Статус")).strip() != "новый"]
    done.reverse()
    if not done:
        text = f"<b>{t('archive', lang)}</b>\n\n—"
        kb = []
    else:
        text = f"<b>{t('archive', lang)}</b> — {len(done)}"
        kb = []
        for idx, r in done[:40]:
            loc = str(r.get("Локаль")).strip()
            typ = str(r.get("Тип")).strip()
            L = LOCALES.get(loc, {})
            label = (f"{DOCTYPES.get(typ, {}).get('emoji', '📄')} "
                     f"{L.get('emoji', '')} {L.get('name', loc)} · "
                     f"{r.get('Дата')} {r.get('Время')}")
            kb.append([InlineKeyboardButton(text=label[:60], callback_data=f"qd:{idx}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="d:stock")])
    await take_over(c, text[:4000], InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("sent:"))
async def cb_sent(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    set_doc_status(int(c.data.split(":")[1]), "отправлено")
    await c.answer(t("marked", lang))
    c.data = "q"
    await cb_queue(c)


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def cb_approve(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    action, uid = c.data.split(":", 1)
    if action == "no":
        set_user(int(uid), status="denied")
        await take_over(c, "❌")
    else:
        set_user(int(uid), role=ROLE_STAFF, depts="all", status="active", lang=DEFAULT_LANG)
        await take_over(c, f"✅ <code>{uid}</code>")
        try:
            await bot.send_message(int(uid), t("granted", DEFAULT_LANG))
        except Exception:
            pass
    await c.answer()


@dp.callback_query(F.data == "staff")
async def cb_staff(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    lines = [f"<b>{t('staff', lang)}</b>\n"]
    for r in rows(USERS_WS, force=True):
        lines.append(f"• {r.get('Имя')} — {role_name(r.get('Роль'), lang)} · {r.get('Статус')}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("back", lang), callback_data="home")]])
    await take_over(c, "\n".join(lines)[:4000], kb)
    await c.answer()


# ---------------- СТАРТ ----------------

@dp.message(CommandStart())
async def start(m: Message):
    if m.chat.type != "private":
        return
    uid, name = m.from_user.id, m.from_user.full_name
    u = get_user(uid)

    if not rows(USERS_WS, force=True):
        ws(USERS_WS).append_row([str(uid), name, ROLE_PATRON, "all", "active", DEFAULT_LANG])
        drop_cache(USERS_WS)
        u = get_user(uid)
        await m.answer(f"<b>{COMPANY}</b>", reply_markup=kb_home(u))
        return

    if not u:
        ws(USERS_WS).append_row([str(uid), name, "", "", "pending", DEFAULT_LANG])
        drop_cache(USERS_WS)
        await m.answer(t("req_sent", DEFAULT_LANG))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅", callback_data=f"ok:{uid}"),
            InlineKeyboardButton(text="❌", callback_data=f"no:{uid}"),
        ]])
        for p in patrons():
            try:
                await bot.send_message(p, f"🔔 <b>{name}</b>\nID: <code>{uid}</code>", reply_markup=kb)
            except Exception:
                pass
        return

    if u.get("Статус") != "active":
        await m.answer(t("req_wait", ulang(u)))
        return

    await m.answer(home_text(u), reply_markup=kb_home(u))


@dp.message(Command("diag"))
async def cmd_diag(m: Message):
    if not is_patron(get_user(m.from_user.id)):
        return
    lines = ["<b>Диагностика</b>\n", "<b>Вкладки в таблице:</b>"]
    try:
        for w in _sh.worksheets():
            lines.append(f"• {w.title}")
    except Exception as e:
        lines.append(f"ошибка: {e}")
    lines.append("")
    for nm in (MENU_WS, TECH_WS, DOCS_WS, GROUPS_WS, PHOTOS_WS):
        try:
            w = ws(nm)
            lines.append(f"<b>{nm}</b> → читаю «{w.title}», строк: {len(rows(nm, force=True))}")
        except Exception as e:
            lines.append(f"<b>{nm}</b> → ошибка: {e}")
    try:
        sample = [r for r in rows(MENU_WS) if "Cruasán Clasico" in str(r.get("Блюдо", ""))]
        if sample:
            r = sample[0]
            lines.append(f"\n<b>Проверка числа</b>\n{r.get('Блюдо')}\n"
                         f"Цена: {r.get('Цена')}  (должно 2,9)\n"
                         f"ФК%: {r.get('ФК%')}  (должно 56,6)")
    except Exception as e:
        lines.append(f"проверка числа: {e}")
    await m.answer("\n".join(lines)[:4000])


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"<code>{m.from_user.id}</code>")


async def main():
    ensure_headers()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
