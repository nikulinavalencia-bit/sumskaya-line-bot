# -*- coding: utf-8 -*-
"""
💶 ФИНАНСЫ И ПЛАТЕЖИ — отдельный модуль бота SUMSKAYA LINE SL.

Живёт своим файлом. В bot555.py добавляется ровно одна вставка (см. README_fin.md):

    try:
        import sys as _sys, fin_block
        fin_block.setup(dp, _sys.modules[__name__])
    except Exception as _e:
        log.error("fin_block не подключён: %s", _e, exc_info=True)

Всё остальное — здесь. Модуль ничего не переопределяет в существующем коде:
свои хендлеры он регистрирует сам, все колбэки с префиксом «fin:», состояния —
свои. Если этот файл сломан, бот стартует без финблока, HR и склад работают.

Реализовано: этапы 1–4 ТЗ (роль, меню, загрузка документа, очередь
«Неоплаченные», карточка, ручной ввод, справочник поставщиков, сборка списка
к ремесе с пересчётом суммы).
Ждёт ответов заказчика: генерация XML/PDF ремесы (вопросы 10.3–10.8), OCR (10.2).
"""

import os
import re
import html
import asyncio
import logging
from time import time

from aiogram import F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("fin")

core = None          # модуль bot.py целиком — подставляется в setup()
_dp = None           # диспетчер, нужен соседним модулям блока
_routes_done = False

# ---------------- НАСТРОЙКИ (открытые вопросы ТЗ — меняются одной строкой) ----------------

# 10.1 — видит ли патрон финблок целиком наравне с фин. директором
PATRON_FULL_ACCESS = True

# Если распознанная фактура полностью сходится — бот сам ставит «готов к ремесе»
# и ничего не спрашивает. Подтверждение только там, где чего-то не хватает.
AUTO_APPROVE = True
# Первый платёж поставщику, которого ещё нет в справочнике, всё же показываем
# человеку: именно здесь ловится подмена реквизитов.
AUTO_APPROVE_NEW_SUPPLIER = False
# Допуск при сверке «база + IVA = итог», в евро
SUM_TOLERANCE = 0.02

# Роль фин. директора. Ставится в таблице Users в колонке «Роль» ровно этим текстом.
ROLE_FINDIR = "Фин. директор"

# Запасной вариант: список Telegram ID через запятую в переменной окружения.
FIN_DIRECTOR_IDS = {
    int(x) for x in os.environ.get("FIN_DIRECTOR_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

STATE_TTL = 30 * 60      # сколько живёт «жду документ / жду ввод», секунд
LIST_LIMIT = 30          # максимум строк на экране списка

# ---------------- ЛИСТЫ GOOGLE SHEETS ----------------

FACTURAS_WS = "FIN_Facturas"
PROVEEDORES_WS = "FIN_Proveedores"
REMESAS_WS = "FIN_Remesas"

FIN_HEADERS = {
    FACTURAS_WS: [
        "ID", "Дата", "Время", "Локаль", "Автор", "AuthorID", "ChatID", "MessageID",
        "FileID", "Тип файла", "Поставщик", "NIF", "IBAN", "База", "IVA", "Total",
        "Номер", "Дата фактуры", "Хустификанте", "Статус", "РемесаID",
        "ХустификантеFileID", "Дата оплаты",
    ],
    PROVEEDORES_WS: [
        "Поставщик", "Алиасы", "NIF", "IBAN", "BIC", "Страна", "Город", "Адрес",
        "Локали", "Комментарий", "Кто добавил", "Дата",
    ],
    REMESAS_WS: [
        "РемесаID", "Локаль", "Дата", "Кол-во", "Сумма", "Кто сформировал",
        "XML", "Статус", "ХустификантеFileID",
    ],
}

# ---------------- СТАТУСЫ ----------------

ST_NEW = "новый"
ST_RECOGNIZED = "распознан"
ST_NEED_IBAN = "нужен IBAN"
ST_READY = "готов к ремесе"
ST_EXCLUDED = "исключён"
ST_IN_REMESA = "в ремесе"
ST_SENT = "отправлено в банк"
ST_PAID = "оплачено"

UNPAID = (ST_NEW, ST_RECOGNIZED, ST_NEED_IBAN, ST_READY, ST_EXCLUDED)
PAID_QUEUE = (ST_IN_REMESA, ST_SENT)

ST_EMOJI = {
    ST_NEW: "🆕",
    ST_RECOGNIZED: "🔍",
    ST_NEED_IBAN: "⚠️",
    ST_READY: "✅",
    ST_EXCLUDED: "🚫",
    ST_IN_REMESA: "📦",
    ST_SENT: "🏦",
    ST_PAID: "💸",
}

# ---------------- СОСТОЯНИЯ (свои, с истечением срока) ----------------

_await_doc = {}     # uid -> ts             : ждём фото/PDF документа на оплату
_pending = {}       # uid -> dict           : загруженный док, ждёт локаль/галочку
_await_fill = {}    # uid -> (fid, ts)      : ждём текст с данными фактуры

# справочник поставщиков
_await_prov_field = {}   # uid -> (row, поле, ts) : ждём новое значение поля
_await_prov_new = {}     # uid -> ts             : ждём данные нового поставщика
_await_prov_import = {}  # uid -> ts             : ждём список пачкой (текст или файл)
_await_prov_find = {}    # uid -> ts             : ждём строку поиска
_prov_query = {}         # uid -> строка поиска
_await_just = {}         # uid -> (fid, ts)      : ждём хустификанте из банка


def _fresh(ts) -> bool:
    return bool(ts) and (time() - ts) < STATE_TTL


def _clear(uid: int):
    _await_doc.pop(uid, None)
    _pending.pop(uid, None)
    _await_fill.pop(uid, None)
    _await_prov_field.pop(uid, None)
    _await_prov_new.pop(uid, None)
    _await_prov_import.pop(uid, None)
    _await_prov_find.pop(uid, None)
    _await_just.pop(uid, None)


# ---------------- МЕЛКИЕ ХЕЛПЕРЫ ----------------

def e(s) -> str:
    """Экранирование для HTML-сообщений Telegram — обязательное правило проекта."""
    return html.escape(str(s or ""))


def parse_amount(s) -> float:
    """'1.234,56' / '1 234,56 €' / '1234.56' -> 1234.56. Не распознал — 0.0."""
    if s is None:
        return 0.0
    txt = str(s)
    txt = txt.replace("€", "").replace(" ", " ").strip()
    txt = "".join(ch for ch in txt if ch.isdigit() or ch in ".,-")
    if not txt:
        return 0.0
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".") if txt.rfind(",") > txt.rfind(".") \
            else txt.replace(",", "")
    elif "," in txt:
        txt = txt.replace(",", ".")
    try:
        return round(float(txt), 2)
    except ValueError:
        return 0.0


def money(v) -> str:
    v = parse_amount(v) if not isinstance(v, (int, float)) else float(v)
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def new_id() -> str:
    """Короткий уникальный ID фактуры: F + ггммдд + ччммсс."""
    return "F" + core.now_local().strftime("%y%m%d%H%M%S")


def loc_label(code: str) -> str:
    l = core.LOCALES.get(code)
    return f"{l['emoji']} {l['name']}" if l else code


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rowlist, back: bool = True) -> InlineKeyboardMarkup:
    rows_ = [r for r in rowlist if r]
    if back:
        rows_.append([btn("⬅️ Назад", "bk")])
    return InlineKeyboardMarkup(inline_keyboard=rows_)


# ---------------- ДОСТУП ----------------

def is_findir(u, uid: int) -> bool:
    if uid in FIN_DIRECTOR_IDS:
        return True
    if not u:
        return False
    if str(u.get("Роль", "")).strip() == ROLE_FINDIR:
        return True
    return PATRON_FULL_ACCESS and core.is_patron(u)


def findir_ids() -> list:
    """Кому слать уведомления о новых фактурах. Нет фин. директоров — патронам."""
    out = list(FIN_DIRECTOR_IDS)
    for r in core.rows(core.USERS_WS):
        if str(r.get("Роль", "")).strip() == ROLE_FINDIR and str(r.get("Статус", "")).strip() == "active":
            try:
                out.append(int(str(r.get("ID")).strip()))
            except (ValueError, TypeError):
                pass
    if not out:
        out = core.patrons()
    return sorted(set(out))


async def deny(c: CallbackQuery):
    await c.answer("Нет доступа", show_alert=True)


# ---------------- РАБОТА С ЛИСТАМИ ----------------

_cols_cache = {}   # ws_name -> (ts, {header: index_1based})


def col_index(ws_name: str) -> dict:
    """Позиции колонок читаем из самой таблицы — если колонки подвинули руками,
    бот всё равно пишет туда, куда нужно, а не по жёсткому номеру."""
    ts, data = _cols_cache.get(ws_name, (0, {}))
    if time() - ts > 300 or not data:
        head = core.ws(ws_name).row_values(1)
        data = {str(h).strip(): i + 1 for i, h in enumerate(head) if str(h).strip()}
        _cols_cache[ws_name] = (time(), data)
    return data


def append_factura(rec: dict) -> str:
    w = core.ws(FACTURAS_WS)
    order = FIN_HEADERS[FACTURAS_WS]
    core.sheets_write_retry(w.append_row, [rec.get(h, "") for h in order],
                            value_input_option="RAW")
    core.drop_cache(FACTURAS_WS)
    return rec["ID"]


def facturas(loc: str = None, statuses=None, force=False) -> list:
    """[(row_idx, record)] — row_idx это номер строки в листе (шапка = 1)."""
    out = []
    for i, r in enumerate(core.rows(FACTURAS_WS, force=force)):
        if loc and str(r.get("Локаль", "")).strip() != loc:
            continue
        if statuses and str(r.get("Статус", "")).strip() not in statuses:
            continue
        out.append((i + 2, r))
    return out


def find_factura(fid: str):
    for idx, r in facturas():
        if str(r.get("ID", "")).strip() == fid:
            return idx, r
    return None, None


def _a1(row: int, col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def set_fields(ws_name: str, row_idx: int, values: dict):
    """Точечно обновляет ячейки строки — одним batch-запросом, чтобы не жечь
    лимит Google API. Чужие строки, шапку и форматирование не трогает."""
    cols = col_index(ws_name)
    w = core.ws(ws_name)
    data = []
    for header, val in values.items():
        col = cols.get(header)
        if not col:
            log.warning("нет колонки %s в %s", header, ws_name)
            continue
        data.append({"range": _a1(row_idx, col), "values": [[val]]})
    if not data:
        return
    core.sheets_write_retry(w.batch_update, data, value_input_option="RAW")
    core.drop_cache(ws_name)


def proveedores(force=False) -> list:
    return core.rows(PROVEEDORES_WS, force=force)


def find_proveedor(name: str):
    """Ищем по названию и по алиасам (в фактурах поставщик пишется по-разному)."""
    n = norm(name)
    if not n:
        return None
    for r in proveedores():
        if norm(r.get("Поставщик")) == n:
            return r
    for r in proveedores():
        aliases = [norm(a) for a in str(r.get("Алиасы", "")).split(",") if norm(a)]
        if any(a and (a in n or n in a) for a in aliases):
            return r
        pn = norm(r.get("Поставщик"))
        if pn and (pn in n or n in pn):
            return r
    return None


def add_proveedor(name: str, iban: str, nif: str, author: str, aliases: str = "",
                  locales: str = "", comment: str = ""):
    w = core.ws(PROVEEDORES_WS)
    order = FIN_HEADERS[PROVEEDORES_WS]
    rec = {"Поставщик": name, "Алиасы": aliases, "NIF": nif, "IBAN": iban,
           "BIC": bic_by_iban(iban), "Страна": "ES", "Локали": locales,
           "Комментарий": comment, "Кто добавил": author,
           "Дата": core.now_local().strftime("%d.%m.%Y %H:%M")}
    core.sheets_write_retry(w.append_row, [rec.get(h, "") for h in order],
                            value_input_option="RAW")
    core.drop_cache(PROVEEDORES_WS)


def add_proveedores_bulk(records: list, author: str) -> int:
    """Пачкой — один запрос к таблице вместо одного на каждого поставщика."""
    if not records:
        return 0
    w = core.ws(PROVEEDORES_WS)
    order = FIN_HEADERS[PROVEEDORES_WS]
    now = core.now_local().strftime("%d.%m.%Y %H:%M")
    rows_ = []
    for r in records:
        iban = r.get("iban", "")
        rec = {"Поставщик": r.get("name", ""), "Алиасы": r.get("aliases", ""),
               "NIF": r.get("nif", ""), "IBAN": iban,
               "BIC": r.get("bic") or bic_by_iban(iban),
               "Страна": iban[:2].upper() or "ES",
               "Кто добавил": author, "Дата": now}
        rows_.append([rec.get(h, "") for h in order])
    core.sheets_write_retry(w.append_rows, rows_, value_input_option="RAW")
    core.drop_cache(PROVEEDORES_WS)
    return len(rows_)


def proveedores_indexed(force=False) -> list:
    """[(row_idx, запись)] — row_idx это номер строки листа."""
    return [(i + 2, r) for i, r in enumerate(proveedores(force=force))]


def find_proveedor_row(name: str):
    n = norm(name)
    for idx, r in proveedores_indexed():
        if norm(r.get("Поставщик")) == n:
            return idx, r
    return None, None


# IBAN -> BIC по коду банка (позиции 5–8 испанского IBAN).
# Коды сверены с выгрузкой получателей из Santander.
BIC_BY_BANK = {
    "0007": "BESCPTPLXXX",   # Novo Banco
    "0030": "BSCHESMMXXX",   # Banesto / Santander
    "0049": "BSCHESMMXXX",   # Santander
    "0073": "OPENESMMXXX",   # Openbank
    "0075": "BSCHESMMXXX",   # Banco Popular → Santander
    "0081": "BSABESBBXXX",   # Sabadell
    "0128": "BKBKESMMXXX",   # Bankinter
    "0182": "BBVAESMMXXX",   # BBVA
    "0234": "CAHMESMMXXX",   # Banco Caminos
    "1465": "INGDESMMXXX",   # ING
    "1491": "TRIOESMMXXX",   # Triodos
    "1563": "NTSBESM1XXX",   # N26
    "1583": "REVOESM2XXX",   # Revolut
    "2038": "CAHMESMMXXX",   # Bankia (ист.)
    "2080": "CAGLESMMXXX",   # Abanca
    "2095": "BASKES2BXXX",   # Kutxabank
    "2100": "CAIXESBBXXX",   # CaixaBank
    "3025": "CDENESBBXXX",   # Caixa d'Enginyers
    "3058": "CCRIES2AXXX",   # Cajamar
    "3110": "CCRIES2A110",   # Caja Rural
    "3159": "BCOEESMM159",   # Caja Rural
    "3162": "BCOEESMM162",   # Caja Rural
}


def iban_clean(s) -> str:
    return "".join(str(s or "").split()).upper()


def iban_valid(s) -> bool:
    """Проверка контрольного числа IBAN (mod-97). Опечатку в реквизитах ловим
    здесь, а не отказом банка по всей ремесе."""
    v = iban_clean(s)
    if len(v) < 15 or len(v) > 34 or not v[:2].isalpha() or not v[2:4].isdigit():
        return False
    moved = v[4:] + v[:4]
    digits = ""
    for ch in moved:
        if ch.isdigit():
            digits += ch
        elif ch.isalpha():
            digits += str(ord(ch) - 55)
        else:
            return False
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def bic_by_iban(s) -> str:
    v = iban_clean(s)
    if v.startswith("ES") and len(v) >= 8:
        return BIC_BY_BANK.get(v[4:8], "")
    return ""


def mask_iban(s) -> str:
    v = iban_clean(s)
    return f"{v[:4]}…{v[-4:]}" if len(v) > 10 else (v or "—")


# ---------------- ЭКРАН: КОРЕНЬ БЛОКА (перехватывает d:fin) ----------------

async def _root_screen(c: CallbackQuery, u):
    """Рисует корневой экран блока. Ответ на колбэк — на совести вызывающего."""
    uid = c.from_user.id
    core.nav_push(uid, "d:fin")
    head = core.crumb("fin", core.ulang(u))

    if not is_findir(u, uid):
        mine = [1 for _, r in facturas() if str(r.get("AuthorID", "")).strip() == str(uid)]
        await core.take_over(
            c, f"{head}\n\nЗагрузи документ на оплату — я передам его финансовому отделу."
               + (f"\n\nТвоих документов в работе: <b>{len(mine)}</b>" if mine else ""),
            kb([[btn("💳 Оплата — загрузить документ", "fin:up")],
                [btn("🌐 Статус моих платежей", "fin:web")]]))
        return

    rows_ = [[btn(f"{loc_label(code)}", f"fin:l:{code}")] for code in core.LOCALES]
    rows_.append([btn("💳 Оплата — загрузить документ", "fin:up")])
    try:
        n_prov = len(proveedores())
    except Exception:
        n_prov = 0
    rows_.append([btn(f"📇 Поставщики ({n_prov})", "fin:prov")])
    rows_.append([btn("🌐 Статус платежей", "fin:web")])
    n = len(facturas(statuses=UNPAID))
    await core.take_over(c, f"{head}\n\nНеоплаченных документов всего: <b>{n}</b>", kb(rows_))


async def cb_root(c: CallbackQuery):
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    await _root_screen(c, u)
    await c.answer()


# ---------------- ЗАГРУЗКА ДОКУМЕНТА ----------------

async def cb_upload(c: CallbackQuery):
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    uid = c.from_user.id
    _clear(uid)
    _await_doc[uid] = time()
    await core.take_over(
        c,
        "💳 <b>Оплата</b>\n\nПришли документ на оплату — фото или PDF.\n"
        "Дальше спрошу локаль и нужен ли тебе хустификанте.",
        kb([[btn("❌ Отмена", "fin:cancel")]], back=False))
    await c.answer()


async def cb_cancel(c: CallbackQuery):
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    _clear(c.from_user.id)
    await _root_screen(c, u)
    await c.answer("Отменено")


async def on_doc(m: Message):
    """Фото или файл от сотрудника, который нажал «Оплата»."""
    uid = m.from_user.id
    if not _fresh(_await_doc.get(uid)):
        _await_doc.pop(uid, None)
        return
    _await_doc.pop(uid, None)

    if m.photo:
        file_id, ftype, fname = m.photo[-1].file_id, "photo", "фото"
        mime = "image/jpeg"
    elif m.document:
        file_id, ftype = m.document.file_id, "document"
        fname = m.document.file_name or "файл"
        mime = str(getattr(m.document, "mime_type", "") or "").strip()
        if not mime:
            low = fname.lower()
            mime = ("application/pdf" if low.endswith(".pdf")
                    else "image/png" if low.endswith(".png")
                    else "image/jpeg")
    else:
        return

    _pending[uid] = {
        "ts": time(), "file_id": file_id, "ftype": ftype, "fname": fname, "mime": mime,
        "chat_id": m.chat.id, "msg_id": m.message_id,
        "author": m.from_user.full_name, "text": (m.caption or "")[:500],
    }
    rows_ = [[btn(loc_label(code), f"fin:loc:{code}")] for code in core.LOCALES]
    rows_.append([btn("❌ Отмена", "fin:cancel")])
    await m.answer(f"Принял: <b>{e(fname)}</b>\n\nВ какую локаль?",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_))


async def cb_pick_loc(c: CallbackQuery):
    uid = c.from_user.id
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    p = _pending.get(uid)
    if not p or not _fresh(p.get("ts")):
        _pending.pop(uid, None)
        await _root_screen(c, u)
        await c.answer("Загрузка устарела, начни заново", show_alert=True)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    p["loc"] = loc
    p["ts"] = time()
    await core.take_over(
        c,
        f"{loc_label(loc)}\n\nНужен ли тебе <b>хустификанте</b> — подтверждение оплаты?\n"
        f"Если да, пришлю его автоматически, как только платёж пройдёт.",
        kb([[btn("✅ Да, нужен", "fin:just:1"), btn("➖ Не нужен", "fin:just:0")],
            [btn("❌ Отмена", "fin:cancel")]], back=False))
    await c.answer()


async def cb_just(c: CallbackQuery):
    uid = c.from_user.id
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    p = _pending.get(uid)
    if not p or not p.get("loc") or not _fresh(p.get("ts")):
        _pending.pop(uid, None)
        await _root_screen(c, u)
        await c.answer("Загрузка устарела, начни заново", show_alert=True)
        return
    need = c.data.split(":")[2] == "1"
    _pending.pop(uid, None)

    now = core.now_local()
    fid = new_id()
    rec = {
        "ID": fid,
        "Дата": now.strftime("%d.%m.%Y"), "Время": now.strftime("%H:%M"),
        "Локаль": p["loc"], "Автор": p["author"], "AuthorID": str(uid),
        "ChatID": str(p["chat_id"]), "MessageID": str(p["msg_id"]),
        "FileID": p["file_id"], "Тип файла": p["ftype"],
        "Поставщик": p.get("text", "")[:120], "Хустификанте": "да" if need else "нет",
        "Статус": ST_NEW,
    }
    try:
        append_factura(rec)
    except Exception as ex:
        log.exception("не удалось записать фактуру: %s", ex)
        await c.answer("Не смог записать в таблицу, попробуй ещё раз", show_alert=True)
        return

    ocr_note = "\n\n🔍 Читаю данные с документа, это займёт несколько секунд." \
        if ocr_enabled() else ""
    await core.take_over(
        c,
        f"✅ Документ принят\n\n{loc_label(p['loc'])}\n"
        f"Хустификанте: <b>{'нужен' if need else 'не нужен'}</b>\n"
        f"Номер: <code>{fid}</code>{ocr_note}",
        kb([[btn("💳 Загрузить ещё", "fin:up")]]))
    await c.answer()
    if ocr_enabled():
        # не дёргаем фин. директора дважды: сообщением будет результат распознавания
        asyncio.create_task(recognize_later(fid, p["file_id"], p.get("mime", "")))
    else:
        await notify_findirs(rec)


# ---------------- РАСПОЗНАВАНИЕ ФАКТУРЫ ----------------

def ocr_enabled() -> bool:
    try:
        import invoice_ocr
        return invoice_ocr.enabled()
    except Exception:
        return False


async def recognize_later(fid: str, file_id: str, mime: str):
    """Фоновая задача: скачать документ, распознать, заполнить строку,
    показать фин. директору карточку с кнопкой подтверждения."""
    try:
        import invoice_ocr
    except Exception as ex:
        log.warning("модуль распознавания недоступен: %s", ex)
        return

    try:
        buf = await core.bot.download(file_id)
        data = buf.read()
    except Exception as ex:
        log.warning("не смог скачать документ %s: %s", fid, ex)
        await ocr_report(fid, None, f"не смогла скачать файл ({type(ex).__name__})")
        return

    try:
        res = await asyncio.to_thread(invoice_ocr.recognize, data, mime or "image/jpeg")
    except Exception as ex:
        log.exception("распознавание упало: %s", ex)
        await ocr_report(fid, None, f"{type(ex).__name__}: {ex}")
        return

    if not res.get("ok"):
        await ocr_report(fid, None, res.get("error", "не получилось"))
        return

    idx, r = find_factura(fid)
    if not r:
        log.warning("строка %s исчезла, пока шло распознавание", fid)
        return

    name = res.get("proveedor", "")
    prov = find_proveedor(name) if name else None
    prov_iban = iban_clean(prov.get("IBAN")) if prov else ""
    doc_iban = iban_clean(res.get("iban"))
    iban = prov_iban or (doc_iban if iban_valid(doc_iban) else "")

    total = res.get("total") or 0.0
    base, iva = res.get("base") or 0.0, res.get("iva") or 0.0

    # Чего не хватает, чтобы платить без вопросов
    blockers = []
    if not name:
        blockers.append("поставщик не распознан")
    if not total:
        blockers.append("сумма не распознана")
    if not iban:
        blockers.append("нет IBAN")
    elif not iban_valid(iban):
        blockers.append("IBAN не проходит проверку")
    if doc_iban and prov_iban and doc_iban != prov_iban:
        blockers.append("🔴 IBAN в фактуре не совпадает со справочником — "
                        "проверь реквизиты с поставщиком")
    if base and iva and total and abs(base + iva - total) > SUM_TOLERANCE:
        blockers.append(f"не сходится арифметика: {money(base)} + {money(iva)} "
                        f"≠ {money(total)}")
    if not res.get("numero"):
        blockers.append("нет номера фактуры")
    if not res.get("fecha"):
        blockers.append("нет даты фактуры")
    if name and not prov and not AUTO_APPROVE_NEW_SUPPLIER:
        blockers.append("новый поставщик — реквизиты ещё не сверялись")

    auto = AUTO_APPROVE and not blockers
    if auto:
        status = ST_READY
    elif not total:
        status = ST_NEW
    elif not iban_valid(iban):
        status = ST_NEED_IBAN
    else:
        status = ST_RECOGNIZED
    fields = {
        "Поставщик": name, "NIF": res.get("nif", ""), "IBAN": iban,
        "Номер": res.get("numero", ""), "Дата фактуры": res.get("fecha", ""),
        "База": f"{res.get('base', 0):.2f}" if res.get("base") else "",
        "IVA": f"{res.get('iva', 0):.2f}" if res.get("iva") else "",
        "Total": f"{total:.2f}" if total else "",
        "Статус": status,
    }
    try:
        set_fields(FACTURAS_WS, idx, fields)
    except Exception as ex:
        log.exception("не смог записать распознанное: %s", ex)
        await ocr_report(fid, None, "таблица не приняла запись")
        return

    await ocr_report(fid, res, None, blockers, auto=auto)


async def ocr_report(fid: str, res, error: str = None, blockers: list = None,
                     auto: bool = False):
    _, r = find_factura(fid)
    if not r:
        return
    if error:
        text = (f"🔍 <b>Не удалось распознать</b>\n"
                f"Фактура <code>{e(fid)}</code>\n\n{e(error)}\n\n"
                f"Заполни данные вручную.")
        rows_ = [[btn("✏️ Заполнить вручную", f"fin:fill:{fid}")]]
    elif auto:
        # всё сошлось — ничего не спрашиваем, просто ставим в известность
        text = (f"✅ <b>{e(r.get('Поставщик'))}</b> · {money(r.get('Total'))}\n"
                f"{loc_label(str(r.get('Локаль', '')).strip())} · "
                f"фактура {e(r.get('Номер'))} от {e(r.get('Дата фактуры'))}\n\n"
                f"Распознано и поставлено в ремесу.")
        rows_ = [[btn("Открыть", f"fin:doc:{fid}")]]
    else:
        text = "🔍 <b>Нужна твоя проверка</b>\n\n" + card_text(r)
        if blockers:
            text += "\n\n" + "\n".join("⚠️ " + w for w in blockers)
        rows_ = [[btn("✅ Подтвердить", f"fin:ok:{fid}")],
                 [btn("✏️ Исправить", f"fin:fill:{fid}")],
                 [btn("📎 Показать документ", f"fin:show:{fid}")]]
    markup = InlineKeyboardMarkup(inline_keyboard=rows_)
    for uid in findir_ids():
        try:
            await core.bot.send_message(uid, text[:4000], reply_markup=markup)
        except Exception as ex:
            log.warning("отчёт о распознавании не ушёл %s: %s", uid, ex)


async def cb_confirm(c: CallbackQuery):
    """Фин. директор подтверждает распознанное — фактура идёт в ремесу."""
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    idx, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return

    iban = iban_clean(r.get("IBAN"))
    name = str(r.get("Поставщик", "")).strip()
    total = parse_amount(r.get("Total"))
    if not total:
        await c.answer("Нет суммы — заполни вручную", show_alert=True)
        return
    if not iban_valid(iban):
        set_fields(FACTURAS_WS, idx, {"Статус": ST_NEED_IBAN})
        await _render_card(c, fid, "Нужен IBAN")
        return

    set_fields(FACTURAS_WS, idx, {"Статус": ST_READY})
    note = "Готово к ремесе"
    if name and not find_proveedor(name):
        try:
            add_proveedor(name, iban, str(r.get("NIF", "")), c.from_user.full_name)
            note = "Готово к ремесе · поставщик добавлен в справочник"
        except Exception as ex:
            log.warning("поставщик не добавлен: %s", ex)
    await _render_card(c, fid, note)


async def notify_findirs(rec: dict):
    text = (f"🆕 <b>Новый документ на оплату</b>\n"
            f"{loc_label(rec['Локаль'])}\n"
            f"От: {e(rec['Автор'])}\n"
            f"Хустификанте: {rec['Хустификанте']}\n"
            f"Номер: <code>{rec['ID']}</code>")
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("Открыть", f"fin:doc:{rec['ID']}")]])
    for pid in findir_ids():
        if str(pid) == str(rec.get("AuthorID")):
            continue
        try:
            await core.bot.send_message(pid, text, reply_markup=markup)
        except Exception as ex:
            log.warning("уведомление фин.диру %s не ушло: %s", pid, ex)


# ---------------- ЭКРАНЫ ФИН. ДИРЕКТОРА ----------------

async def cb_loc_menu(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    core.nav_push(c.from_user.id, c.data)

    unpaid = facturas(loc, UNPAID)
    ready = [r for _, r in unpaid if str(r.get("Статус")).strip() != ST_EXCLUDED]
    total = sum(parse_amount(r.get("Total")) for r in ready)
    in_bank = facturas(loc, PAID_QUEUE)
    paid = facturas(loc, (ST_PAID,))

    text = (f"💶 <b>Финансы</b> · {loc_label(loc)}\n\n"
            f"Неоплаченные: <b>{len(unpaid)}</b> на {money(total)}\n"
            f"В банке / ждут хустификанте: <b>{len(in_bank)}</b>\n"
            f"Оплачено: <b>{len(paid)}</b>")
    await core.take_over(c, text, kb([
        [btn(f"🧾 Неоплаченные ({len(unpaid)})", f"fin:unp:{loc}")],
        [btn(f"✅ Оплаченные ({len(in_bank) + len(paid)})", f"fin:pay:{loc}")],
        [btn("🗂 Архив фактур", f"fin:arch:{loc}")],
        [btn("▶️ Сформировать ремесу", f"fin:rem:{loc}")],
        [btn("💳 Загрузить документ", "fin:up")],
    ]))
    await c.answer()


def doc_label(r: dict) -> str:
    st = ST_EMOJI.get(str(r.get("Статус", "")).strip(), "•")
    name = str(r.get("Поставщик") or "без поставщика").strip()[:24]
    total = parse_amount(r.get("Total"))
    tail = money(total) if total else "сумма не указана"
    return f"{st} {name} · {tail}"


async def _list_screen(c: CallbackQuery, loc: str, statuses, title: str, empty: str):
    items = facturas(loc, statuses)
    core.nav_push(c.from_user.id, c.data)
    if not items:
        await core.take_over(c, f"{title}\n{loc_label(loc)}\n\n{empty}", kb([]))
        await c.answer()
        return
    total = sum(parse_amount(r.get("Total")) for _, r in items
                if str(r.get("Статус")).strip() != ST_EXCLUDED)
    rows_ = [[btn(doc_label(r), f"fin:doc:{r.get('ID')}")] for _, r in items[:LIST_LIMIT]]
    more = f"\n\nПоказаны первые {LIST_LIMIT} из {len(items)}." if len(items) > LIST_LIMIT else ""
    await core.take_over(
        c, f"{title}\n{loc_label(loc)}\n\nДокументов: <b>{len(items)}</b> · "
           f"итого <b>{money(total)}</b>{more}", kb(rows_))
    await c.answer()


async def cb_unpaid(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    await _list_screen(c, loc, UNPAID, "🧾 <b>Неоплаченные</b>", "Пусто — всё оплачено.")


async def cb_paid(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    await _list_screen(c, loc, PAID_QUEUE + (ST_PAID,), "✅ <b>Оплаченные</b>",
                       "Пока ничего не оплачено.")


async def cb_archive(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    await _list_screen(c, loc, None, "🗂 <b>Архив фактур</b>", "Архив пуст.")


# ---------------- КАРТОЧКА ФАКТУРЫ ----------------

def card_text(r: dict) -> str:
    st = str(r.get("Статус", "")).strip()
    lines = [
        f"{ST_EMOJI.get(st, '•')} <b>{e(r.get('Поставщик') or 'Поставщик не указан')}</b>",
        f"{loc_label(str(r.get('Локаль', '')).strip())} · {e(r.get('Дата'))} {e(r.get('Время'))}",
        "",
        f"Номер фактуры: <b>{e(r.get('Номер') or '—')}</b>",
        f"Дата фактуры: {e(r.get('Дата фактуры') or '—')}",
        f"NIF: {e(r.get('NIF') or '—')}",
        f"IBAN: <code>{e(mask_iban(r.get('IBAN')))}</code>",
        f"Сумма: <b>{money(r.get('Total'))}</b>",
        "",
        f"Загрузил: {e(r.get('Автор'))}",
        f"Хустификанте: {e(r.get('Хустификанте'))}",
        f"Статус: <b>{e(st)}</b>",
        f"ID: <code>{e(r.get('ID'))}</code>",
    ]
    return "\n".join(lines)


async def _render_card(c: CallbackQuery, fid: str, note: str = None) -> bool:
    _, r = find_factura(fid)
    if not r:
        return False
    core.nav_push(c.from_user.id, f"fin:doc:{fid}")
    st = str(r.get("Статус", "")).strip()

    rows_ = [[btn("📎 Показать документ", f"fin:show:{fid}")],
             [btn("✏️ Заполнить данные", f"fin:fill:{fid}")]]
    if st in (ST_RECOGNIZED, ST_NEED_IBAN):
        rows_.insert(0, [btn("✅ Подтвердить", f"fin:ok:{fid}")])
    if st == ST_EXCLUDED:
        rows_.append([btn("↩️ Вернуть в очередь", f"fin:inc:{fid}")])
    elif st in (ST_NEW, ST_RECOGNIZED, ST_NEED_IBAN, ST_READY):
        rows_.append([btn("🚫 Исключить из ремесы", f"fin:exc:{fid}")])
    if st in (ST_READY, ST_IN_REMESA, ST_SENT):
        rows_.append([btn("💸 Оплачено + хустификанте", f"fin:paid:{fid}")])
    if str(r.get("ХустификантеFileID", "")).strip():
        rows_.append([btn("🧾 Показать хустификанте", f"fin:jst:{fid}")])
    rows_.append([btn("🗑 Удалить документ", f"fin:del:{fid}")])
    await core.take_over(c, card_text(r), kb(rows_))
    await c.answer(note or "")
    return True


async def cb_card(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    if not await _render_card(c, fid):
        await c.answer("Документ не найден", show_alert=True)


async def cb_show(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    _, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    just = c.data.startswith("fin:jst:")
    file_id = str(r.get("ХустификантеFileID" if just else "FileID", "")).strip()
    if not file_id:
        await c.answer("Файл не сохранён", show_alert=True)
        return
    cap = ("🧾 Хустификанте · " if just else "") + \
          f"{e(r.get('Поставщик') or '')} · {money(r.get('Total'))}"
    as_photo = (not just) and str(r.get("Тип файла")).strip() == "photo"
    chat = c.message.chat.id
    try:
        # хустификанте пришёл фото или файлом — пробуем оба варианта
        try:
            if as_photo:
                await core.bot.send_photo(chat, file_id, caption=cap)
            else:
                await core.bot.send_document(chat, file_id, caption=cap)
        except Exception:
            if as_photo:
                await core.bot.send_document(chat, file_id, caption=cap)
            else:
                await core.bot.send_photo(chat, file_id, caption=cap)
        await c.answer()
    except Exception as ex:
        log.warning("не смог отправить файл %s: %s", fid, ex)
        await c.answer("Файл недоступен", show_alert=True)


FILL_HELP = (
    "✏️ <b>Правка данных</b>\n\n"
    "Пришли только то, что нужно поправить — по строке на поле, в любом порядке. "
    "Что это за поле, я пойму сама:\n\n"
    "• <code>ES91 2100 …</code> — IBAN\n"
    "• <code>1234,56</code> — сумма к оплате\n"
    "• <code>15.09.2026</code> — дата фактуры\n"
    "• <code>№ 2026/192</code> или <code>F-2026/192</code> — номер фактуры\n"
    "• <code>B10467371</code> — NIF\n"
    "• всё остальное — название поставщика\n\n"
    "Можно одной строкой: пришлёшь только IBAN — поправлю только его."
)

# Явные подсказки, если хочется указать поле руками: «сумма: 1234,56»
FIELD_HINTS = {
    "поставщик": "Поставщик", "proveedor": "Поставщик",
    "номер": "Номер", "фактура": "Номер", "factura": "Номер",
    "дата": "Дата фактуры", "fecha": "Дата фактуры",
    "сумма": "Total", "итого": "Total", "total": "Total",
    "iban": "IBAN", "ибан": "IBAN",
    "nif": "NIF", "cif": "NIF", "ниф": "NIF",
    "база": "База", "base": "База",
    "iva": "IVA", "ндс": "IVA",
}


def looks_like_iban(s: str) -> bool:
    v = iban_clean(s)
    return len(v) >= 15 and v[:2].isalpha() and v[2:4].isdigit()


def looks_like_nif(s: str) -> bool:
    v = str(s or "").replace("-", "").replace(" ", "").upper()
    return bool(re.fullmatch(r"[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z]", v))


def looks_like_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", str(s or "").strip()))


def looks_like_amount(s: str) -> bool:
    v = str(s or "").replace("€", "").strip()
    return bool(re.fullmatch(r"[\d\s.,]+", v)) and any(c.isdigit() for c in v) \
        and parse_amount(v) > 0


def looks_like_numero(s: str) -> bool:
    v = str(s or "").strip()
    if len(v) > 24 or not any(c.isdigit() for c in v):
        return False
    return bool(re.search(r"[/\-№]", v)) or v.lower().startswith(("f", "fra", "nº", "no"))


def parse_fill(text: str) -> tuple:
    """Разбирает присланные строки по полям. -> (что записать, что не понято)"""
    fields, unknown = {}, []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        # «сумма: 1234,56» — поле названо явно
        m = re.match(r"^\s*([A-Za-zА-Яа-яё ]{2,14})\s*[:=]\s*(.+)$", line)
        if m:
            hint = FIELD_HINTS.get(m.group(1).strip().lower())
            if hint:
                value = m.group(2).strip()
                fields[hint] = (iban_clean(value) if hint == "IBAN"
                                else f"{parse_amount(value):.2f}" if hint in ("Total", "База", "IVA")
                                else value)
                continue

        if looks_like_iban(line):
            fields["IBAN"] = iban_clean(line)
        elif looks_like_date(line):
            fields["Дата фактуры"] = _norm_fill_date(line)
        elif looks_like_nif(line):
            fields["NIF"] = line.replace(" ", "").upper()
        elif looks_like_amount(line):
            fields["Total"] = f"{parse_amount(line):.2f}"
        elif looks_like_numero(line):
            fields["Номер"] = line.lstrip("№ ").strip()
        elif len(line) >= 3:
            fields["Поставщик"] = line
        else:
            unknown.append(line)
    return fields, unknown


def _norm_fill_date(s: str) -> str:
    m = re.match(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$", str(s))
    if not m:
        return str(s).strip()
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{int(d):02d}.{int(mo):02d}.{y}"


async def cb_fill(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    idx, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    _await_fill[c.from_user.id] = (fid, time())
    await core.take_over(c, FILL_HELP, kb([[btn("❌ Отмена", f"fin:doc:{fid}")]], back=False))
    await c.answer()


async def on_fill_text(m: Message):
    """Ручная правка: принимаем ровно то, что прислали — хоть одну строку."""
    uid = m.from_user.id
    state = _await_fill.get(uid)
    if not state or not _fresh(state[1]):
        _await_fill.pop(uid, None)
        return
    fid = state[0]

    idx, r = find_factura(fid)
    if not r:
        _await_fill.pop(uid, None)
        await m.answer("Документ не найден — видимо, строку удалили из таблицы.")
        return

    fields, unknown = parse_fill(m.text or "")
    if not fields:
        await m.answer("Не поняла, что править. Пришли IBAN, сумму, дату, номер "
                       "или название поставщика — можно по одной строке.")
        return

    # IBAN проверяем контрольным числом — опечатку ловим здесь
    iban_in = fields.get("IBAN", "")
    if iban_in and not iban_valid(iban_in):
        await m.answer("IBAN не проходит проверку контрольного числа. Проверь и пришли снова.")
        return
    fecha = fields.get("Дата фактуры", "")
    if fecha and not core.parse_ddmmyyyy(fecha):
        await m.answer("Дата не распознана, формат дд.мм.гггг. Попробуй ещё раз.")
        return

    warn = ""
    name = fields.get("Поставщик") or str(r.get("Поставщик", "")).strip()
    prov = find_proveedor(name) if name else None
    prov_iban = iban_clean(prov.get("IBAN")) if prov else ""

    # прислали поставщика, а IBAN не прислали — подставим из справочника
    if not iban_in and prov_iban and not iban_valid(r.get("IBAN")):
        fields["IBAN"] = prov_iban
        if prov.get("NIF") and not str(r.get("NIF", "")).strip():
            fields.setdefault("NIF", str(prov.get("NIF")))
        warn += "\n\n➕ IBAN подставлен из справочника."
    if iban_in and prov_iban and iban_in != prov_iban:
        warn += ("\n\n🔴 <b>Внимание:</b> присланный IBAN не совпадает с тем, что в "
                 f"справочнике ({mask_iban(prov_iban)}). Записала присланный — проверь "
                 "реквизиты с поставщиком, это классическая схема подмены счёта.")

    # статус пересчитываем по тому, что получилось в строке после правки
    merged = dict(r)
    merged.update(fields)
    total = parse_amount(merged.get("Total"))
    iban_final = iban_clean(merged.get("IBAN"))
    old_status = str(r.get("Статус", "")).strip()
    if old_status in (ST_IN_REMESA, ST_SENT, ST_PAID):
        pass                                   # оплаченное не трогаем
    elif total and iban_valid(iban_final) and merged.get("Номер") and merged.get("Дата фактуры"):
        fields["Статус"] = ST_READY
    elif total and not iban_valid(iban_final):
        fields["Статус"] = ST_NEED_IBAN
    elif total:
        fields["Статус"] = ST_RECOGNIZED

    _await_fill.pop(uid, None)
    try:
        set_fields(FACTURAS_WS, idx, fields)
    except Exception as ex:
        log.exception("не смог обновить фактуру %s: %s", fid, ex)
        await m.answer("Не смогла записать в таблицу, попробуй ещё раз.")
        return

    if iban_in and name and not prov:
        try:
            add_proveedor(name, iban_in, str(merged.get("NIF", "")), m.from_user.full_name)
            warn += "\n\n➕ Поставщик добавлен в справочник."
        except Exception as ex:
            log.warning("поставщик не добавлен: %s", ex)

    changed = ", ".join(k for k in fields if k != "Статус")
    if unknown:
        warn += "\n\n⚠️ Не поняла строки: " + ", ".join(e(u) for u in unknown[:3])

    _, r2 = find_factura(fid)
    await m.answer(f"✅ Обновлено: {e(changed)}\n\n" + card_text(r2 or r) + warn,
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [btn("К документу", f"fin:doc:{fid}")]]))


# ---------------- ВЕБ-СТРАНИЦА СТАТУСА ПЛАТЕЖЕЙ ----------------

async def cb_web_link(c: CallbackQuery):
    """Личная ссылка на страницу статуса платежей. Видит каждый сотрудник —
    свои документы; фин. директор и патрон — все."""
    u = core.guard(c)
    if not u:
        await deny(c)
        return
    try:
        import fin_web
        await fin_web.send_link(c.message.chat.id, c.from_user.id)
        await c.answer()
    except Exception as ex:
        log.warning("страница статуса недоступна: %s", ex)
        await c.answer("Страница статуса ещё не настроена", show_alert=True)


# ---------------- УДАЛЕНИЕ, ОПЛАТА, ХУСТИФИКАНТЕ ----------------

async def cb_delete_ask(c: CallbackQuery):
    """Спрашиваем подтверждение — удаление строки необратимо."""
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    _, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    await core.take_over(
        c,
        f"🗑 <b>Удалить документ?</b>\n\n{card_text(r)}\n\n"
        f"Строка исчезнет из таблицы насовсем. Отменить будет нельзя.",
        kb([[btn("🗑 Да, удалить", f"fin:delok:{fid}")],
            [btn("↩️ Нет, оставить", f"fin:doc:{fid}")]], back=False))
    await c.answer()


async def cb_delete_do(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    idx, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    loc = str(r.get("Локаль", "")).strip()
    try:
        w = core.ws(FACTURAS_WS)
        core.sheets_write_retry(w.delete_rows, idx)
        core.drop_cache(FACTURAS_WS)
    except Exception as ex:
        log.exception("не смогла удалить строку %s: %s", fid, ex)
        await c.answer("Таблица не дала удалить строку", show_alert=True)
        return
    c2 = c.model_copy(update={"data": f"fin:unp:{loc}"}) if loc in core.LOCALES else c
    if loc in core.LOCALES:
        await cb_unpaid(c2)
    else:
        await _root_screen(c, u)
        await c.answer("Удалено")


async def cb_paid_ask(c: CallbackQuery):
    """Фин. директор оплатил в банке: ждём хустификанте или отметку без него."""
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    _, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    _await_just[c.from_user.id] = (fid, time())
    need = str(r.get("Хустификанте", "")).strip().lower() == "да"
    who = f"\n\nАвтор загрузки ({e(r.get('Автор'))}) просил хустификанте — " \
          f"перешлю ему сразу." if need else ""
    await core.take_over(
        c,
        f"💸 <b>Оплата</b>\n\n{e(r.get('Поставщик'))} · {money(r.get('Total'))}\n\n"
        f"Пришли хустификанте из банка — фото или PDF. Он привяжется к этой фактуре, "
        f"и процесс закроется.{who}",
        kb([[btn("✅ Отметить оплаченной без хустификанте", f"fin:paidonly:{fid}")],
            [btn("❌ Отмена", f"fin:doc:{fid}")]], back=False))
    await c.answer()


async def mark_paid(fid: str, author_name: str, just_file_id: str = "",
                    just_type: str = "") -> dict:
    idx, r = find_factura(fid)
    if not r:
        return {}
    fields = {"Статус": ST_PAID,
              "Дата оплаты": core.now_local().strftime("%d.%m.%Y %H:%M")}
    if just_file_id:
        fields["ХустификантеFileID"] = just_file_id
    set_fields(FACTURAS_WS, idx, fields)
    _, r2 = find_factura(fid)
    return r2 or r


async def send_justificante(r: dict, file_id: str, ftype: str):
    """Пересылаем хустификанте тому, кто грузил документ и ставил галочку."""
    if str(r.get("Хустификанте", "")).strip().lower() != "да":
        return
    try:
        uid = int(str(r.get("AuthorID", "")).strip())
    except (TypeError, ValueError):
        return
    cap = (f"💸 Оплачено: {e(r.get('Поставщик'))} · {money(r.get('Total'))}\n"
           f"{loc_label(str(r.get('Локаль', '')).strip())} · фактура {e(r.get('Номер'))}")
    try:
        if ftype == "photo":
            await core.bot.send_photo(uid, file_id, caption=cap)
        else:
            await core.bot.send_document(uid, file_id, caption=cap)
    except Exception as ex:
        log.warning("хустификанте автору %s не ушёл: %s", uid, ex)


async def cb_paid_only(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    _await_just.pop(c.from_user.id, None)
    r = await mark_paid(fid, c.from_user.full_name)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    await _render_card(c, fid, "Отмечено оплаченной")


async def on_justificante(m: Message):
    """Файл от фин. директора после нажатия «Оплата»."""
    uid = m.from_user.id
    state = _await_just.get(uid)
    if not state or not _fresh(state[1]):
        _await_just.pop(uid, None)
        return
    fid = state[0]
    if m.photo:
        file_id, ftype = m.photo[-1].file_id, "photo"
    elif m.document:
        file_id, ftype = m.document.file_id, "document"
    else:
        return
    _await_just.pop(uid, None)

    try:
        r = await mark_paid(fid, m.from_user.full_name, file_id, ftype)
    except Exception as ex:
        log.exception("не смогла записать хустификанте: %s", ex)
        await m.answer("Не смогла записать в таблицу, попробуй ещё раз.")
        return
    if not r:
        await m.answer("Документ не найден.")
        return
    await send_justificante(r, file_id, ftype)
    sent = " Автору отправлен." if str(r.get("Хустификанте", "")).lower() == "да" else ""
    await m.answer(f"✅ Хустификанте привязан, фактура закрыта.{sent}\n\n" + card_text(r),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [btn("К документу", f"fin:doc:{fid}")]]))


async def _set_status(c: CallbackQuery, status: str, note: str):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    fid = c.data.split(":")[2]
    idx, r = find_factura(fid)
    if not r:
        await c.answer("Документ не найден", show_alert=True)
        return
    set_fields(FACTURAS_WS, idx, {"Статус": status})
    await _render_card(c, fid, note)


async def cb_exclude(c: CallbackQuery):
    await _set_status(c, ST_EXCLUDED, "Исключено из ремесы")


async def cb_include(c: CallbackQuery):
    await _set_status(c, ST_READY, "Вернул в очередь")


# ---------------- СПРАВОЧНИК ПОСТАВЩИКОВ ----------------

PROV_PAGE = 12          # поставщиков на экране

PROV_FIELDS = {
    "iban": ("IBAN", "Пришли новый IBAN одной строкой. Проверю контрольное число "
                     "и сам подставлю BIC."),
    "nif": ("NIF", "Пришли NIF / CIF одной строкой."),
    "name": ("Поставщик", "Пришли новое название поставщика одной строкой."),
    "alias": ("Алиасы", "Пришли алиасы через запятую — так поставщик будет "
                        "узнаваться, как бы его ни написали в фактуре."),
    "loc": ("Локали", "Пришли локали через запятую: Reina, Francia, Panadería, Boi Boi."),
    "note": ("Комментарий", "Пришли комментарий одной строкой."),
}


def prov_label(r: dict) -> str:
    name = str(r.get("Поставщик") or "без названия").strip()[:26]
    iban = iban_clean(r.get("IBAN"))
    mark = "✅" if iban_valid(iban) else ("⚠️" if iban else "➖")
    return f"{mark} {name}"


def prov_card(r: dict) -> str:
    iban = iban_clean(r.get("IBAN"))
    if not iban:
        iban_line = "IBAN: <b>не заполнен</b>"
    elif iban_valid(iban):
        iban_line = f"IBAN: <code>{e(iban)}</code> ✅"
    else:
        iban_line = f"IBAN: <code>{e(iban)}</code>\n⚠️ <b>не проходит проверку — опечатка</b>"
    lines = [
        f"📇 <b>{e(r.get('Поставщик') or 'Без названия')}</b>",
        "",
        f"NIF: {e(r.get('NIF') or '—')}",
        iban_line,
        f"BIC: {e(r.get('BIC') or '—')}",
        f"Алиасы: {e(r.get('Алиасы') or '—')}",
        f"Локали: {e(r.get('Локали') or 'все')}",
    ]
    if str(r.get("Комментарий") or "").strip():
        lines.append(f"Комментарий: {e(r.get('Комментарий'))}")
    who, when = str(r.get("Кто добавил") or "").strip(), str(r.get("Дата") or "").strip()
    if who or when:
        lines += ["", f"<i>Добавил: {e(who or '—')} · {e(when or '—')}</i>"]
    return "\n".join(lines)


async def _prov_list_screen(c: CallbackQuery, page: int = 0):
    uid = c.from_user.id
    query = _prov_query.get(uid, "")
    items = proveedores_indexed()
    if query:
        q = norm(query)
        items = [(i, r) for i, r in items
                 if q in norm(r.get("Поставщик")) or q in norm(r.get("Алиасы"))
                 or q in norm(r.get("NIF")) or q in norm(r.get("IBAN"))]

    pages = max(1, (len(items) + PROV_PAGE - 1) // PROV_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PROV_PAGE:(page + 1) * PROV_PAGE]

    bad = sum(1 for _, r in proveedores_indexed() if not iban_valid(r.get("IBAN")))
    head = ["📇 <b>Справочник поставщиков</b>", ""]
    if query:
        head.append(f"Поиск: «{e(query)}» — найдено {len(items)}")
    else:
        head.append(f"Всего: <b>{len(items)}</b>" + (f" · без нормального IBAN: {bad}" if bad else ""))
    if pages > 1:
        head.append(f"Страница {page + 1} из {pages}")
    if not items:
        head.append("")
        head.append("Пусто. Добавь первого поставщика или загрузи список пачкой.")

    rows_ = [[btn(prov_label(r), f"fin:prov:{idx}")] for idx, r in chunk]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"fin:provp:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"fin:provp:{page + 1}"))
    if nav:
        rows_.append(nav)
    rows_.append([btn("🔎 Поиск", "fin:provfind"),
                  btn("🧹 Сбросить" if query else "➕ Добавить",
                      "fin:provreset" if query else "fin:provadd")])
    if query:
        rows_.append([btn("➕ Добавить", "fin:provadd")])
    rows_.append([btn("📥 Загрузить список пачкой", "fin:provimp")])
    await core.take_over(c, "\n".join(head), kb(rows_))


async def cb_prov_list(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    core.nav_push(c.from_user.id, "fin:prov")
    await _prov_list_screen(c, 0)
    await c.answer()


async def cb_prov_page(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    try:
        page = int(c.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await _prov_list_screen(c, page)
    await c.answer()


async def cb_prov_reset(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    _prov_query.pop(c.from_user.id, None)
    await _prov_list_screen(c, 0)
    await c.answer("Поиск сброшен")


async def _prov_card_screen(c: CallbackQuery, row: int, note: str = None) -> bool:
    found = None
    for idx, r in proveedores_indexed():
        if idx == row:
            found = r
            break
    if not found:
        return False
    core.nav_push(c.from_user.id, f"fin:prov:{row}")
    rows_ = [
        [btn("✏️ IBAN", f"fin:provf:{row}:iban"), btn("✏️ NIF", f"fin:provf:{row}:nif")],
        [btn("✏️ Название", f"fin:provf:{row}:name"),
         btn("✏️ Алиасы", f"fin:provf:{row}:alias")],
        [btn("✏️ Локали", f"fin:provf:{row}:loc"),
         btn("✏️ Комментарий", f"fin:provf:{row}:note")],
        [btn("📇 К списку", "fin:prov")],
    ]
    await core.take_over(c, prov_card(found), kb(rows_))
    await c.answer(note or "")
    return True


async def cb_prov_card(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    try:
        row = int(c.data.split(":")[2])
    except (IndexError, ValueError):
        await c.answer()
        return
    if not await _prov_card_screen(c, row):
        await c.answer("Поставщик не найден", show_alert=True)


async def cb_prov_field(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    parts = c.data.split(":")
    try:
        row, field = int(parts[2]), parts[3]
    except (IndexError, ValueError):
        await c.answer()
        return
    if field not in PROV_FIELDS:
        await c.answer()
        return
    _await_prov_field[c.from_user.id] = (row, field, time())
    header, hint = PROV_FIELDS[field]
    await core.take_over(c, f"✏️ <b>{e(header)}</b>\n\n{hint}",
                         kb([[btn("❌ Отмена", f"fin:prov:{row}")]], back=False))
    await c.answer()


async def cb_prov_add(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    _await_prov_new[c.from_user.id] = time()
    await core.take_over(
        c,
        "➕ <b>Новый поставщик</b>\n\nПришли одним сообщением, по строке на пункт:\n\n"
        "<code>Название\nIBAN\nNIF (необязательно)\nалиасы через запятую (необязательно)</code>\n\n"
        "Например:\n<code>ACEM CAFE, S.L.\nES2221003464812200104761\nB10467371\n"
        "acem, don gallo</code>",
        kb([[btn("❌ Отмена", "fin:prov")]], back=False))
    await c.answer()


async def cb_prov_find(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    _await_prov_find[c.from_user.id] = time()
    await core.take_over(c, "🔎 Пришли кусок названия, NIF или IBAN — найду.",
                         kb([[btn("❌ Отмена", "fin:prov")]], back=False))
    await c.answer()


IMPORT_HELP = (
    "📥 <b>Загрузка списка пачкой</b>\n\n"
    "Пришли список — текстом в сообщении или файлом <code>.csv</code> / <code>.txt</code>.\n"
    "Одна строка на поставщика, поля через <code>|</code>, <code>;</code> или табуляцию:\n\n"
    "<code>Название | IBAN | NIF | алиасы | BIC</code>\n\n"
    "Обязательны только название и IBAN. BIC нужен для иностранных счетов — "
    "по испанскому IBAN бот подставит его сам. Пример:\n"
    "<code>ACEM CAFE, S.L. | ES2221003464812200104761 | B10467371 | acem, don gallo\n"
    "VORAVINS SL | ES9121000418450200051332 | B98765432 |</code>\n\n"
    "Если поставщик с таким названием уже есть — обновлю ему IBAN и NIF, "
    "дубликат не создам. Строки с непонятным IBAN пропущу и покажу списком."
)


async def cb_prov_import(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    _await_prov_import[c.from_user.id] = time()
    await core.take_over(c, IMPORT_HELP, kb([[btn("❌ Отмена", "fin:prov")]], back=False))
    await c.answer()


def split_import_line(line: str) -> list:
    for sep in ("|", "\t", ";"):
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    return [line.strip()]


def parse_import(text: str) -> tuple:
    """-> (записи, ошибки). Шапку таблицы пропускаем сами."""
    records, errors = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = split_import_line(line)
        if len(parts) < 2:
            errors.append(f"{line[:40]} — нет IBAN")
            continue
        name = parts[0].strip()
        iban = iban_clean(parts[1])
        nif = parts[2].strip() if len(parts) > 2 else ""
        aliases = parts[3].strip() if len(parts) > 3 else ""
        # пятое поле — BIC. Нужен для иностранных счетов и банков, которых нет
        # в справочнике кодов: там по IBAN его не вывести.
        bic = parts[4].strip().upper() if len(parts) > 4 else ""
        if not name:
            errors.append(f"{line[:40]} — нет названия")
            continue
        if norm(name) in ("поставщик", "nombre", "proveedor", "название"):
            continue          # шапка таблицы
        if not iban_valid(iban):
            errors.append(f"{name[:30]} — IBAN не проходит проверку")
            continue
        records.append({"name": name, "iban": iban, "nif": nif,
                        "aliases": aliases, "bic": bic})
    return records, errors


async def apply_import(records: list, author: str) -> tuple:
    """Новых добавляем пачкой, существующим обновляем реквизиты. -> (добавлено, обновлено)"""
    existing = {norm(r.get("Поставщик")): (idx, r) for idx, r in proveedores_indexed(force=True)}
    fresh, updated = [], 0
    for rec in records:
        hit = existing.get(norm(rec["name"]))
        if not hit:
            fresh.append(rec)
            continue
        idx, old = hit
        changes = {}
        if iban_clean(old.get("IBAN")) != rec["iban"]:
            changes["IBAN"] = rec["iban"]
            changes["BIC"] = rec.get("bic") or bic_by_iban(rec["iban"])
        elif rec.get("bic") and str(old.get("BIC", "")).strip() != rec["bic"]:
            changes["BIC"] = rec["bic"]
        if rec["nif"] and str(old.get("NIF", "")).strip() != rec["nif"]:
            changes["NIF"] = rec["nif"]
        if changes:
            changes["Дата"] = core.now_local().strftime("%d.%m.%Y %H:%M")
            changes["Кто добавил"] = author
            set_fields(PROVEEDORES_WS, idx, changes)
            updated += 1
    added = add_proveedores_bulk(fresh, author) if fresh else 0
    return added, updated


async def on_prov_text(m: Message):
    """Один обработчик на все текстовые ответы внутри справочника."""
    uid = m.from_user.id

    # 1) поиск
    if _fresh(_await_prov_find.get(uid)):
        _await_prov_find.pop(uid, None)
        _prov_query[uid] = (m.text or "").strip()[:40]
        found = len([1 for _, r in proveedores_indexed()
                     if norm(_prov_query[uid]) in norm(r.get("Поставщик"))
                     or norm(_prov_query[uid]) in norm(r.get("Алиасы"))])
        await m.answer(f"🔎 Нашла: <b>{found}</b>. Открой справочник — список уже отфильтрован.",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [btn("📇 Справочник", "fin:prov")]]))
        return

    # 2) правка одного поля
    state = _await_prov_field.get(uid)
    if state and _fresh(state[2]):
        row, field, _ = state
        header, _hint = PROV_FIELDS[field]
        value = (m.text or "").strip()
        changes = {}

        if field == "iban":
            value = iban_clean(value)
            if not iban_valid(value):
                await m.answer("IBAN не проходит проверку контрольного числа. "
                               "Проверь и пришли снова.")
                return
            changes = {"IBAN": value, "BIC": bic_by_iban(value)}
        elif field == "name":
            if not value:
                await m.answer("Название не может быть пустым.")
                return
            other, _r = find_proveedor_row(value)
            if other and other != row:
                await m.answer("Поставщик с таким названием уже есть в справочнике.")
                return
            changes = {"Поставщик": value}
        else:
            changes = {header: value}

        changes["Дата"] = core.now_local().strftime("%d.%m.%Y %H:%M")
        changes["Кто добавил"] = m.from_user.full_name
        _await_prov_field.pop(uid, None)
        try:
            set_fields(PROVEEDORES_WS, row, changes)
        except Exception as ex:
            log.exception("не смог обновить поставщика: %s", ex)
            await m.answer("Не смог записать в таблицу, попробуй ещё раз.")
            return
        found = None
        for idx, r in proveedores_indexed(force=True):
            if idx == row:
                found = r
                break
        await m.answer((prov_card(found) if found else "Сохранено ✅"),
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [btn("📇 К списку", "fin:prov")]]))
        return

    # 3) новый поставщик
    if _fresh(_await_prov_new.get(uid)):
        parts = [p.strip() for p in (m.text or "").splitlines() if p.strip()]
        if len(parts) < 2:
            await m.answer("Нужно минимум две строки: название и IBAN.")
            return
        name, iban = parts[0], iban_clean(parts[1])
        nif = parts[2] if len(parts) > 2 else ""
        aliases = parts[3] if len(parts) > 3 else ""
        if not iban_valid(iban):
            await m.answer("IBAN не проходит проверку контрольного числа. "
                           "Проверь и пришли снова.")
            return
        other, _r = find_proveedor_row(name)
        if other:
            await m.answer("Такой поставщик уже есть — открой его в справочнике и "
                           "поправь реквизиты там.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                               [btn("Открыть", f"fin:prov:{other}")]]))
            return
        _await_prov_new.pop(uid, None)
        try:
            add_proveedor(name, iban, nif, m.from_user.full_name, aliases=aliases)
        except Exception as ex:
            log.exception("не смог добавить поставщика: %s", ex)
            await m.answer("Не смог записать в таблицу, попробуй ещё раз.")
            return
        await m.answer(f"✅ Добавлен: <b>{e(name)}</b>\nBIC: {e(bic_by_iban(iban)) or '—'}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [btn("📇 Справочник", "fin:prov")]]))
        return

    # 4) импорт списком, присланный текстом
    if _fresh(_await_prov_import.get(uid)):
        await do_import(m, m.text or "")
        return


async def on_prov_file(m: Message):
    """Импорт списка файлом .csv / .txt."""
    uid = m.from_user.id
    if not _fresh(_await_prov_import.get(uid)):
        _await_prov_import.pop(uid, None)
        return
    doc = m.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".csv", ".txt")):
        await m.answer("Жду файл .csv или .txt — или просто пришли список текстом.")
        return
    if (doc.file_size or 0) > 2 * 1024 * 1024:
        await m.answer("Файл слишком большой. Раздели на части или пришли текстом.")
        return
    try:
        buf = await core.bot.download(doc)
        raw = buf.read()
    except Exception as ex:
        log.warning("не смог скачать файл импорта: %s", ex)
        await m.answer("Не смогла скачать файл, пришли список текстом.")
        return
    text = ""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    await do_import(m, text)


async def do_import(m: Message, text: str):
    uid = m.from_user.id
    records, errors = parse_import(text or "")
    if not records and not errors:
        await m.answer("Ничего не разобрала. Проверь формат: "
                       "<code>Название | IBAN | NIF | алиасы</code>")
        return
    _await_prov_import.pop(uid, None)
    added = updated = 0
    if records:
        try:
            added, updated = await apply_import(records, m.from_user.full_name)
        except Exception as ex:
            log.exception("импорт справочника упал: %s", ex)
            await m.answer("Таблица не приняла запись, попробуй ещё раз.")
            return

    lines = ["📥 <b>Импорт справочника</b>", "",
             f"✅ Добавлено: <b>{added}</b>",
             f"🔄 Обновлено: <b>{updated}</b>"]
    if errors:
        lines.append(f"⚠️ Пропущено: <b>{len(errors)}</b>")
        lines.append("")
        for err in errors[:10]:
            lines.append(f"• {e(err)}")
        if len(errors) > 10:
            lines.append(f"…и ещё {len(errors) - 10}")
        lines.append("")
        lines.append("Пропущенные строки поправь и пришли ещё раз — дубликатов не будет.")
    await m.answer("\n".join(lines)[:4000],
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [btn("📇 Справочник", "fin:prov")]]))


# ---------------- СБОРКА РЕМЕСЫ (список + пересчёт; выгрузка — после ответов) ----------------

async def cb_remesa(c: CallbackQuery):
    u = core.guard(c)
    if not u or not is_findir(u, c.from_user.id):
        await deny(c)
        return
    loc = c.data.split(":")[2]
    if loc not in core.LOCALES:
        await c.answer()
        return
    core.nav_push(c.from_user.id, c.data)

    items = [(idx, r) for idx, r in
             facturas(loc, (ST_NEW, ST_RECOGNIZED, ST_NEED_IBAN, ST_READY))]
    ready = [(idx, r) for idx, r in items if str(r.get("Статус")).strip() == ST_READY]
    not_ready = [r for _, r in items if str(r.get("Статус")).strip() != ST_READY]
    total = sum(parse_amount(r.get("Total")) for _, r in ready)

    lines = [f"▶️ <b>Ремеса</b> · {loc_label(loc)}", ""]
    if ready:
        lines.append(f"В ремесу пойдут <b>{len(ready)}</b> переводов на <b>{money(total)}</b>:")
        for _, r in ready[:LIST_LIMIT]:
            lines.append(f"• {e(str(r.get('Поставщик'))[:28])} — {money(r.get('Total'))} · "
                         f"{e(mask_iban(r.get('IBAN')))}")
    else:
        lines.append("Готовых к ремесе документов нет — заполни данные в «Неоплаченных».")
    if not_ready:
        lines.append("")
        lines.append(f"⚠️ Не готовы ({len(not_ready)}): нет суммы, нет IBAN "
                     f"или ждут подтверждения.")
    lines.append("")
    lines.append("Выгрузка XML + 2 PDF включится, как только будут ответы по счёту списания, "
                 "концепту платежа и дате исполнения.")

    rows_ = [[btn(f"🚫 {str(r.get('Поставщик'))[:20]} · {money(r.get('Total'))}",
                  f"fin:exc:{r.get('ID')}")] for _, r in ready[:LIST_LIMIT]]
    await core.take_over(c, "\n".join(lines)[:4000], kb(rows_))
    await c.answer()


# ---------------- РЕГИСТРАЦИЯ ----------------

CALLBACKS = [
    ("d:fin", cb_root, True),          # True = точное совпадение
    ("fin:up", cb_upload, True),
    ("fin:cancel", cb_cancel, True),
    # справочник поставщиков — точные совпадения раньше префиксов
    ("fin:provadd", cb_prov_add, True),
    ("fin:provimp", cb_prov_import, True),
    ("fin:provfind", cb_prov_find, True),
    ("fin:provreset", cb_prov_reset, True),
    ("fin:provf:", cb_prov_field, False),
    ("fin:provp:", cb_prov_page, False),
    ("fin:prov:", cb_prov_card, False),
    ("fin:prov", cb_prov_list, True),
    ("fin:loc:", cb_pick_loc, False),
    ("fin:just:", cb_just, False),
    ("fin:l:", cb_loc_menu, False),
    ("fin:unp:", cb_unpaid, False),
    ("fin:pay:", cb_paid, False),
    ("fin:arch:", cb_archive, False),
    ("fin:rem:", cb_remesa, False),
    ("fin:doc:", cb_card, False),
    ("fin:ok:", cb_confirm, False),
    ("fin:show:", cb_show, False),
    ("fin:jst:", cb_show, False),
    ("fin:delok:", cb_delete_do, False),
    ("fin:del:", cb_delete_ask, False),
    ("fin:paidonly:", cb_paid_only, False),
    ("fin:paid:", cb_paid_ask, False),
    ("fin:web", cb_web_link, True),
    ("fin:fill:", cb_fill, False),
    ("fin:exc:", cb_exclude, False),
    ("fin:inc:", cb_include, False),
]

# Порядок важен: длинные префиксы раньше коротких.
NAV_ROUTES = [
    ("fin:provp:", cb_prov_page),
    ("fin:prov:", cb_prov_card),
    ("fin:prov", cb_prov_list),
    ("fin:doc:", cb_card),
    ("fin:unp:", cb_unpaid),
    ("fin:pay:", cb_paid),
    ("fin:arch:", cb_archive),
    ("fin:rem:", cb_remesa),
    ("fin:l:", cb_loc_menu),
    ("d:fin", cb_root),
]


def ensure_routes():
    """Кнопка «Назад» в основном коде ходит по таблице ROUTES — дописываем туда
    свои экраны. ROUTES объявлена ниже по файлу, поэтому делаем это лениво."""
    global _routes_done
    if _routes_done:
        return
    table = getattr(core, "ROUTES", None)
    if table is None:
        return
    for prefix, fn in reversed(NAV_ROUTES):
        table.insert(0, (prefix, _wrap(fn)))
    _routes_done = True
    log.info("fin_block: маршруты «назад» подключены")


def _wrap(fn):
    """Общая обёртка: ленивое подключение маршрутов + отлов ошибок, чтобы падение
    финблока не уходило в глобальный обработчик и не пугало патронов."""
    async def inner(c: CallbackQuery):
        ensure_routes()
        try:
            await fn(c)
        except Exception as ex:
            log.exception("fin_block %s: %s", getattr(fn, "__name__", "?"), ex)
            try:
                await c.answer("Ошибка в финблоке, я записал её в лог", show_alert=True)
            except Exception:
                pass
    inner.__name__ = getattr(fn, "__name__", "fin_handler")
    return inner


def install_quota_guard():
    """Защита от лимита Google Sheets (60 чтений в минуту на пользователя).

    Модулей у бота стало много, и на старте они читают таблицы почти
    одновременно: шапки всех листов, архив сотрудников, заявки, справочники.
    Google отвечает 429, gspread бросает APIError, бот падает — и Railway
    поднимает его заново, снова сжигая квоту.

    Оборачиваем запросы gspread: на 429 и временные ошибки сервера ждём и
    повторяем. Это чинит и соседние модули, менять их файлы не нужно.
    """
    try:
        from gspread.http_client import HTTPClient
    except Exception as ex:
        log.warning("не смог поставить защиту от лимита Google: %s", ex)
        return
    orig = getattr(HTTPClient, "request", None)
    if orig is None or getattr(orig, "_quota_guard", False):
        return

    import time as _time

    RETRY_AFTER = (3, 8, 20)

    def guarded(self, *args, **kwargs):
        last = None
        for i, pause in enumerate((0,) + RETRY_AFTER):
            if pause:
                _time.sleep(pause)
            try:
                return orig(self, *args, **kwargs)
            except Exception as ex:
                text = str(ex)
                transient = ("[429]" in text or "Quota exceeded" in text
                             or "[500]" in text or "[503]" in text
                             or "rateLimitExceeded" in text)
                if not transient or i == len(RETRY_AFTER):
                    raise
                last = ex
                log.warning("Google ответил лимитом, жду %d сек и повторяю", RETRY_AFTER[i])
        if last:
            raise last

    guarded._quota_guard = True
    HTTPClient.request = guarded
    log.info("защита от лимита Google Sheets установлена")


def setup(dp, core_module):
    """Вызывается из bot555.py сразу после создания Dispatcher.

    Хендлеры регистрируются ПЕРВЫМИ, поэтому колбэк d:fin и документы для оплаты
    забирает финблок, а всё остальное как работало, так и работает: фильтры
    требуют либо префикс fin:, либо наше состояние ожидания.
    """
    global core, _dp
    core = core_module
    _dp = dp

    # ставим первым делом: дальше стартуют и читают таблицы все остальные модули
    install_quota_guard()

    # свои листы — чтобы ensure_headers() на старте создал их с правильной шапкой
    try:
        core.HEADERS.update(FIN_HEADERS)
    except Exception as ex:
        log.warning("не смог зарегистрировать шапки листов: %s", ex)

    # веб-страница статуса платежей живёт в отдельном файле и делит
    # веб-сервер с архивом сотрудников — порт у Railway один
    try:
        import fin_web
        fin_web.setup(core_module)
    except Exception as ex:
        log.warning("fin_web не подключён: %s", ex)

    for prefix, fn, exact in CALLBACKS:
        flt = (F.data == prefix) if exact else F.data.startswith(prefix)
        dp.callback_query.register(_wrap(fn), flt)

    dp.message.register(
        on_doc,
        F.chat.type == "private",
        F.photo | F.document,
        lambda m: m.from_user and m.from_user.id in _await_doc,
    )
    dp.message.register(
        on_fill_text,
        F.chat.type == "private",
        F.text,
        lambda m: m.from_user and m.from_user.id in _await_fill
        and not (m.text or "").startswith("/"),
    )
    # справочник поставщиков: файл со списком и любые текстовые ответы
    dp.message.register(
        on_prov_file,
        F.chat.type == "private",
        F.document,
        lambda m: m.from_user and m.from_user.id in _await_prov_import,
    )
    # хустификанте из банка — фото или файл после нажатия «Оплачено»
    dp.message.register(
        on_justificante,
        F.chat.type == "private",
        F.photo | F.document,
        lambda m: m.from_user and m.from_user.id in _await_just,
    )
    dp.message.register(
        on_prov_text,
        F.chat.type == "private",
        F.text,
        lambda m: m.from_user and (
            m.from_user.id in _await_prov_field or m.from_user.id in _await_prov_new
            or m.from_user.id in _await_prov_import or m.from_user.id in _await_prov_find
        ) and not (m.text or "").startswith("/"),
    )
    log.info("fin_block подключён: %d колбэков, 4 обработчика сообщений", len(CALLBACKS))
