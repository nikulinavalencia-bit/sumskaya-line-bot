# =========================================================
#  solicitudes.py — заявки управляющих: Alta / Baja / Médico / Cambio
#
#  Управляющий открывает архив (кнопка в HR или /archivo), вкладка
#  «📝 Заявки», выбирает одну из четырёх кнопок и заполняет форму.
#  Заявка падает в Google-таблицу бота (листы SOL_Alta, SOL_Baja,
#  SOL_Medico, SOL_Cambio) и сразу уходит Патронам в личку.
#
#  Для Alta бот тут же просит у автора фото NIE/TIE в Telegram и
#  подшивает file_id к строке заявки — без доступа к Google Drive.
#
#  Досье сотрудника (кнопка «📁 Документы» в карточке архива):
#  контракт с Диска, письма Histora (CONTRATO/BAJA/CAMBIO), данные из
#  анкеты Solicitud и все его заявки Alta/Baja/Médico/Cambio.
#
#  Подключение в bot.py (после hr_web):
#      try:
#          import sys as _sys
#          import solicitudes
#          solicitudes.setup(dp, _sys.modules[__name__])
#      except Exception as _e:
#          log.error("solicitudes не подключён: %s", _e, exc_info=True)
# =========================================================

import re
import json
import asyncio
import logging
import unicodedata

log = logging.getLogger("sumskaya.solicitudes")

VERSION = "solicitudes 1.0 · 23.09.2026"

M = None
_awaiting_photo = {}      # uid -> (лист, номер строки, ФИО)

# Локали формы Solicitud (как в Google-форме) → тег Registro.
LOCALES = [
    ("P&S Reina", "REINA"),
    ("P&S Francia", "FRANCIA"),
    ("P&S Bakery", "BAKERY"),
    ("Boi Boi Gran Via", "BOI BOI"),
    ("Boi Boi Calle del Mar", "BOI BOI"),
    ("Oficina", "OFFICE"),
]
PUESTOS = [
    "Ayudante camarero", "Camarero", "Jefe del restaurante", "Encargado", "Barman",
    "Ayudante cocinero", "Cocinero", "Jefe de cocina", "Personal de Limpieza",
    "Conductor-repartidor", "Ayudante Pastelero", "Ayudante Panadero", "Panadero",
    "Pastelero", "otros",
]
HORAS = ["10", "20", "30", "40", "otros"]
PERIODOS = ["Período de prueba", "Después"]
MOTIVOS = ["Baja voluntaria", "Despido", "Fin de contrato"]

SHEETS = {
    "alta": ("SOL_Alta", [
        "Дата заявки", "Автор", "Local", "Nombre", "Apellido", "Fecha de nacimiento",
        "NIE/TIE", "Domicilio", "Código postal", "IBAN", "Correo electrónico",
        "Teléfono", "Numero seguridad social", "Título profesional",
        "Número de horas", "Fecha de inicio", "Фото NIE", "Статус", "Комментарий"]),
    "baja": ("SOL_Baja", [
        "Дата заявки", "Автор", "Nombre y Apellidos", "Puesto", "Persona responsable",
        "local", "Período de despido", "Vacaciones días", "Motivo del despido",
        "Fecha de Baja", "Mes", "Año", "Статус", "Комментарий"]),
    "medico": ("SOL_Medico", [
        "Дата заявки", "Автор", "Nombre y Apellidos", "Fecha de nacimiento", "NIE/TIE",
        "Domicilio/Código postal", "IBAN", "Correo electrónico", "Teléfono",
        "Numero seguridad social", "local", "Título profesional", "Статус", "Комментарий"]),
    "cambio": ("SOL_Cambio", [
        "Дата заявки", "Автор", "Nombre y Apellidos", "Persona responsable",
        "local Antes", "local Después", "Horas Antes", "Horas Después",
        "Puesto Antes", "Puesto Después", "Fecha de Alta", "Mes", "Año",
        "Статус", "Комментарий"]),
}
TITLES = {"alta": "🟢 Alta — новый сотрудник", "baja": "🔴 Baja — увольнение",
          "medico": "🏥 Seguro médico", "cambio": "🔁 Cambio — изменение контракта"}

# Какие поля формы в какую колонку листа (порядок = порядок колонок).
FIELDS = {
    "alta": ["local", "nombre", "apellido", "nacimiento", "nie", "domicilio", "cp",
             "iban", "email", "telefono", "nss", "puesto", "horas", "inicio"],
    "baja": ["nombre", "puesto", "responsable", "local", "periodo", "vacaciones",
             "motivo", "fecha", "mes", "anio"],
    "medico": ["nombre", "nacimiento", "nie", "domicilio", "iban", "email",
               "telefono", "nss", "local", "puesto"],
    "cambio": ["nombre", "responsable", "local_antes", "local_despues", "horas_antes",
               "horas_despues", "puesto_antes", "puesto_despues", "fecha", "mes", "anio"],
}
REQUIRED = {
    "alta": ["local", "nombre", "apellido", "nie", "domicilio", "iban", "email",
             "telefono", "nss", "puesto", "horas", "inicio"],
    "baja": ["nombre", "local", "periodo", "motivo", "fecha"],
    "medico": ["nombre", "nie", "local"],
    "cambio": ["nombre", "local_antes", "fecha"],
}


# ---------------- ТАБЛИЦА ----------------

def _ws(kind: str):
    """Лист заявок; создаётся сам, шапка дописывается при необходимости."""
    name, headers = SHEETS[kind]
    try:
        w = M._sh.worksheet(name)
    except Exception:
        w = M._sh.add_worksheet(title=name, rows=2000, cols=len(headers) + 2)
        w.update(range_name="A1", values=[headers])
        M._all_sheets_cache["ts"] = 0
        return w
    if not w.row_values(1):
        w.update(range_name="A1", values=[headers])
    return w


def _append(kind: str, values: list) -> int:
    w = _ws(kind)
    M.sheets_write_retry(w.append_row, values, value_input_option="USER_ENTERED")
    return len(w.col_values(1))


def _rows(kind: str) -> list:
    try:
        return _ws(kind).get_all_records()
    except Exception as e:
        log.warning("solicitudes: лист %s не прочитан: %s", SHEETS[kind][0], e)
        return []


def _norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def _split_date(s: str):
    """'25.09.2026' → ('25', '9', '2026'); пустое — три пустые строки."""
    d = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime
            d = datetime.strptime(str(s or "").strip(), fmt).date()
            break
        except ValueError:
            continue
    return (str(d.day), str(d.month), str(d.year)) if d else ("", "", "")


def save(kind: str, data: dict, author: str) -> tuple:
    """Пишет заявку в лист. Возвращает (номер строки, ФИО)."""
    now = M.now_local().strftime("%d.%m.%Y %H:%M")
    g = lambda k: str(data.get(k, "")).strip()
    if kind == "alta":
        fio = f"{g('nombre')} {g('apellido')}".strip()
        row = [now, author, g("local"), g("nombre"), g("apellido"), g("nacimiento"),
               g("nie"), g("domicilio"), g("cp"), g("iban"), g("email"), g("telefono"),
               g("nss"), g("puesto"), g("horas"), g("inicio"), "", "новая", g("comentario")]
    elif kind == "baja":
        fio = g("nombre")
        d, mo, y = _split_date(g("fecha"))
        row = [now, author, fio, g("puesto"), g("responsable"), g("local"), g("periodo"),
               g("vacaciones"), g("motivo"), d, mo, y, "новая", g("comentario")]
    elif kind == "medico":
        fio = g("nombre")
        row = [now, author, fio, g("nacimiento"), g("nie"), g("domicilio"), g("iban"),
               g("email"), g("telefono"), g("nss"), g("local"), g("puesto"),
               "новая", g("comentario")]
    else:
        fio = g("nombre")
        d, mo, y = _split_date(g("fecha"))
        row = [now, author, fio, g("responsable"), g("local_antes"), g("local_despues"),
               g("horas_antes"), g("horas_despues"), g("puesto_antes"), g("puesto_despues"),
               d, mo, y, "новая", g("comentario")]
    return _append(kind, row), fio


def summary(kind: str, data: dict) -> str:
    esc = M.html_lib.escape
    g = lambda k: esc(str(data.get(k, "")).strip())
    if kind == "alta":
        return (f"{g('nombre')} {g('apellido')} · {g('local')}\n"
                f"{g('puesto')} · {g('horas')} ч · с {g('inicio')}\n"
                f"NIE {g('nie')} · тел. {g('telefono')} · NSS {g('nss')}\n"
                f"{g('email')}\n{g('domicilio')} {g('cp')}\nIBAN {g('iban')}")
    if kind == "baja":
        return (f"{g('nombre')} · {g('local')} · {g('puesto')}\n"
                f"Última fecha: {g('fecha')}\n{g('periodo')} · {g('motivo')}\n"
                f"Vacaciones: {g('vacaciones')} дн.")
    if kind == "medico":
        return (f"{g('nombre')} · {g('local')} · {g('puesto')}\n"
                f"NIE {g('nie')} · тел. {g('telefono')} · NSS {g('nss')}")
    return (f"{g('nombre')} · {g('responsable')}\n"
            f"Local: {g('local_antes')} → {g('local_despues')}\n"
            f"Horas: {g('horas_antes')} → {g('horas_despues')}\n"
            f"Puesto: {g('puesto_antes')} → {g('puesto_despues')}\n"
            f"С даты: {g('fecha')}")


# ---------------- ДОСЬЕ СОТРУДНИКА ----------------

def dossier(name: str) -> dict:
    """Всё, что бот знает о человеке: контракт, письма, анкета, заявки."""
    key = set(_norm(name).split())
    hit = lambda s: bool(key & set(_norm(s).split()))

    mails = []
    try:
        for r in M.rows(M.MAIL_WS, force=False):
            if hit(r.get("ФИО", "")) or hit(r.get("Вложение", "")):
                mails.append({"tipo": str(r.get("Тип", "")).strip(),
                              "file": str(r.get("Вложение", "")).strip(),
                              "fecha": str(r.get("Дата", "")).strip(),
                              "msg": str(r.get("MessageID", "")).strip()})
    except Exception as e:
        log.warning("solicitudes: архив почты недоступен: %s", e)

    ficha = {}
    try:
        for r in M.applicants_rows():
            if hit(f"{r.get('Nombre', '')} {r.get('Apellido', '')}"):
                ficha = {k: str(v).strip() for k, v in r.items() if str(v).strip()}
                break
    except Exception as e:
        log.warning("solicitudes: анкеты недоступны: %s", e)

    sols = []
    for kind in SHEETS:
        for i, r in enumerate(_rows(kind), start=2):
            fio = r.get("Nombre y Apellidos") or f"{r.get('Nombre', '')} {r.get('Apellido', '')}"
            if hit(fio):
                sols.append({"tipo": kind, "row": i,
                             "fecha": str(r.get("Дата заявки", "")).strip(),
                             "autor": str(r.get("Автор", "")).strip(),
                             "estado": str(r.get("Статус", "")).strip(),
                             "datos": {k: str(v).strip() for k, v in r.items() if str(v).strip()}})
    sols.sort(key=lambda x: x["fecha"], reverse=True)

    drive = {}
    try:
        import drive_docs
        drive = drive_docs.docs_for(name)
    except Exception as e:
        log.warning("solicitudes: папка с Диска не подтянулась: %s", e)

    return {"mails": mails, "ficha": ficha, "solicitudes": sols, "drive": drive}


# ---------------- ВЕБ-МАРШРУТЫ ----------------

def _routes(app):
    from aiohttp import web
    import hr_web

    def _who(request):
        uid = app["uid_from"](request)
        return (uid, hr_web.user_role(uid)) if uid else (None, "")

    async def meta(request):
        uid, role = _who(request)
        if not uid:
            return web.json_response({"error": "auth"}, status=401)
        return web.json_response({
            "locales": [l for l, _ in LOCALES], "puestos": PUESTOS, "horas": HORAS,
            "periodos": PERIODOS, "motivos": MOTIVOS,
        })

    async def create(request):
        uid, role = _who(request)
        if not uid:
            return web.json_response({"error": "auth"}, status=401)
        body = await request.json()
        kind = str(body.get("tipo", "")).strip()
        if kind not in SHEETS:
            return web.json_response({"error": "тип заявки не распознан"}, status=400)
        data = body.get("datos") or {}
        missing = [f for f in REQUIRED[kind] if not str(data.get(f, "")).strip()]
        if missing:
            return web.json_response({"error": "не заполнено: " + ", ".join(missing)}, status=400)
        author = hr_web.user_name(uid)
        try:
            row, fio = await asyncio.to_thread(save, kind, data, author)
        except Exception as e:
            log.error("solicitudes: не записалось: %s", e, exc_info=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
        asyncio.create_task(_notify(kind, data, author, uid, row, fio))
        return web.json_response({"ok": True, "row": row,
                                  "foto": kind == "alta"})

    async def mine(request):
        uid, role = _who(request)
        if not uid:
            return web.json_response({"error": "auth"}, status=401)
        author = hr_web.user_name(uid)
        out = []
        for kind in SHEETS:
            for i, r in enumerate(await asyncio.to_thread(_rows, kind), start=2):
                if role != "patron" and str(r.get("Автор", "")).strip() != author:
                    continue
                out.append({
                    "tipo": kind, "row": i,
                    "fecha": str(r.get("Дата заявки", "")).strip(),
                    "autor": str(r.get("Автор", "")).strip(),
                    "nombre": str(r.get("Nombre y Apellidos")
                                  or f"{r.get('Nombre', '')} {r.get('Apellido', '')}").strip(),
                    "local": str(r.get("local") or r.get("Local") or r.get("local Antes") or "").strip(),
                    "estado": str(r.get("Статус", "")).strip() or "новая",
                })
        out.sort(key=lambda x: x["fecha"], reverse=True)
        return web.json_response({"solicitudes": out[:200]})

    async def docs(request):
        uid, role = _who(request)
        if not uid or role != "patron":
            return web.json_response({"error": "auth"}, status=401)
        name = request.query.get("name", "")
        if not name:
            return web.json_response({"error": "нет имени"}, status=400)
        try:
            return web.json_response(await asyncio.to_thread(dossier, name))
        except Exception as e:
            log.error("solicitudes: досье не собралось: %s", e, exc_info=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def send_doc(request):
        """Бот присылает файл из архива почты в личку тому, кто нажал."""
        uid, role = _who(request)
        if not uid or role != "patron":
            return web.json_response({"error": "auth"}, status=401)
        body = await request.json()
        asyncio.create_task(_send_file(uid, str(body.get("msg", "")), str(body.get("file", ""))))
        return web.json_response({"ok": True})

    app.router.add_get("/hr/api/sol/meta", meta)
    app.router.add_post("/hr/api/sol/create", create)
    app.router.add_get("/hr/api/sol/mine", mine)
    app.router.add_get("/hr/api/docs", docs)
    app.router.add_post("/hr/api/docs/send", send_doc)


async def _send_file(uid: int, msg_id: str, filename: str):
    try:
        from aiogram.types import BufferedInputFile
        raw = await M.gmail_redownload(msg_id, filename)
        if raw:
            await M.bot.send_document(uid, BufferedInputFile(raw, filename=filename))
        else:
            await M.bot.send_message(uid, f"⚠️ Не удалось скачать {M.html_lib.escape(filename)}.")
    except Exception as e:
        log.error("solicitudes: файл не отправлен: %s", e)


async def _notify(kind: str, data: dict, author: str, uid: int, row: int, fio: str):
    """Патронам — заявка, автору — просьба прислать фото NIE (для Alta)."""
    esc = M.html_lib.escape
    text = (f"{TITLES[kind]}\n<b>Новая заявка</b> от {esc(author)}\n\n{summary(kind, data)}")
    com = str(data.get("comentario", "")).strip()
    if com:
        text += f"\n\n💬 {esc(com)}"
    text += f"\n\n<i>Записано в лист {SHEETS[kind][0]}, строка {row}.</i>"
    for pid in M.patrons():
        try:
            await M.bot.send_message(pid, text[:4000])
        except Exception as e:
            log.error("solicitudes: не уведомила %s: %s", pid, e)
    if kind == "alta":
        _awaiting_photo[uid] = (kind, row, fio)
        try:
            await M.bot.send_message(
                uid,
                f"✅ Заявка на <b>{esc(fio)}</b> отправлена.\n\n"
                "Теперь пришлите сюда <b>фото NIE/TIE</b> этого сотрудника — "
                "приложу к заявке. Если фото нет, напишите «нет».")
        except Exception as e:
            log.error("solicitudes: не попросила фото у %s: %s", uid, e)


def _save_photo(kind: str, row: int, file_id: str):
    w = _ws(kind)
    headers = w.row_values(1)
    if "Фото NIE" in headers:
        M.sheets_write_retry(w.update_cell, row, headers.index("Фото NIE") + 1, file_id)


# ---------------- ПОДКЛЮЧЕНИЕ ----------------

def setup(dp, main_module):
    global M
    M = main_module
    from aiogram import F

    import hr_web
    hr_web.EXTRA_ROUTES.append(_routes)

    @dp.message(F.chat.type == "private", F.photo,
                lambda m: m.from_user and m.from_user.id in _awaiting_photo)
    async def on_photo(m):
        kind, row, fio = _awaiting_photo.pop(m.from_user.id, (None, None, None))
        if not kind:
            return
        file_id = m.photo[-1].file_id
        try:
            await asyncio.to_thread(_save_photo, kind, row, file_id)
        except Exception as e:
            log.error("solicitudes: фото не записано: %s", e)
        await m.answer(f"📎 Фото NIE подшито к заявке на <b>{M.html_lib.escape(fio)}</b>.")
        for pid in M.patrons():
            try:
                await M.bot.send_photo(pid, file_id,
                                       caption=f"📎 NIE/TIE — {M.html_lib.escape(fio)}")
            except Exception:
                pass

    @dp.message(F.chat.type == "private", F.text,
                lambda m: m.from_user and m.from_user.id in _awaiting_photo
                and _norm(m.text) in ("нет", "no", "без фото"))
    async def on_no_photo(m):
        _awaiting_photo.pop(m.from_user.id, None)
        await m.answer("Хорошо, заявка осталась без фото.")

    log.info("%s подключён", VERSION)
