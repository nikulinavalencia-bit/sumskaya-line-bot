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
import html
import logging
from time import time

from aiogram import F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("fin")

core = None          # модуль bot555 целиком — подставляется в setup()
_routes_done = False

# ---------------- НАСТРОЙКИ (открытые вопросы ТЗ — меняются одной строкой) ----------------

# 10.1 — видит ли патрон финблок целиком наравне с фин. директором
PATRON_FULL_ACCESS = True

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
ST_NEED_IBAN = "нужен IBAN"
ST_READY = "готов к ремесе"
ST_EXCLUDED = "исключён"
ST_IN_REMESA = "в ремесе"
ST_SENT = "отправлено в банк"
ST_PAID = "оплачено"

UNPAID = (ST_NEW, ST_NEED_IBAN, ST_READY, ST_EXCLUDED)
PAID_QUEUE = (ST_IN_REMESA, ST_SENT)

ST_EMOJI = {
    ST_NEW: "🆕",
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


def _fresh(ts) -> bool:
    return bool(ts) and (time() - ts) < STATE_TTL


def _clear(uid: int):
    _await_doc.pop(uid, None)
    _pending.pop(uid, None)
    _await_fill.pop(uid, None)


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


def set_fields(row_idx: int, values: dict):
    """Точечно обновляет ячейки строки — одним batch-запросом, чтобы не жечь
    лимит Google API. Чужие строки, шапку и форматирование не трогает."""
    cols = col_index(FACTURAS_WS)
    w = core.ws(FACTURAS_WS)
    data = []
    for header, val in values.items():
        col = cols.get(header)
        if not col:
            log.warning("нет колонки %s в %s", header, FACTURAS_WS)
            continue
        data.append({"range": _a1(row_idx, col), "values": [[val]]})
    if not data:
        return
    core.sheets_write_retry(w.batch_update, data, value_input_option="RAW")
    core.drop_cache(FACTURAS_WS)


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


def add_proveedor(name: str, iban: str, nif: str, author: str):
    w = core.ws(PROVEEDORES_WS)
    order = FIN_HEADERS[PROVEEDORES_WS]
    rec = {"Поставщик": name, "NIF": nif, "IBAN": iban, "BIC": bic_by_iban(iban),
           "Страна": "ES", "Кто добавил": author,
           "Дата": core.now_local().strftime("%d.%m.%Y %H:%M")}
    core.sheets_write_retry(w.append_row, [rec.get(h, "") for h in order],
                            value_input_option="RAW")
    core.drop_cache(PROVEEDORES_WS)


# IBAN -> BIC по коду банка (позиции 5–8 испанского IBAN)
BIC_BY_BANK = {
    "0049": "BSCHESMMXXX",   # Santander
    "0075": "POPUESMMXXX",   # Banco Popular / Santander
    "0081": "BSABESBBXXX",   # Sabadell
    "0128": "BKBKESMMXXX",   # Bankinter
    "0182": "BBVAESMMXXX",   # BBVA
    "0234": "CAHMESMMXXX",   # Banco Caminos
    "1465": "INGDESMMXXX",   # ING
    "1491": "TRIOESMMXXX",   # Triodos
    "2038": "CAHMESMMXXX",   # Bankia (ист.)
    "2080": "CAGLESMMXXX",   # Abanca
    "2100": "CAIXESBBXXX",   # CaixaBank
    "3058": "CCRIES2AXXX",   # Cajamar
    "3159": "BCOEESMM159",   # Caja Rural
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
        await core.take_over(
            c, f"{head}\n\nЗагрузи документ на оплату — я передам его финансовому отделу.",
            kb([[btn("💳 Оплата — загрузить документ", "fin:up")]]))
        return

    rows_ = [[btn(f"{loc_label(code)}", f"fin:l:{code}")] for code in core.LOCALES]
    rows_.append([btn("💳 Оплата — загрузить документ", "fin:up")])
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
    elif m.document:
        file_id, ftype = m.document.file_id, "document"
        fname = m.document.file_name or "файл"
    else:
        return

    _pending[uid] = {
        "ts": time(), "file_id": file_id, "ftype": ftype, "fname": fname,
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

    await core.take_over(
        c,
        f"✅ Документ принят\n\n{loc_label(p['loc'])}\n"
        f"Хустификанте: <b>{'нужен' if need else 'не нужен'}</b>\n"
        f"Номер: <code>{fid}</code>",
        kb([[btn("💳 Загрузить ещё", "fin:up")]]))
    await c.answer()
    await notify_findirs(rec)


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
    if st == ST_EXCLUDED:
        rows_.append([btn("↩️ Вернуть в очередь", f"fin:inc:{fid}")])
    elif st in (ST_NEW, ST_NEED_IBAN, ST_READY):
        rows_.append([btn("🚫 Исключить из ремесы", f"fin:exc:{fid}")])
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
    file_id = str(r.get("FileID", "")).strip()
    if not file_id:
        await c.answer("Файл не сохранён", show_alert=True)
        return
    cap = f"{e(r.get('Поставщик') or '')} · {money(r.get('Total'))}"
    try:
        if str(r.get("Тип файла")).strip() == "photo":
            await core.bot.send_photo(c.message.chat.id, file_id, caption=cap)
        else:
            await core.bot.send_document(c.message.chat.id, file_id, caption=cap)
        await c.answer()
    except Exception as ex:
        log.warning("не смог отправить файл %s: %s", fid, ex)
        await c.answer("Файл недоступен", show_alert=True)


FILL_HELP = (
    "✏️ Пришли данные одним сообщением, по строке на пункт:\n\n"
    "<code>Поставщик\n"
    "Номер фактуры\n"
    "Дата фактуры (дд.мм.гггг)\n"
    "Сумма к оплате\n"
    "IBAN (если поставщика ещё нет в справочнике)</code>\n\n"
    "Пятая строка нужна только для нового поставщика — для известного IBAN подставлю сам."
)


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
    uid = m.from_user.id
    state = _await_fill.get(uid)
    if not state or not _fresh(state[1]):
        _await_fill.pop(uid, None)
        return
    fid = state[0]
    parts = [p.strip() for p in (m.text or "").splitlines() if p.strip()]
    if len(parts) < 4:
        await m.answer("Нужно минимум 4 строки: поставщик, номер, дата, сумма. Попробуй ещё раз.")
        return

    idx, r = find_factura(fid)
    if not r:
        _await_fill.pop(uid, None)
        await m.answer("Документ не найден — видимо, строку удалили из таблицы.")
        return

    name, numero, fecha, total_raw = parts[0], parts[1], parts[2], parts[3]
    iban_in = iban_clean(parts[4]) if len(parts) > 4 else ""
    total = parse_amount(total_raw)
    if total <= 0:
        await m.answer("Сумму не понял. Напиши, например, <code>1234,56</code>.")
        return
    if not core.parse_ddmmyyyy(fecha):
        await m.answer("Дата не распознана, формат дд.мм.гггг. Попробуй ещё раз.")
        return
    if iban_in and not iban_valid(iban_in):
        await m.answer("IBAN не проходит проверку контрольного числа. Проверь и пришли снова.")
        return

    prov = find_proveedor(name)
    prov_iban = iban_clean(prov.get("IBAN")) if prov else ""
    iban = iban_in or prov_iban
    nif = str((prov or {}).get("NIF", "") or "")
    warn = ""
    if iban_in and prov_iban and iban_in != prov_iban:
        warn = ("\n\n🔴 <b>Внимание:</b> присланный IBAN не совпадает с тем, что в справочнике "
                f"({mask_iban(prov_iban)}). В таблицу записал присланный — проверь реквизиты "
                "с поставщиком, это классическая схема подмены счёта.")

    status = ST_READY if iban_valid(iban) else ST_NEED_IBAN
    _await_fill.pop(uid, None)

    try:
        set_fields(idx, {
            "Поставщик": name, "Номер": numero, "Дата фактуры": fecha,
            "Total": f"{total:.2f}", "IBAN": iban, "NIF": nif, "Статус": status,
        })
    except Exception as ex:
        log.exception("не смог обновить фактуру %s: %s", fid, ex)
        await m.answer("Не смог записать в таблицу, попробуй ещё раз.")
        return

    if iban_in and not prov:
        try:
            add_proveedor(name, iban_in, nif, m.from_user.full_name)
            warn += "\n\n➕ Поставщик добавлен в справочник."
        except Exception as ex:
            log.warning("поставщик не добавлен: %s", ex)

    _, r2 = find_factura(fid)
    await m.answer(card_text(r2 or r) + warn,
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
    set_fields(idx, {"Статус": status})
    await _render_card(c, fid, note)


async def cb_exclude(c: CallbackQuery):
    await _set_status(c, ST_EXCLUDED, "Исключено из ремесы")


async def cb_include(c: CallbackQuery):
    await _set_status(c, ST_READY, "Вернул в очередь")


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

    items = [(idx, r) for idx, r in facturas(loc, (ST_NEW, ST_NEED_IBAN, ST_READY))]
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
        lines.append(f"⚠️ Не готовы ({len(not_ready)}): нет суммы или IBAN.")
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
    ("fin:loc:", cb_pick_loc, False),
    ("fin:just:", cb_just, False),
    ("fin:l:", cb_loc_menu, False),
    ("fin:unp:", cb_unpaid, False),
    ("fin:pay:", cb_paid, False),
    ("fin:arch:", cb_archive, False),
    ("fin:rem:", cb_remesa, False),
    ("fin:doc:", cb_card, False),
    ("fin:show:", cb_show, False),
    ("fin:fill:", cb_fill, False),
    ("fin:exc:", cb_exclude, False),
    ("fin:inc:", cb_include, False),
]

# Порядок важен: длинные префиксы раньше коротких.
NAV_ROUTES = [
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


def setup(dp, core_module):
    """Вызывается из bot555.py сразу после создания Dispatcher.

    Хендлеры регистрируются ПЕРВЫМИ, поэтому колбэк d:fin и документы для оплаты
    забирает финблок, а всё остальное как работало, так и работает: фильтры
    требуют либо префикс fin:, либо наше состояние ожидания.
    """
    global core
    core = core_module

    # свои листы — чтобы ensure_headers() на старте создал их с правильной шапкой
    try:
        core.HEADERS.update(FIN_HEADERS)
    except Exception as ex:
        log.warning("не смог зарегистрировать шапки листов: %s", ex)

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
    log.info("fin_block подключён: %d колбэков, 2 обработчика сообщений", len(CALLBACKS))
