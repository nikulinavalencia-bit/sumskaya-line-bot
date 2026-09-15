# =========================================================
#  SUMSKAYA LINE SL — корпоративный бот
#  v0.4 — 3 языка + статус за сегодня + приём из групп
# =========================================================

import os
import json
import asyncio
import logging
import requests
import base64
import re
from time import time
import time as time_module
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")


def now_local():
    return datetime.now(TZ)

import gspread
from google.oauth2.service_account import Credentials

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InputMediaPhoto,
    InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent, BotCommand,
    BufferedInputFile,
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

# Почта — пересылка фактур и списаний в Билз, через Brevo (HTTP API, т.к.
# Railway блокирует прямые SMTP-подключения — Errno 101 Network unreachable).
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
SMTP_USER = os.environ.get("SMTP_USER", "")          # адрес-отправитель (должен быть подтверждён в Brevo)
BILLZ_EMAIL = os.environ.get("BILLZ_EMAIL", "sa@bilz.ai")
APPLICANTS_SHEET_ID = os.environ.get("APPLICANTS_SHEET_ID", "1WvQz7v3hu31Rw4JSXeQXszK4RY9t5FYSDo2ok1CrTF4")
REGISTRO_SHEET_ID = os.environ.get("REGISTRO_SHEET_ID", "1JRjfjCH4_LDuU78EqIbjbCKWXKvx3D13LUA84o4uVds")
REGISTRO_WS_NAME = os.environ.get("REGISTRO_WS_NAME", "Registro")

# Gmail API — читаем почту sl.valencia.resta@gmail.com на предмет готовых
# контрактов/баха/камбио от Histora (по имени вложения).
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_ATTACHMENT_KEYWORDS = ("CONTRATO", "BAJA", "CAMBIO")
GMAIL_CHECK_INTERVAL = 15 * 60  # секунд
EMPLEADOS_CHAT_ID = os.environ.get("EMPLEADOS_CHAT_ID", "")
SIGNING_HOURS = "11:00–16:00"

# Ключевые фразы для автоматического распознавания типа заявки из группы
# Empleados — без хэштегов, по смыслу текста сообщения.
EMPLEADOS_CATEGORY_KEYWORDS = {
    "Baja":      ["ultimo dia de trabajo", "último día de trabajo", "despido",
                  "baja voluntaria", "finiquito", "causa baja"],
    "Alta":      ["alta de su baja", "alta medica", "alta médica",
                  "se reincorpora", "vuelve al trabajo", "fin de baja"],
    "Jornada":   ["cambio de jornada", "jornada", "horario", "cambio de horas"],
    "Categoria": ["categoria", "categoría", "cambio de puesto", "nuevo puesto"],
    "Contrato":  ["contrato"],
}
EMPLEADOS_CATEGORY_EMOJI = {
    "Baja": "🔴", "Alta": "🟢", "Jornada": "🕐", "Categoria": "🔁", "Contrato": "📄",
}


def classify_empleados_text(text: str) -> str:
    t = _norm(text or "")
    if not t:
        return ""
    scores = {}
    for cat, kws in EMPLEADOS_CATEGORY_KEYWORDS.items():
        for kw in kws:
            if _norm(kw) in t:
                scores[cat] = scores.get(cat, 0) + 1
    return max(scores, key=scores.get) if scores else ""
APPLICANTS_WS_NAME = os.environ.get("APPLICANTS_WS_NAME", "Form_Responses")

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
    "invoices":    {"es": "Facturas", "ru": "Фактуры", "en": "Invoices"},
    "writeoffs":   {"es": "Bajas", "ru": "Списания", "en": "Write-offs"},
    "queue_empty": {"es": "Todo procesado.", "ru": "Всё обработано.", "en": "All processed."},
    "queue_hint":  {"es": "Pulsa para abrir y reenviar.", "ru": "Нажмите, чтобы открыть и переслать.", "en": "Tap to open and forward."},
    "archive":     {"es": "🗄 Archivo", "ru": "🗄 Архив", "en": "🗄 Archive"},
    "arch_put":    {"es": "🗄 Al archivo", "ru": "🗄 Убрать в архив", "en": "🗄 Archive dish"},
    "arch_done":   {"es": "Plato archivado.", "ru": "Блюдо убрано в архив.", "en": "Dish archived."},
    "arch_back":   {"es": "Devuelto a la carta.", "ru": "Возвращено в меню.", "en": "Restored to menu."},
    "arch_empty":  {"es": "El archivo está vacío.", "ru": "Архив пуст.", "en": "Archive is empty."},
    "arch_hint":   {"es": "Pulsa para devolver a la carta.", "ru": "Нажмите, чтобы вернуть в меню.", "en": "Tap to restore."},
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


def _open_sheet_with_retry(gc, sheet_id, attempts=5, delay=20):
    """При старте иногда ловим 429 (лимит Google API) из-за частых рестартов —
    пробуем ещё раз вместо мгновенного краша всего процесса."""
    last_exc = None
    for i in range(attempts):
        try:
            return gc.open_by_key(sheet_id)
        except gspread.exceptions.APIError as e:
            last_exc = e
            log.warning("Sheets API недоступен при старте (попытка %s/%s): %s",
                        i + 1, attempts, e)
            time_module.sleep(delay * (i + 1))
    raise last_exc


_sh = _open_sheet_with_retry(_gc, SHEET_ID)


def sheets_write_retry(fn, *args, attempts=3, delay=8, **kwargs):
    """Оборачивает функцию записи в таблицу — при 429 (лимит) ждёт и
    повторяет, вместо того чтобы ронять всю пачку обработки."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" not in str(e):
                raise
            last_exc = e
            log.warning("Sheets API 429 при записи (попытка %s/%s): %s", i + 1, attempts, e)
            time_module.sleep(delay * (i + 1))
    raise last_exc

USERS_WS = "Users"
CONTENT_WS = "Content"
GROUPS_WS = "Groups"
DOCS_WS = "Docs"
MENU_WS = "Menu"
TECH_WS = "TechCards"
PHOTOS_WS = "Photos"
ARCHIVE_WS = "Archive"
HR_WS = "HR"
MAIL_WS = "MailContracts"
EMPLOYEES_WS = "Employees"
EMPLEADOS_REQ_WS = "EmpleadosRequests"

HEADERS = {
    USERS_WS:   ["ID", "Имя", "Роль", "Отделы", "Статус", "Язык"],
    CONTENT_WS: ["Отдел", "Локаль", "Порядок", "Раздел", "Текст", "Язык"],
    GROUPS_WS:  ["ChatID", "Название группы", "Локаль", "Тип"],
    DOCS_WS:    ["Дата", "Время", "Локаль", "Тип", "Автор",
                 "ChatID", "MessageID", "FileID", "Текст", "Статус", "Письмо"],
    MENU_WS:    ["Локаль", "Группа", "Подгруппа", "Блюдо", "Ед",
                 "Цена", "Себестоимость", "ФК%"],
    TECH_WS:    ["Блюдо", "Карта №", "Дата", "№", "Ингредиент", "Ед",
                 "Брутто", "Нетто", "Итого вес, кг", "На выход"],
    PHOTOS_WS:  ["Блюдо", "FileID", "Кто добавил", "Дата"],
    ARCHIVE_WS: ["Блюдо", "Кто убрал", "Дата"],
    HR_WS:      ["RowKey", "ФИО", "Локаль", "Должность", "Дата заявки",
                 "Статус", "Обновил", "Дата обновления",
                 "Срок документа", "Разрешение на работу"],
    MAIL_WS:    ["MessageID", "Дата", "От кого", "Тема", "Вложение"],
    EMPLOYEES_WS: ["ФИО", "Локаль", "Должность", "Дата заявки",
                   "Тип файла", "Имя файла", "GmailMsgID", "Дата добавления"],
    EMPLEADOS_REQ_WS: ["Категория", "Текст", "Автор", "ChatID", "MessageID",
                        "FileID", "Дата", "Статус"],
}

_cache = {}
CACHE_TTL = 45


def _norm(s: str) -> str:
    return " ".join(str(s).split()).strip().lower()


_ws_cache = {}
_all_sheets_cache = {"ts": 0, "data": []}


def _all_sheets():
    """Список вкладок таблицы — кэшируем на минуту, чтобы не дёргать API
    по разу на каждый вызов ws() (раньше это было главной причиной
    перерасхода лимита Google при старте бота)."""
    if time() - _all_sheets_cache["ts"] < 60 and _all_sheets_cache["data"]:
        return _all_sheets_cache["data"]
    data = _sh.worksheets()
    _all_sheets_cache["ts"] = time()
    _all_sheets_cache["data"] = data
    return data


def ws(name: str):
    """
    Находит лист по имени, прощая мусор вокруг:
    лишние пробелы, регистр и суффиксы импорта — 'Menu 1', 'Menu (2)'.
    """
    if name in _ws_cache:
        return _ws_cache[name]
    target = _norm(name)
    sheets = _all_sheets()

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
    _all_sheets_cache["ts"] = 0  # список вкладок изменился — не доверяем кэшу
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


# ---------------- HR: заявки кандидатов + чек-лист оформления ----------------

_applicants_sh = None


def applicants_ws():
    global _applicants_sh
    if _applicants_sh is None:
        _applicants_sh = _gc.open_by_key(APPLICANTS_SHEET_ID)
    sheets = _applicants_sh.worksheets()
    target = _norm(APPLICANTS_WS_NAME)
    for w in sheets:
        if _norm(w.title) == target:
            return w
    # точного совпадения нет — берём первую вкладку таблицы
    log.warning("лист '%s' не найден в таблице заявок, использую '%s'",
                APPLICANTS_WS_NAME, sheets[0].title)
    return sheets[0]


_registro_sh = None


def registro_ws():
    global _registro_sh
    if _registro_sh is None:
        _registro_sh = _gc.open_by_key(REGISTRO_SHEET_ID)
    sheets = _registro_sh.worksheets()
    target = _norm(REGISTRO_WS_NAME)
    for w in sheets:
        if _norm(w.title) == target:
            return w
    log.warning("лист '%s' не найден в таблице Registro, использую '%s'",
                REGISTRO_WS_NAME, sheets[0].title)
    return sheets[0]


def registro_header_row(w) -> int:
    """Ищет реальную строку с названиями колонок среди первых 5 строк —
    у Registro сверху есть баннер и групповые заголовки, настоящие имена
    колонок не обязательно в самой первой строке."""
    expected = {"iban", "sex", "horas", "domicilio", "puesto", "telefono",
                "contrato", "departamento"}
    values = w.get_values("A1:Z6")
    best_row, best_score = 1, -1
    for i, row in enumerate(values, start=1):
        cells = {_norm(c) for c in row if c}
        score = sum(1 for e in expected if any(e in c for c in cells))
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def registro_append(values_by_header: dict):
    """Дописывает строку в Registro, сопоставляя значения с колонками ПО
    НАЗВАНИЮ (не по номеру) — устойчиво к тому, в каком порядке реально
    стоят колонки в таблице, и к тому, что настоящие заголовки не в первой
    строке (сверху баннер/групповые шапки)."""
    w = registro_ws()
    header_row = registro_header_row(w)
    headers = w.row_values(header_row)
    row = []
    unmatched = dict(values_by_header)
    for h in headers:
        h_norm = _norm(h)
        val = ""
        for key in list(unmatched.keys()):
            if _norm(key) == h_norm:
                val = unmatched.pop(key)
                break
        row.append(val)
    w.append_row(row, value_input_option="USER_ENTERED")
    if unmatched:
        log.warning("В Registro не нашлось колонок для: %s", list(unmatched.keys()))
    return unmatched


def applicants_rows(force=False):
    ts, data = _cache.get("applicants", (0, []))
    if force or time() - ts > CACHE_TTL:
        w = applicants_ws()
        values = w.get_all_values()
        if not values:
            data = []
        else:
            headers = values[0]
            seen = {}
            safe_headers = []
            for h in headers:
                h = h.strip()
                if h in seen:
                    seen[h] += 1
                    safe_headers.append(f"{h}_{seen[h]}")
                else:
                    seen[h] = 0
                    safe_headers.append(h)
            data = [dict(zip(safe_headers, row)) for row in values[1:] if any(row)]
        _cache["applicants"] = (time(), data)
    return data


HR_STAGES = [
    "Заявка получена",
    "Отправлено в Histora",
    "Контракт получен, отправлено на подпись",
    "Подписано",
    "Заведено в Control Laboral",
    "Активен",
]


def tracked_row_keys() -> set:
    return {str(r.get("RowKey")) for r in rows(HR_WS, force=True)}


def match_locale_code(text: str) -> str:
    """Сопоставляет текст локали из анкеты (P&S Reina, Boi Boi Gran Via,
    P&S Francia, P&S Bakery...) с нашим внутренним кодом локали."""
    t = _norm(text)
    if "reina" in t:
        return "reina"
    if "fransia" in t or "francia" in t:
        return "fransia"
    if "boi" in t:
        return "boiboi"
    if "panad" in t or "bakery" in t:
        return "panaderia"
    return ""


def new_applicants(loc: str = None):
    """Заявки из формы, ещё не добавленные в чек-лист HR. Можно отфильтровать
    по локали (код из LOCALES)."""
    tracked = tracked_row_keys()
    out = []
    for idx, r in enumerate(applicants_rows(), start=2):
        key = str(idx)
        if key in tracked:
            continue
        if loc and match_locale_code(r.get("Local", "")) != loc:
            continue
        out.append((idx, r))
    return out


def format_applicant_block(r: dict) -> str:
    nombre = str(r.get("Nombre", "")).strip()
    apellido = str(r.get("Apellido", "")).strip()
    puesto = str(r.get("Título profesional", "")).strip()
    horas = str(r.get("Número de horas bajo contrato", "")).strip()
    lines = [
        f"{nombre} {apellido}".strip(),
        str(r.get("Fecha de nacimiento", "")).strip(),
        str(r.get("NIE/TIE", "")).strip(),
        str(r.get("Domicilio", "")).strip(),
        str(r.get("Código postal", "")).strip(),
        str(r.get("IBAN", "")).strip(),
        str(r.get("Correo electrónico", "")).strip(),
        str(r.get("Teléfono", "")).strip(),
        str(r.get("Numero seguridad social", "")).strip(),
        f"{puesto} {horas}".strip(),
        str(r.get("Fecha de inicio", "")).strip(),
        str(r.get("Local", "")).strip(),
    ]
    return "\n".join(l for l in lines if l)


def applicant_doc_link(r: dict) -> str:
    for k, v in r.items():
        v = str(v).strip()
        if v.startswith("http") and "drive.google.com" in v:
            return v
    return ""


def _drive_file_id(link: str) -> str:
    m = re.search(r"(?:id=|/d/)([a-zA-Z0-9_-]{15,})", link)
    return m.group(1) if m else ""


def _download_drive_file_sync(link: str):
    file_id = _drive_file_id(link)
    if not file_id:
        return None
    try:
        r = requests.get(f"https://drive.google.com/uc?export=download&id={file_id}", timeout=20)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 200 and ctype.startswith(("image/", "application/octet-stream")):
            return r.content
    except Exception as e:
        log.error("Не удалось скачать файл с Drive: %s", e)
    return None


async def download_drive_file(link: str):
    return await asyncio.to_thread(_download_drive_file_sync, link)


def add_to_hr_checklist(row_idx: int, r: dict, author: str) -> int:
    now = now_local()
    fio = f"{r.get('Nombre', '')} {r.get('Apellido', '')}".strip()
    w = ws(HR_WS)
    w.append_row([
        str(row_idx), fio, str(r.get("Local", "")).strip(),
        str(r.get("Título profesional", "")).strip(),
        now.strftime("%d.%m.%Y"), HR_STAGES[0], author, now.strftime("%d.%m.%Y %H:%M"),
    ], value_input_option="RAW")
    drop_cache(HR_WS)
    return len(w.col_values(1))


def set_hr_stage(hr_row_idx: int, stage: str, author: str):
    now = now_local().strftime("%d.%m.%Y %H:%M")
    w = ws(HR_WS)
    w.update_cell(hr_row_idx, 6, stage)
    w.update_cell(hr_row_idx, 7, author)
    w.update_cell(hr_row_idx, 8, now)
    drop_cache(HR_WS)


def set_hr_doc_info(hr_row_idx: int, expiry_str: str, permit_ok: bool):
    w = ws(HR_WS)
    w.update_cell(hr_row_idx, 9, expiry_str)
    w.update_cell(hr_row_idx, 10, "да" if permit_ok else "нет")
    drop_cache(HR_WS)


def parse_ddmmyyyy(s: str):
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def hr_doc_warning(r: dict) -> str:
    """Возвращает пометку, если с документом что-то не так: истёк, скоро истекает,
    не указан срок, или явно нет разрешения на работу."""
    permit = str(r.get("Разрешение на работу", "")).strip().lower()
    if permit == "нет":
        return "🚫 нет разрешения на работу"
    expiry_raw = str(r.get("Срок документа", "")).strip()
    if not expiry_raw:
        return "❔ срок документа не указан"
    d = parse_ddmmyyyy(expiry_raw)
    if not d:
        return "❔ срок документа не распознан"
    today = now_local().date()
    if d < today:
        return f"⛔ документ просрочен ({expiry_raw})"
    if (d - today).days <= 30:
        return f"⚠️ истекает {expiry_raw}"
    return ""


# ---------------- GMAIL: контракты/baja/cambio от Histora ----------------

_gmail_access_token = {"token": "", "exp": 0}
_gmail_auth_status = {"broken": False, "notified": False}


def _gmail_get_access_token_sync() -> str:
    if _gmail_access_token["token"] and time() < _gmail_access_token["exp"] - 60:
        return _gmail_access_token["token"]
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        log.warning("Gmail: не заданы GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN — пропускаю")
        return ""
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }, timeout=20)
        if r.status_code != 200:
            log.error("Не удалось обновить Gmail-токен: %s %s", r.status_code, r.text[:300])
            _gmail_auth_status["broken"] = True
            return ""
        data = r.json()
        _gmail_access_token["token"] = data["access_token"]
        _gmail_access_token["exp"] = time() + int(data.get("expires_in", 3600))
        _gmail_auth_status["broken"] = False
        _gmail_auth_status["notified"] = False
        return _gmail_access_token["token"]
    except Exception as e:
        log.error("Ошибка получения Gmail-токена: %s", e)
        _gmail_auth_status["broken"] = True
        return ""


def _gmail_api_get_sync(path: str, params: dict = None):
    token = _gmail_get_access_token_sync()
    if not token:
        return None
    try:
        r = requests.get(f"https://gmail.googleapis.com/gmail/v1/users/me/{path}",
                          headers={"Authorization": f"Bearer {token}"},
                          params=params or {}, timeout=20)
        if r.status_code != 200:
            log.error("Gmail API ошибка %s на %s: %s", r.status_code, path, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        log.error("Ошибка запроса к Gmail API (%s): %s", path, e)
        return None


def _walk_gmail_parts(payload: dict):
    """Рекурсивно обходит части письма и возвращает список вложений
    (filename, attachmentId)."""
    out = []
    if not payload:
        return out
    filename = payload.get("filename")
    body = payload.get("body", {})
    if filename and body.get("attachmentId"):
        out.append((filename, body["attachmentId"]))
    for p in payload.get("parts", []):
        out.extend(_walk_gmail_parts(p))
    return out


def _gmail_search_all_messages(q: str, max_total: int = 200):
    """Собирает ID писем по запросу со всех страниц (Gmail отдаёт максимум
    ~100-500 за раз, а без постраничного обхода можно потерять письма,
    если их накопилось больше одной страницы)."""
    ids = []
    page_token = None
    while len(ids) < max_total:
        params = {"q": q, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        data = _gmail_api_get_sync("messages", params)
        if not data:
            break
        ids.extend(m["id"] for m in data.get("messages", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_total]


def _check_gmail_contracts_sync():
    """Синхронная (блокирующая) проверка почты — вызывается через to_thread."""
    seen = {str(r.get("MessageID")) for r in rows(MAIL_WS, force=True)}
    q = " OR ".join(f"filename:{kw}" for kw in GMAIL_ATTACHMENT_KEYWORDS)
    ids = _gmail_search_all_messages(q)
    found = []
    for mid in ids:
        if mid in seen:
            continue
        msg = _gmail_api_get_sync(f"messages/{mid}", {"format": "full"})
        if not msg:
            continue
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        attachments = _walk_gmail_parts(msg.get("payload", {}))
        matching = [(fn, aid) for fn, aid in attachments
                    if any(kw.lower() in fn.lower() for kw in GMAIL_ATTACHMENT_KEYWORDS)]
        if not matching:
            continue
        found.append({
            "id": mid, "from": headers.get("From", ""), "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""), "attachments": matching,
        })
    return found


async def check_gmail_contracts():
    return await asyncio.to_thread(_check_gmail_contracts_sync)


def _gmail_download_attachment_sync(msg_id: str, attachment_id: str):
    data = _gmail_api_get_sync(f"messages/{msg_id}/attachments/{attachment_id}")
    if not data or "data" not in data:
        return None
    import base64 as b64
    raw = data["data"].replace("-", "+").replace("_", "/")
    return b64.b64decode(raw)


async def gmail_download_attachment(msg_id: str, attachment_id: str):
    return await asyncio.to_thread(_gmail_download_attachment_sync, msg_id, attachment_id)


def mark_mail_seen(msg_id: str, date: str, sender: str, subject: str, attachment: str):
    ws(MAIL_WS).append_row([msg_id, date, sender, subject, attachment], value_input_option="RAW")
    drop_cache(MAIL_WS)


def extract_name_from_filename(filename: str) -> str:
    """'Contrato Mark 12 09 2026.pdf' -> 'mark'. Убирает расширение, ключевые
    слова (CONTRATO/BAJA/CAMBIO...) и числа/даты, оставляет только имя."""
    base = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", filename)
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", base)
    skip = {"contrato", "baja", "cambio", "voluntaria", "carta", "nspp", "jornada"}
    name_words = [w for w in words if w.lower() not in skip]
    return " ".join(name_words).strip()


def find_checklist_match(candidate_name: str):
    """Ищет в чек-листе HR запись, чьё ФИО совпадает по словам с именем
    из файла. Возвращает (row_idx, row) или (None, None)."""
    cand_words = set(_norm(candidate_name).split())
    if not cand_words:
        return None, None
    best, best_score = None, 0
    for idx, r in enumerate(rows(HR_WS, force=True), start=2):
        fio_words = set(_norm(str(r.get("ФИО", ""))).split())
        if not fio_words:
            continue
        score = len(cand_words & fio_words)
        if score > best_score and score >= 1:
            best, best_score = (idx, r), score
    return best if best else (None, None)


def add_employee_card(r: dict, file_type: str, filename: str, gmail_msg_id: str):
    now = now_local().strftime("%d.%m.%Y %H:%M")
    ws(EMPLOYEES_WS).append_row([
        str(r.get("ФИО", "")), str(r.get("Локаль", "")), str(r.get("Должность", "")),
        str(r.get("Дата заявки", "")), file_type, filename, gmail_msg_id, now,
    ], value_input_option="RAW")
    drop_cache(EMPLOYEES_WS)


async def notify_empleados(fio: str):
    if not EMPLEADOS_CHAT_ID:
        return
    try:
        await bot.send_message(
            int(EMPLEADOS_CHAT_ID),
            f"📄 Los documentos de {fio} ya están listos — puede pasar a "
            f"firmarlos, de {SIGNING_HOURS}.",
        )
    except Exception as e:
        log.error("Не удалось отправить в Empleados: %s", e)


async def notify_new_contracts() -> int:
    """Проверяет почту. Для каждого нового письма пытается сопоставить вложение
    с записью в чек-листе по имени — если находит, сама заводит карточку
    сотрудника, продвигает этап и пишет в Empleados. Если не находит —
    просто присылает файл патронам на ручную обработку."""
    found = await check_gmail_contracts()
    for item in found:
        names = ", ".join(fn for fn, _ in item["attachments"])
        matched_any = False

        for fn, aid in item["attachments"]:
            raw = await gmail_download_attachment(item["id"], aid)
            if not raw:
                log.error("Не удалось скачать вложение '%s' из письма %s", fn, item["id"])
                for pid in patrons():
                    try:
                        await bot.send_message(
                            pid,
                            f"⚠️ <b>Не удалось скачать вложение</b>\n"
                            f"От: {item['from']}\nТема: {item['subject']}\n"
                            f"Файл: {fn}\n\nПосмотри это письмо в почте вручную.")
                    except Exception:
                        pass
                continue
            candidate = extract_name_from_filename(fn)
            hr_idx, hr_row = find_checklist_match(candidate)
            file_type = next((kw for kw in GMAIL_ATTACHMENT_KEYWORDS
                               if kw.lower() in fn.lower()), "")

            if hr_row:
                matched_any = True
                fio = str(hr_row.get("ФИО", ""))
                sheets_write_retry(add_employee_card, hr_row, file_type, fn, item["id"])
                if file_type == "CONTRATO":
                    sheets_write_retry(set_hr_stage, hr_idx,
                                        "Контракт получен, отправлено на подпись",
                                        "Gmail (авто)")
                for pid in patrons():
                    try:
                        await bot.send_message(
                            pid,
                            f"✅ Файл <b>{fn}</b> распознан и привязан к "
                            f"<b>{fio}</b> — карточка сотрудника создана.")
                        await bot.send_document(pid, BufferedInputFile(raw, filename=fn))
                    except Exception as e:
                        log.error("Не удалось уведомить patron %s о найденном контракте %s: %s",
                                  pid, fn, e)
                if file_type == "CONTRATO":
                    await notify_empleados(fio)
            else:
                for pid in patrons():
                    try:
                        await bot.send_message(
                            pid,
                            f"📨 <b>Новый документ от Histora</b>\n"
                            f"От: {item['from']}\nТема: {item['subject']}\n"
                            f"Дата: {item['date']}\nВложение: {fn}\n\n"
                            f"⚠️ Не удалось автоматически определить, к кому "
                            f"относится — привяжи вручную.")
                        await bot.send_document(pid, BufferedInputFile(raw, filename=fn))
                    except Exception as e:
                        log.error("Не удалось уведомить patron %s о нераспознанном письме %s: %s",
                                  pid, fn, e)

        sheets_write_retry(mark_mail_seen, item["id"], item["date"], item["from"],
                            item["subject"], names)
        await asyncio.sleep(2)  # не жечь лимит записи Google при пачке писем разом
    return len(found)


async def gmail_watch_loop():
    """Фоновая задача — проверяет почту каждые GMAIL_CHECK_INTERVAL секунд."""
    while True:
        try:
            await notify_new_contracts()
            if _gmail_auth_status["broken"] and not _gmail_auth_status["notified"]:
                _gmail_auth_status["notified"] = True
                for pid in patrons():
                    try:
                        await bot.send_message(
                            pid,
                            "⚠️ Доступ к почте (sl.valencia.resta@gmail.com) для проверки "
                            "контрактов истёк — нужна повторная авторизация через OAuth "
                            "Playground (та же процедура, что настраивали).",
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.error("Ошибка фоновой проверки почты: %s", e)
        await asyncio.sleep(GMAIL_CHECK_INTERVAL)


def _gmail_diag_sync():
    """Диагностика: сколько писем Gmail вообще находит по поисковому запросу
    (до фильтрации по вложениям) — помогает понять, где рвётся цепочка."""
    token = _gmail_get_access_token_sync()
    if not token:
        return {"auth_ok": False, "count": 0}
    q = " OR ".join(f"filename:{kw}" for kw in GMAIL_ATTACHMENT_KEYWORDS)
    ids = _gmail_search_all_messages(q)
    return {"auth_ok": True, "count": len(ids)}


async def gmail_diag():
    return await asyncio.to_thread(_gmail_diag_sync)
    """Фоновая задача — проверяет почту каждые GMAIL_CHECK_INTERVAL секунд."""
    while True:
        try:
            await notify_new_contracts()
            if _gmail_auth_status["broken"] and not _gmail_auth_status["notified"]:
                _gmail_auth_status["notified"] = True
                for pid in patrons():
                    try:
                        await bot.send_message(
                            pid,
                            "⚠️ Доступ к почте (sl.valencia.resta@gmail.com) для проверки "
                            "контрактов истёк — нужна повторная авторизация через OAuth "
                            "Playground (та же процедура, что настраивали).",
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.error("Ошибка фоновой проверки почты: %s", e)
        await asyncio.sleep(GMAIL_CHECK_INTERVAL)


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


_seen_msgs = set()  # (chat_id, message_id) — защита от повторной обработки в рамках этого запуска


def doc_already_saved(chat_id, msg_id) -> bool:
    """Проверяет, не сохраняли ли мы уже этот же документ (на случай повторной
    доставки одного и того же апдейта Telegram при рестарте бота)."""
    key = (str(chat_id), str(msg_id))
    if key in _seen_msgs:
        return True
    for r in rows(DOCS_WS, force=True):
        if str(r.get("ChatID")) == key[0] and str(r.get("MessageID")) == key[1]:
            _seen_msgs.add(key)
            return True
    return False


def save_doc(loc, typ, author, chat_id, msg_id, file_id, text) -> int:
    now = now_local()
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


def set_doc_email(row_idx: int, mark: str):
    """Отметка в колонке «Письмо» — ✅ ушло в Билз / ⚠️ не ушло."""
    ws(DOCS_WS).update_cell(row_idx, 11, mark)
    drop_cache(DOCS_WS)


# ---------------- ПОЧТА (пересылка фактур в Билз, через Brevo API) ----------------

def _send_email_sync(subject: str, body: str, to_addr: str,
                      attachment: bytes = None, filename: str = None) -> bool:
    if not BREVO_API_KEY or not SMTP_USER:
        log.warning("Brevo не настроен (нет BREVO_API_KEY/SMTP_USER) — письмо '%s' не отправлено", subject)
        return False

    payload = {
        "sender": {"name": COMPANY, "email": SMTP_USER},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body,
    }
    if attachment:
        payload["attachment"] = [{
            "content": base64.b64encode(attachment).decode("ascii"),
            "name": filename or "factura.jpg",
        }]

    try:
        r = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=20,
        )
        if r.status_code in (200, 201):
            return True
        log.error("Brevo вернул ошибку %s для '%s': %s", r.status_code, subject, r.text[:300])
        return False
    except Exception as e:
        log.error("Ошибка отправки письма '%s' через Brevo: %s", subject, e)
        return False


async def send_email_async(subject, body, to_addr, attachment=None, filename=None) -> bool:
    return await asyncio.to_thread(_send_email_sync, subject, body, to_addr, attachment, filename)


EMAIL_SUBJECT_PREFIX = {"factura": "Фактура", "baja": "Списание"}

# Поставщики товарного учёта — только их фактуры летят в Билз.
# Сотрудник ставит хэштег поставщика в подписи к фото (#Levante, #Macro и т.д.);
# хозтовары/тара (Levasel и всё, чего нет в списке) в Билз не пересылаются.
BILLZ_SUPPLIER_TAGS = {"voravins", "levante", "cominport", "cocacola", "macro", "tgt", "campoluz"}


def caption_matches_billz_supplier(text: str) -> bool:
    if not text:
        return False
    norm = re.sub(r"[^a-zа-яё0-9]", "", text.lower())
    return any(tag in norm for tag in BILLZ_SUPPLIER_TAGS)


async def forward_doc_to_billz(loc: str, typ: str, file_id: str, text: str) -> bool:
    """Пересылает фактуру или списание (фото/файл из группы) на почту Билза."""
    if typ not in EMAIL_SUBJECT_PREFIX:
        return False

    prefix = EMAIL_SUBJECT_PREFIX[typ]
    loc_name = LOCALES.get(loc, {}).get("name", loc)
    now = now_local()
    subject = f"{prefix} — {loc_name} — {now.strftime('%d.%m.%Y')}"
    body = text or f"{prefix} от {loc_name}, {now.strftime('%d.%m.%Y %H:%M')}"

    attachment, filename = None, None
    if file_id:
        try:
            tg_file = await bot.get_file(file_id)
            buf = await bot.download_file(tg_file.file_path)
            attachment = buf.read()
            filename = tg_file.file_path.rsplit("/", 1)[-1] or "doc.jpg"
        except Exception as e:
            log.error("Не удалось скачать файл из Telegram для пересылки: %s", e)

    ok = await send_email_async(subject, body, BILLZ_EMAIL, attachment, filename)
    if not ok:
        log.warning("%s %s (%s) НЕ отправлен(а) в Билз", prefix, loc_name, now.strftime("%d.%m.%Y %H:%M"))
    return ok


def pending_docs():
    return [(i, r) for i, r in enumerate(rows(DOCS_WS, force=True), start=2)
            if str(r.get("Статус")).strip() == "новый"]


def pending_docs_by(loc: str = None, typ: str = None):
    items = pending_docs()
    if loc:
        items = [(i, r) for i, r in items if str(r.get("Локаль")).strip() == loc]
    if typ:
        items = [(i, r) for i, r in items if str(r.get("Тип")).strip() == typ]
    return items


def today_stats():
    """{локаль: {тип: количество}} за сегодня."""
    today = now_local().strftime("%d.%m.%Y")
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
    arch = archived()
    out = []
    for r in rows(MENU_WS):
        locs = str(r.get("Локаль", "")).replace(" ", "").lower()
        if locs and locs != "all" and loc not in locs.split(","):
            continue
        dish = str(r.get("Блюдо", "")).strip()
        if not dish or _n(dish) in arch:
            continue
        out.append(r)
    return out


def _n(s) -> str:
    return " ".join(str(s).split()).strip().lower()


def archived() -> set:
    return {_n(r.get("Блюдо")) for r in rows(ARCHIVE_WS) if str(r.get("Блюдо", "")).strip()}


def archive_add(dish: str, who: str):
    ws(ARCHIVE_WS).append_row(
        [dish, who, now_local().strftime("%d.%m.%Y %H:%M")], value_input_option="RAW")
    drop_cache(ARCHIVE_WS)


def archive_remove(dish: str):
    w = ws(ARCHIVE_WS)
    for i, r in enumerate(rows(ARCHIVE_WS, force=True), start=2):
        if _n(r.get("Блюдо")) == _n(dish):
            w.delete_rows(i)
            drop_cache(ARCHIVE_WS)
            return True
    return False


def archived_rows():
    return [r for r in rows(ARCHIVE_WS) if str(r.get("Блюдо", "")).strip()]


# ---- история переходов: «Назад» работает на любом экране ----
_nav = {}


def nav_push(uid: int, data: str):
    st = _nav.setdefault(uid, [])
    if st and st[-1] == data:
        return
    st.append(data)
    if len(st) > 40:
        del st[:-40]


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
        [dish, file_id, who, now_local().strftime("%d.%m.%Y %H:%M")],
        value_input_option="RAW")
    drop_cache(PHOTOS_WS)


# кто сейчас загружает фото: uid -> название блюда
_awaiting_photo = {}
_awaiting_hr_input = {}  # user_id -> hr_row_idx: ждём от патрона срок документа + разрешение
_awaiting_registro_input = {}  # user_id -> hr_row_idx: ждём поля для таблицы Registro


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


def save_empleados_request(category, text, author, chat_id, msg_id, file_id) -> int:
    now = now_local()
    w = ws(EMPLEADOS_REQ_WS)
    w.append_row([
        category, (text or "")[:2000], author, str(chat_id), str(msg_id),
        file_id or "", now.strftime("%d.%m.%Y %H:%M"), "новый",
    ], value_input_option="RAW")
    drop_cache(EMPLEADOS_REQ_WS)
    return len(w.col_values(1))


if EMPLEADOS_CHAT_ID:
    @dp.message(F.chat.id == int(EMPLEADOS_CHAT_ID),
                lambda m: not (m.text and m.text.startswith("/")))
    async def empleados_intake(m: Message):
        text = m.caption or m.text or ""
        file_id = None
        if m.photo:
            file_id = m.photo[-1].file_id
        elif m.document:
            file_id = m.document.file_id

        if doc_already_saved(m.chat.id, m.message_id):
            return

        category = classify_empleados_text(text)
        author = m.from_user.full_name if m.from_user else "—"

        if not category:
            # не смогли распознать — покажем патронам как есть, без папки
            for pid in patrons():
                try:
                    await bot.send_message(
                        pid,
                        f"❔ <b>Не удалось определить тип заявки</b>\n"
                        f"От: {author}\n\n{text[:600] or '(без текста)'}")
                    if file_id:
                        await bot.forward_message(pid, m.chat.id, m.message_id)
                except Exception:
                    pass
            return

        row_idx = save_empleados_request(category, text, author, m.chat.id,
                                          m.message_id, file_id)
        _seen_msgs.add((str(m.chat.id), str(m.message_id)))
        emoji = EMPLEADOS_CATEGORY_EMOJI.get(category, "📌")
        for pid in patrons():
            try:
                await bot.send_message(
                    pid,
                    f"{emoji} <b>{category}</b>\nОт: {author}\n\n"
                    f"<code>{text[:1000]}</code>\n\n"
                    f"Сохранено — HR → Заявки сотрудников → {category}")
                if file_id:
                    await bot.forward_message(pid, m.chat.id, m.message_id)
            except Exception:
                pass


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            lambda m: not (m.text and m.text.startswith("/")))
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
    else:
        return  # обычный текст в группе — не документ, игнорируем

    author = m.from_user.full_name if m.from_user else "—"
    if doc_already_saved(m.chat.id, m.message_id):
        return
    row_idx = save_doc(loc, typ, author, m.chat.id, m.message_id, file_id, text)
    _seen_msgs.add((str(m.chat.id), str(m.message_id)))

    if typ in EMAIL_SUBJECT_PREFIX:  # factura, baja — пересылаем в Билз
        should_forward = True
        if typ == "factura":
            should_forward = caption_matches_billz_supplier(text)
        if should_forward:
            ok = await forward_doc_to_billz(loc, typ, file_id, text)
            set_doc_email(row_idx, "✅" if ok else "⚠️")
        else:
            set_doc_email(row_idx, "➖")  # не товарный поставщик — не пересылаем


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
    _nav.pop(c.from_user.id, None)
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
    nav_push(c.from_user.id, c.data)

    if dept == "stock":
        kb = [[InlineKeyboardButton(
            text=f"{MENU_EMOJI} {dept_name('menu', lang)}", callback_data="menu")]]
        if is_patron(u):
            kb.insert(0, [InlineKeyboardButton(
                text=f"{DOCTYPES['baja']['emoji']} {t('writeoffs', lang)} "
                     f"({len(pending_docs_by(typ='baja'))})", callback_data="wo")])
            kb.insert(0, [InlineKeyboardButton(
                text=f"{DOCTYPES['factura']['emoji']} {t('invoices', lang)} "
                     f"({len(pending_docs_by(typ='factura'))})", callback_data="inv")])
            kb.insert(0, [InlineKeyboardButton(text=t("today", lang), callback_data="today")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
        await take_over(c, f"{crumb(dept, lang)}", InlineKeyboardMarkup(inline_keyboard=kb))
        await c.answer()
        return

    if dept == "hr":
        kb = []
        if is_patron(u):
            kb.append([InlineKeyboardButton(
                text=f"🆕 Новые заявки ({len(new_applicants())})", callback_data="hrn")])
            checklist = rows(HR_WS)
            n_warn = sum(1 for r in checklist if hr_doc_warning(r))
            label = f"📋 Чек-лист сотрудников ({len(checklist)})"
            if n_warn:
                label = f"⚠️ {label} — {n_warn} с проблемой"
            kb.append([InlineKeyboardButton(text=label, callback_data="hrc")])
            kb.append([InlineKeyboardButton(
                text=f"👤 Сотрудники ({len(rows(EMPLOYEES_WS))})", callback_data="hre")])
            kb.append([InlineKeyboardButton(text="📧 Проверить почту на контракты", callback_data="hrmail")])
            n_req = sum(1 for r in rows(EMPLEADOS_REQ_WS) if str(r.get("Статус")).strip() == "новый")
            kb.append([InlineKeyboardButton(
                text=f"📨 Заявки сотрудников ({n_req})", callback_data="hrq")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
        await take_over(c, f"{crumb(dept, lang)}", InlineKeyboardMarkup(inline_keyboard=kb))
        await c.answer()
        return

    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:{dept}:{code}")]
          for code, l in LOCALES.items()]
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
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
    nav_push(c.from_user.id, "menu")
    kb = [[InlineKeyboardButton(text=f"{l['emoji']} {l['name']}", callback_data=f"l:menu:{code}")]
          for code, l in LOCALES.items()]
    if is_patron(u):
        kb.append([InlineKeyboardButton(text=t("archive", lang), callback_data="arch")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
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
    nav_push(c.from_user.id, c.data)
    _, dept, loc = c.data.split(":")
    if (dept not in DEPTS and dept != "menu") or loc not in LOCALES:
        await c.answer()
        return

    if dept == "menu":
        groups = menu_groups(loc)
        if not groups:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
            await take_over(c, f"{crumb(dept, lang, loc)}\n\n<i>{t('empty', lang)}</i>", kb)
            await c.answer()
            return
        kb = []
        if is_patron(u):
            kb.append([InlineKeyboardButton(text=t("fc_btn", lang), callback_data=f"fc:{loc}")])
        for i, g in enumerate(groups):
            n = len([r for r in menu_rows(loc) if str(r.get("Группа", "")).strip() == g])
            kb.append([InlineKeyboardButton(text=f"{g}  ·  {n}", callback_data=f"mg:{loc}:{i}")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
        await take_over(c, f"{crumb(dept, lang, loc)}\n\n{t('choose_item', lang)}",
                        InlineKeyboardMarkup(inline_keyboard=kb))
        await c.answer()
        return

    items = sections(dept, loc, lang)
    if not items:
        text = f"{crumb(dept, lang, loc)}\n\n<i>{t('empty', lang)}</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
    else:
        text = f"{crumb(dept, lang, loc)}\n\n{t('choose_item', lang)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=str(r["Раздел"])[:60], callback_data=f"s:{dept}:{loc}:{i}")]
              for i, r in enumerate(items)],
            [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
    await take_over(c, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_section(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
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
        [InlineKeyboardButton(text=t("back", lang), callback_data="bk")],
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
    nav_push(c.from_user.id, c.data)
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
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
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
    nav_push(c.from_user.id, c.data)
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
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
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
    nav_push(c.from_user.id, c.data)
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
    if is_patron(u):
        kb_rows.append([InlineKeyboardButton(
            text=t("arch_put", lang), callback_data=f"ar:{kkey(dish)}")])
    kb_rows.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
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


@dp.message(F.chat.type == "private", lambda m: m.from_user.id in _awaiting_hr_input)
async def on_private_text(m: Message):
    idx = _awaiting_hr_input.get(m.from_user.id)
    if not idx:
        return
    parts = [p.strip() for p in m.text.strip().splitlines() if p.strip()]
    if len(parts) < 2:
        await m.answer("Нужно две строки: дата и да/нет. Попробуй ещё раз.")
        return
    date_str, permit_str = parts[0], parts[1].lower()
    if not parse_ddmmyyyy(date_str):
        await m.answer("Дата не распознана, формат дд.мм.гггг. Попробуй ещё раз.")
        return
    if permit_str not in ("да", "нет"):
        await m.answer("Вторая строка должна быть 'да' или 'нет'. Попробуй ещё раз.")
        return
    _awaiting_hr_input.pop(m.from_user.id, None)
    set_hr_doc_info(idx, date_str, permit_str == "да")
    await m.answer("Сохранено ✅")


@dp.message(F.chat.type == "private", lambda m: m.from_user.id in _awaiting_registro_input)
async def on_private_text_registro(m: Message):
    idx = _awaiting_registro_input.get(m.from_user.id)
    if not idx:
        return
    parts = [p.strip() for p in m.text.strip().splitlines() if p.strip()]
    if len(parts) < 6:
        await m.answer("Нужно 6 строк (пол / departamento / дата / тип контракта / "
                        "график / ссылка на папку Диска). Попробуй ещё раз, каждое "
                        "с новой строки.")
        return
    sex, departamento, fecha_alta_raw, tipo_contrato, horario, folder_link = parts[:6]
    if sex.strip().upper() not in ("M", "F"):
        await m.answer("Первая строка должна быть M или F. Попробуй ещё раз.")
        return
    if not parse_ddmmyyyy(fecha_alta_raw):
        await m.answer("Дата (3-я строка) не распознана, формат дд.мм.гггг. Попробуй ещё раз.")
        return
    if not folder_link.startswith("http"):
        await m.answer("6-я строка должна быть ссылкой на папку Google Диска "
                        "(начинается с http). Попробуй ещё раз.")
        return

    hr_data = rows(HR_WS, force=True)
    try:
        hr_row = hr_data[idx - 2]
    except IndexError:
        await m.answer("Не нашла эту запись в чек-листе — возможно, её удалили.")
        _awaiting_registro_input.pop(m.from_user.id, None)
        return

    row_key = str(hr_row.get("RowKey", "")).strip()
    applicant = {}
    try:
        applicant = applicants_rows()[int(row_key) - 2]
    except Exception:
        pass

    nombre = str(applicant.get("Nombre", "")).strip()
    apellido = str(applicant.get("Apellido", "")).strip()
    full_name = f"{nombre} {apellido}".strip() or str(hr_row.get("ФИО", ""))
    loc_code = match_locale_code(str(hr_row.get("Локаль", "")))
    loc_label = LOCALES.get(loc_code, {}).get("tag", str(hr_row.get("Локаль", "")))

    values = {
        "Nombre y Apellidos": full_name,
        "Nombre": nombre,
        "Apellido": apellido,
        "Sex": sex.strip().upper(),
        "Local": loc_label,
        "local": loc_label,
        "Horas": str(applicant.get("Número de horas bajo contrato", "")).strip(),
        "Departamento": departamento,
        "Puesto": str(hr_row.get("Должность", "")).strip(),
        "Fecha de Alta": fecha_alta_raw,
        "Tipo de contrato": tipo_contrato,
        "Horario": horario,
        "IBAN": str(applicant.get("IBAN", "")).strip(),
        "Fecha Nacimiento": str(applicant.get("Fecha de nacimiento", "")).strip(),
        "Fecha de nacimiento": str(applicant.get("Fecha de nacimiento", "")).strip(),
        "Dirección e-mail": str(applicant.get("Correo electrónico", "")).strip(),
        "Correo electrónico": str(applicant.get("Correo electrónico", "")).strip(),
        "Teléfono Móvil": str(applicant.get("Teléfono", "")).strip(),
        "Teléfono": str(applicant.get("Teléfono", "")).strip(),
        "Domicilio": str(applicant.get("Domicilio", "")).strip(),
        "TIE/NIE": str(applicant.get("NIE/TIE", "")).strip(),
        "NIE/TIE": str(applicant.get("NIE/TIE", "")).strip(),
        "Contrato": f'=HYPERLINK("{folder_link}"; "{full_name}")',
    }

    try:
        unmatched = registro_append(values)
    except Exception as e:
        log.error("Ошибка записи в Registro: %s", e)
        await m.answer(f"⚠️ Не удалось записать в Registro: {type(e).__name__}: {e}")
        return

    _awaiting_registro_input.pop(m.from_user.id, None)
    author = m.from_user.full_name if m.from_user else "—"
    set_hr_stage(idx, HR_STAGES[-1], author)
    note = "Записано в Registro ✅"
    if unmatched:
        note += f"\n(не нашли колонки: {', '.join(unmatched.keys())})"
    await m.answer(note)


@dp.callback_query(F.data.startswith("tk:"))
async def cb_tech(c: CallbackQuery):
    u = guard(c)
    if not u:
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
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
            text=t("back", lang), callback_data="bk")])
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
    nav_push(c.from_user.id, c.data)
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
        [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
    await take_over(c, text[:4000], kb)
    await c.answer()


@dp.callback_query(F.data == "today")
async def cb_today(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    stats = today_stats()
    today = now_local().strftime("%d.%m.%Y")
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
        [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
    await take_over(c, "\n".join(lines)[:4000], kb)
    await c.answer()


@dp.callback_query(F.data == "q")
async def cb_queue(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    items = pending_docs()
    if not items:
        text = f"<b>{t('queue', lang)}</b>\n\n{t('queue_empty', lang)}"
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data="q")],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
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
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text[:4000], InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


DOC_MENUS = {
    "factura": {"prefix": "inv", "title_key": "invoices"},
    "baja":    {"prefix": "wo",  "title_key": "writeoffs"},
}


async def show_doc_locale_menu(c: CallbackQuery, typ: str):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    cfg = DOC_MENUS[typ]
    kb = []
    for code, L in LOCALES.items():
        n = len(pending_docs_by(loc=code, typ=typ))
        kb.append([InlineKeyboardButton(
            text=f"{L['emoji']} {L['name']} ({n})", callback_data=f"{cfg['prefix']}:{code}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    title = f"{DOCTYPES[typ]['emoji']} <b>{t(cfg['title_key'], lang)}</b>"
    await take_over(c, f"{title}\n\n{t('choose_locale', lang)}",
                    InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


async def show_doc_locale_list(c: CallbackQuery, typ: str, loc: str):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    cfg = DOC_MENUS[typ]
    L = LOCALES.get(loc, {})
    items = pending_docs_by(loc=loc, typ=typ)
    title = (f"{DOCTYPES[typ]['emoji']} <b>{t(cfg['title_key'], lang)}</b> · "
             f"{L.get('emoji', '')} {L.get('name', loc)}")
    if not items:
        text = f"{title}\n\n{t('queue_empty', lang)}"
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
    else:
        text = f"{title} — {len(items)}\n\n<i>{t('queue_hint', lang)}</i>"
        kb = []
        for idx, r in items[:40]:
            mark = str(r.get("Письмо") or "").strip()
            label = f"{(mark + ' ') if mark else ''}{r.get('Дата')} {r.get('Время')} · {r.get('Автор')}"
            kb.append([InlineKeyboardButton(text=label[:60], callback_data=f"qd:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text[:4000], InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data == "inv")
async def cb_invoices(c: CallbackQuery):
    await show_doc_locale_menu(c, "factura")


@dp.callback_query(F.data.startswith("inv:"))
async def cb_invoices_loc(c: CallbackQuery):
    await show_doc_locale_list(c, "factura", c.data.split(":")[1])


@dp.callback_query(F.data == "wo")
async def cb_writeoffs(c: CallbackQuery):
    await show_doc_locale_menu(c, "baja")


@dp.callback_query(F.data.startswith("wo:"))
async def cb_writeoffs_loc(c: CallbackQuery):
    await show_doc_locale_list(c, "baja", c.data.split(":")[1])


# ---------------- HR ----------------

@dp.callback_query(F.data == "hrn")
async def cb_hr_new(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    kb = []
    for code, L in LOCALES.items():
        n = len(new_applicants(loc=code))
        kb.append([InlineKeyboardButton(
            text=f"{L['emoji']} {L['name']} ({n})", callback_data=f"hrn:{code}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, "🆕 <b>Новые заявки</b>\n\n" + t("choose_locale", lang),
                    InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrn:"))
async def cb_hr_new_loc(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    loc = c.data.split(":")[1]
    L = LOCALES.get(loc, {})
    items = new_applicants(loc=loc)
    title = f"🆕 <b>Новые заявки</b> · {L.get('emoji', '')} {L.get('name', loc)}"
    if not items:
        text = f"{title}\n\nНовых заявок нет."
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
    else:
        text = f"{title} — {len(items)}"
        kb = []
        for idx, r in items[:40]:
            name = f"{r.get('Nombre', '')} {r.get('Apellido', '')}".strip()
            kb.append([InlineKeyboardButton(text=name[:60], callback_data=f"hrnd:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrnd:"))
async def cb_hr_new_detail(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    nav_push(c.from_user.id, c.data)
    idx = int(c.data.split(":")[1])
    data = applicants_rows()
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    block = format_applicant_block(r)
    link = applicant_doc_link(r)
    text = f"🆕 <b>Заявка</b>\n\n<code>{block}</code>"
    photo = None
    if link:
        raw = await download_drive_file(link)
        if raw:
            photo = BufferedInputFile(raw, filename="document.jpg")
        else:
            text += f"\n\n📎 <a href=\"{link}\">Фото документа (открыть по ссылке)</a>"
    kb = [[InlineKeyboardButton(text="➕ Добавить в чек-лист", callback_data=f"hradd:{idx}")],
          [InlineKeyboardButton(text=t("back", ulang(u)), callback_data="bk")]]
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb), photo=photo)
    await c.answer()


@dp.callback_query(F.data.startswith("hradd:"))
async def cb_hr_add(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    data = applicants_rows()
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    author = c.from_user.full_name if c.from_user else "—"
    add_to_hr_checklist(idx, r, author)
    await c.answer("Добавлено в чек-лист ✅", show_alert=True)
    loc = match_locale_code(r.get("Local", ""))
    await route(c, f"hrn:{loc}" if loc else "hrn")


@dp.callback_query(F.data == "hre")
async def cb_hr_employees(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    items = list(enumerate(rows(EMPLOYEES_WS, force=True), start=2))
    title = "👤 <b>Сотрудники</b>"
    if not items:
        text = f"{title}\n\nПусто."
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data="hre")],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
    else:
        text = f"{title} — {len(items)}"
        kb = []
        for idx, r in items:
            fio = str(r.get("ФИО", "")).strip()
            loc = str(r.get("Локаль", "")).strip()
            kb.append([InlineKeyboardButton(text=f"{fio} · {loc}"[:60], callback_data=f"hre:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data="hre")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hre:"))
async def cb_hr_employee_detail(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    nav_push(c.from_user.id, c.data)
    idx = int(c.data.split(":")[1])
    data = rows(EMPLOYEES_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    text = (f"👤 <b>{r.get('ФИО', '')}</b>\n"
            f"{r.get('Локаль', '')} · {r.get('Должность', '')}\n\n"
            f"Заявка от: {r.get('Дата заявки', '')}\n"
            f"Файл: {r.get('Тип файла', '')} — {r.get('Имя файла', '')}\n"
            f"Добавлено: {r.get('Дата добавления', '')}")
    kb = [[InlineKeyboardButton(text=t("back", ulang(u)), callback_data="bk")]]
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


def pending_empleados_requests(category: str = None):
    out = []
    for idx, r in enumerate(rows(EMPLEADOS_REQ_WS, force=True), start=2):
        if str(r.get("Статус")).strip() != "новый":
            continue
        if category and str(r.get("Категория")).strip() != category:
            continue
        out.append((idx, r))
    return out


@dp.callback_query(F.data == "hrq")
async def cb_hr_requests(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    kb = []
    for cat in EMPLEADOS_CATEGORY_KEYWORDS:
        n = len(pending_empleados_requests(cat))
        emoji = EMPLEADOS_CATEGORY_EMOJI.get(cat, "📌")
        kb.append([InlineKeyboardButton(text=f"{emoji} {cat} ({n})", callback_data=f"hrq:{cat}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, "📨 <b>Заявки сотрудников</b>\n\nВыберите папку:",
                    InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrq:"))
async def cb_hr_requests_cat(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    cat = c.data.split(":", 1)[1]
    emoji = EMPLEADOS_CATEGORY_EMOJI.get(cat, "📌")
    items = pending_empleados_requests(cat)
    title = f"{emoji} <b>{cat}</b>"
    if not items:
        text = f"{title}\n\nПусто."
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
    else:
        text = f"{title} — {len(items)}"
        kb = []
        for idx, r in items[:40]:
            label = f"{r.get('Автор', '')} · {r.get('Дата', '')}"
            kb.append([InlineKeyboardButton(text=label[:60], callback_data=f"hrqd:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data=c.data)])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrqd:"))
async def cb_hr_requests_detail(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    nav_push(c.from_user.id, c.data)
    idx = int(c.data.split(":")[1])
    data = rows(EMPLEADOS_REQ_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    cat = str(r.get("Категория", ""))
    emoji = EMPLEADOS_CATEGORY_EMOJI.get(cat, "📌")
    text = (f"{emoji} <b>{cat}</b>\nОт: {r.get('Автор', '')}\n{r.get('Дата', '')}\n\n"
            f"<code>{str(r.get('Текст', ''))[:1000]}</code>")
    kb = [[InlineKeyboardButton(text="✅ Обработано", callback_data=f"hrqok:{idx}")],
          [InlineKeyboardButton(text=t("back", ulang(u)), callback_data="bk")]]
    fid = str(r.get("FileID") or "").strip() or None
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb), photo=fid)
    await c.answer()


@dp.callback_query(F.data.startswith("hrqok:"))
async def cb_hr_requests_done(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    ws(EMPLEADOS_REQ_WS).update_cell(idx, 8, "обработано")
    drop_cache(EMPLEADOS_REQ_WS)
    await c.answer("Отмечено ✅")
    await nav_back(c)


@dp.callback_query(F.data == "hrmail")
async def cb_hr_mail_check(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    await c.answer("Проверяю почту…")
    n = await notify_new_contracts()
    if n == 0:
        diag = await gmail_diag()
        if not diag["auth_ok"]:
            await bot.send_message(
                c.from_user.id,
                "⚠️ Не удалось подключиться к почте — доступ не работает "
                "(нужна повторная авторизация через OAuth Playground).",
            )
        elif diag["count"] == 0:
            await bot.send_message(
                c.from_user.id,
                "Почта проверена — Gmail не нашёл ни одного письма с вложением, "
                "где в имени файла есть CONTRATO / BAJA / CAMBIO.",
            )
        else:
            await bot.send_message(
                c.from_user.id,
                f"Почта проверена — Gmail нашёл {diag['count']} подходящих писем, "
                "но все они уже были показаны раньше (или проблема в имени вложения "
                "внутри письма — сверю ещё раз, если пришлёшь пример темы письма).",
            )


@dp.callback_query(F.data == "hrc")
async def cb_hr_checklist(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
    items = list(enumerate(rows(HR_WS, force=True), start=2))
    title = "📋 <b>Чек-лист сотрудников</b>"
    if not items:
        text = f"{title}\n\nПусто."
        kb = [[InlineKeyboardButton(text=t("refresh", lang), callback_data="hrc")],
              [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]]
    else:
        text = f"{title} — {len(items)}"
        kb = []
        for idx, r in items:
            fio = str(r.get("ФИО", "")).strip()
            stage = str(r.get("Статус", "")).strip()
            warn = hr_doc_warning(r)
            label = f"{fio} · {stage}"
            if warn:
                label = f"⚠️ {label}"
            kb.append([InlineKeyboardButton(text=label[:60], callback_data=f"hrc:{idx}")])
        kb.append([InlineKeyboardButton(text=t("refresh", lang), callback_data="hrc")])
        kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrc:"))
async def cb_hr_checklist_detail(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    nav_push(c.from_user.id, c.data)
    idx = int(c.data.split(":")[1])
    data = rows(HR_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    fio = str(r.get("ФИО", "")).strip()
    loc = str(r.get("Локаль", "")).strip()
    stage = str(r.get("Статус", "")).strip()
    stage_idx = HR_STAGES.index(stage) if stage in HR_STAGES else 0
    expiry = str(r.get("Срок документа", "")).strip() or "не указан"
    permit = str(r.get("Разрешение на работу", "")).strip() or "не указано"
    text = (f"📋 <b>{fio}</b>\n{loc} · {r.get('Должность', '')}\n\n"
            f"Этап: <b>{stage}</b>\nЗаявка от: {r.get('Дата заявки', '')}\n"
            f"Обновил: {r.get('Обновил', '')} ({r.get('Дата обновления', '')})\n\n"
            f"📅 Срок документа: {expiry}\n✅ Разрешение на работу: {permit}")
    warn = hr_doc_warning(r)
    if warn:
        text += f"\n\n{warn}"
    kb = []
    if stage_idx < len(HR_STAGES) - 1:
        nxt = HR_STAGES[stage_idx + 1]
        kb.append([InlineKeyboardButton(text=f"➡️ {nxt}", callback_data=f"hrnext:{idx}")])
    kb.append([InlineKeyboardButton(text="📅 Указать срок и разрешение", callback_data=f"hrdate:{idx}")])
    kb.append([InlineKeyboardButton(text="🗑 Удалить из чек-листа", callback_data=f"hrdel:{idx}")])
    kb.append([InlineKeyboardButton(text=t("back", ulang(u)), callback_data="bk")])
    await take_over(c, text, InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("hrdate:"))
async def cb_hr_ask_date(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    _awaiting_hr_input[c.from_user.id] = idx
    await c.answer()
    await bot.send_message(
        c.from_user.id,
        "Напиши двумя строками:\n"
        "1) срок действия документа — дд.мм.гггг\n"
        "2) есть разрешение на работу — да / нет\n\n"
        "Например:\n<code>15.03.2027\nда</code>",
    )


@dp.callback_query(F.data.startswith("hrnext:"))
async def cb_hr_next(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    data = rows(HR_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    stage = str(r.get("Статус", "")).strip()
    stage_idx = HR_STAGES.index(stage) if stage in HR_STAGES else 0
    if stage_idx >= len(HR_STAGES) - 1:
        await c.answer("Уже финальный этап")
        return
    next_stage = HR_STAGES[stage_idx + 1]

    if next_stage == HR_STAGES[-1]:  # "Активен" — перед этим соберём Registro
        _awaiting_registro_input[c.from_user.id] = idx
        await c.answer()
        await bot.send_message(
            c.from_user.id,
            f"Финальный этап для <b>{r.get('ФИО', '')}</b> — заполняю строку "
            f"в Registro. Напиши 6 строк подряд:\n"
            f"1) Пол — M / F\n"
            f"2) Departamento (например Cocina, Barra, Managment)\n"
            f"3) Fecha de Alta — дд.мм.гггг\n"
            f"4) Tipo de contrato (например Indefinido, Temporal)\n"
            f"5) Horario (например 40 h 9-17)\n"
            f"6) Ссылка на папку сотрудника на Google Диске\n\n"
            f"Например:\n<code>M\nCocina\n15.09.2026\nIndefinido\n40 h 9-17\n"
            f"https://drive.google.com/drive/folders/xxxxx</code>",
        )
        return

    author = c.from_user.full_name if c.from_user else "—"
    set_hr_stage(idx, next_stage, author)
    await c.answer("Обновлено")
    await route(c, f"hrc:{idx}")


@dp.callback_query(F.data.startswith("hrdel:"))
async def cb_hr_delete(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    ws(HR_WS).delete_rows(idx)
    drop_cache(HR_WS)
    await c.answer("Удалено")
    await route(c, "hrc")


@dp.callback_query(F.data.startswith("qd:"))
async def cb_queue_doc(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, c.data)
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
    status = str(r.get("Статус")).strip()
    done = status != "новый"
    mark = str(r.get("Письмо") or "").strip()
    cap = (f"{DOCTYPES.get(typ, {}).get('emoji', '📄')} "
           f"<b>{doc_name(typ, lang) if typ in DOCTYPES else typ}</b>"
           f" · {L.get('emoji', '')} {L.get('name', loc)}\n"
           f"{t('from', lang)}: {r.get('Автор')}\n{r.get('Дата')} {r.get('Время')}")
    if mark:
        mark_text = {"✅": "отправлено в Билз", "⚠️": "не отправлено в Билз",
                     "➖": "не товарный поставщик — не пересылалось"}.get(mark, "")
        cap += f"\n{mark} {mark_text}"
    txt = str(r.get("Текст") or "").strip()
    if txt:
        cap += f"\n\n{txt[:600]}"
    kb = []
    if not done:
        kb.append([InlineKeyboardButton(text=t("sent_btn", lang), callback_data=f"sent:{idx}")])
    if typ in EMAIL_SUBJECT_PREFIX and mark != "✅":
        kb.append([InlineKeyboardButton(text="📧 Переслать в Билз", callback_data=f"fwd:{idx}")])
    kb.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{idx}")])
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    fid = str(r.get("FileID") or "").strip() or None
    await take_over(c, cap, InlineKeyboardMarkup(inline_keyboard=kb), photo=fid)
    await c.answer()


@dp.callback_query(F.data.startswith("fwd:"))
async def cb_forward_doc(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    data = rows(DOCS_WS, force=True)
    try:
        r = data[idx - 2]
    except IndexError:
        await c.answer("—", show_alert=True)
        return
    loc = str(r.get("Локаль")).strip()
    typ = str(r.get("Тип")).strip()
    file_id = str(r.get("FileID") or "").strip() or None
    text = str(r.get("Текст") or "").strip()
    ok = await forward_doc_to_billz(loc, typ, file_id, text)
    set_doc_email(idx, "✅" if ok else "⚠️")
    await c.answer("✅ Отправлено" if ok else "⚠️ Не удалось отправить", show_alert=True)
    await route(c, c.data.replace("fwd:", "qd:"))


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_doc(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    idx = int(c.data.split(":")[1])
    set_doc_status(idx, "удалено")
    await c.answer("Удалено")
    await nav_back(c)


@dp.callback_query(F.data == "arch")
async def cb_archive(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    nav_push(c.from_user.id, "arch")
    items = archived_rows()
    if not items:
        text = f"<b>{t('archive', lang)}</b>\n\n<i>{t('arch_empty', lang)}</i>"
        kb = []
    else:
        text = f"<b>{t('archive', lang)}</b> — {len(items)}\n\n<i>{t('arch_hint', lang)}</i>"
        kb = [[InlineKeyboardButton(text=f"↩️ {str(r.get('Блюдо'))[:50]}",
                                    callback_data=f"unar:{kkey(str(r.get('Блюдо')))}")]
              for r in items[:40]]
    kb.append([InlineKeyboardButton(text=t("back", lang), callback_data="bk")])
    await take_over(c, text[:4000], InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@dp.callback_query(F.data.startswith("ar:"))
async def cb_archive_add(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    dish = kname(c.data.split(":")[1])
    if not dish:
        await c.answer("Откройте блюдо заново", show_alert=True)
        return
    archive_add(dish, u.get("Имя") or "—")
    await c.answer(t("arch_done", lang), show_alert=True)
    await nav_back(c)


@dp.callback_query(F.data.startswith("unar:"))
async def cb_archive_restore(c: CallbackQuery):
    u = guard(c)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    dish = kname(c.data.split(":")[1])
    if dish:
        archive_remove(dish)
        await c.answer(t("arch_back", lang))
    await cb_archive(c)


@dp.callback_query(F.data.startswith("sent:"))
async def cb_sent(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not is_patron(u):
        await c.answer(t("only_patron", ulang(u)), show_alert=True)
        return
    lang = ulang(u)
    set_doc_status(int(c.data.split(":")[1]), "отправлено")
    await c.answer(t("marked", lang))
    await nav_back(c)


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ---- маршрутизатор: «Назад» возвращает на предыдущий экран ----

ROUTES = [
    ("home",  lambda cc: cb_home(cc)),
    ("menu",  lambda cc: cb_menu_root(cc)),
    ("arch",  lambda cc: cb_archive(cc)),
    ("today", lambda cc: cb_today(cc)),
    ("q",     lambda cc: cb_queue(cc)),
    ("inv:",  lambda cc: cb_invoices_loc(cc)),
    ("inv",   lambda cc: cb_invoices(cc)),
    ("wo:",   lambda cc: cb_writeoffs_loc(cc)),
    ("wo",    lambda cc: cb_writeoffs(cc)),
    ("hrn:",  lambda cc: cb_hr_new_loc(cc)),
    ("hrnd:", lambda cc: cb_hr_new_detail(cc)),
    ("hrn",   lambda cc: cb_hr_new(cc)),
    ("hrc:",  lambda cc: cb_hr_checklist_detail(cc)),
    ("hrc",   lambda cc: cb_hr_checklist(cc)),
    ("hre:",  lambda cc: cb_hr_employee_detail(cc)),
    ("hre",   lambda cc: cb_hr_employees(cc)),
    ("hrq:",  lambda cc: cb_hr_requests_cat(cc)),
    ("hrqd:", lambda cc: cb_hr_requests_detail(cc)),
    ("hrq",   lambda cc: cb_hr_requests(cc)),
    ("d:",    lambda cc: cb_dept(cc)),
    ("l:",    lambda cc: cb_loc(cc)),
    ("mg:",   lambda cc: cb_menu_group(cc)),
    ("ms:",   lambda cc: cb_menu_sub(cc)),
    ("md:",   lambda cc: cb_menu_dish(cc)),
    ("tk:",   lambda cc: cb_tech(cc)),
    ("s:",    lambda cc: cb_section(cc)),
    ("qd:",   lambda cc: cb_queue_doc(cc)),
    ("fc:",   lambda cc: cb_foodcost(cc)),
]


async def route(c: CallbackQuery, data: str):
    cc = c.model_copy(update={"data": data})
    for prefix, fn in ROUTES:
        if data == prefix or (prefix.endswith(":") and data.startswith(prefix)):
            await fn(cc)
            return
    await cb_home(cc)


async def nav_back(c: CallbackQuery):
    st = _nav.get(c.from_user.id, [])
    if st:
        st.pop()
    target = st[-1] if st else "home"
    await route(c, target)


@dp.callback_query(F.data == "bk")
async def cb_back(c: CallbackQuery):
    if not guard(c):
        await c.answer(t("no_access", DEFAULT_LANG), show_alert=True)
        return
    await nav_back(c)
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
        [InlineKeyboardButton(text=t("back", lang), callback_data="bk")]])
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


# ---------------- ОШИБКИ И СТАТУС БОТА — В ЛИЧКУ ПАТРОНАМ ----------------

@dp.errors()
async def error_handler(event: ErrorEvent):
    log.exception("Необработанная ошибка: %s", event.exception)
    text = (f"🔴 Ошибка в боте\n"
            f"<code>{type(event.exception).__name__}: {event.exception}</code>")
    for pid in patrons():
        try:
            await bot.send_message(pid, text[:4000])
        except Exception:
            pass
    return True


async def main():
    ensure_headers()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="🔄 Обновить / открыть меню"),
    ])

    now = now_local().strftime("%d.%m.%Y %H:%M")
    for pid in patrons():
        try:
            await bot.send_message(pid, f"🟢 Бот перезапущен — {now}")
        except Exception:
            pass

    if GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN:
        asyncio.create_task(gmail_watch_loop())

    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception as e:
            log.exception("Сбой в цикле polling: %s", e)
            for pid in patrons():
                try:
                    await bot.send_message(
                        pid,
                        f"🔴 Сбой в работе бота, перезапускаю через 10 сек (без потери "
                        f"подключения к таблице):\n<code>{type(e).__name__}: {e}</code>"[:4000],
                    )
                except Exception:
                    pass
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
