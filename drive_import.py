# =========================================================
#  drive_import.py — восстановление архива персонала по документам с Диска
#
#  Берёт папки сотрудников из «CONTRATOS de TRABAJO» (drive_docs), читает
#  контракты через Gemini и дополняет Registro: пустые ячейки заполняет,
#  людей, которых в Registro нет, добавляет новой строкой. Ничего уже
#  заполненного не перезаписывает — только пустое.
#
#  В боте: HR → «🗂 Восстановить архив с Диска» (только Патрон).
#   1) сверка: сколько папок связалось с Registro, кого нет в таблице,
#      у кого нет папки;
#   2) кнопка «Заполнить пустые поля» — по 15 человек за проход;
#   3) кнопка «Добавить недостающих» — новые строки в Registro.
#
#  Нужны: DRIVE_ROOT_ID (папка на Диске) и GEMINI_API_KEY (чтение контрактов).
#
#  Подключение в bot.py (после drive_docs):
#      try:
#          import sys as _sys
#          import drive_import
#          drive_import.setup(dp, _sys.modules[__name__])
#      except Exception as _e:
#          log.error("drive_import не подключён: %s", _e, exc_info=True)
# =========================================================

import os
import re
import json
import base64
import asyncio
import logging
import unicodedata

import requests

log = logging.getLogger("sumskaya.drive_import")

VERSION = "drive_import 1.0 · 23.09.2026"

M = None
BATCH = int(os.environ.get("DRIVE_IMPORT_BATCH", "15") or 15)
TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "90") or 90)
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """Eres un extractor de datos de documentos laborales españoles
(contratos de trabajo, anexos, bajas). Devuelve SOLO un objeto JSON.

Campos (usa null si el dato no aparece, nunca inventes):
- "nombre": nombre y apellidos del TRABAJADOR (no de la empresa).
- "nie": NIE/TIE/DNI del trabajador.
- "nss": número de la seguridad social del trabajador.
- "nacimiento": fecha de nacimiento, dd.mm.aaaa.
- "domicilio": domicilio del trabajador.
- "telefono": teléfono del trabajador.
- "email": correo del trabajador.
- "iban": IBAN del trabajador, sin espacios.
- "puesto": categoría o puesto de trabajo.
- "horas": horas semanales de la jornada, solo el número.
- "alta": fecha de inicio del contrato, dd.mm.aaaa.
- "baja": fecha de fin o de baja si el documento es una baja, dd.mm.aaaa.
- "tipo": tipo de contrato tal como aparece (indefinido, temporal, etc.).
- "centro": centro de trabajo o local.

La empresa es SUMSKAYA LINE SL — nunca la devuelvas como trabajador.
"""

MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                      "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                      "gemini-flash-latest", "gemini-2.5-flash-lite"] if m]
_model = None

# поле из документа → ключи колонок Registro (как их ищет hr_web.map_columns)
FIELD_TO_COL = {
    "nie": "nie", "nacimiento": "nac", "domicilio": "domicilio",
    "telefono": "tel", "email": "email", "iban": "iban",
    "puesto": "puesto", "horas": "horas", "tipo": "tipo",
}
DATE_FIELDS = {"alta": "alta", "baja": "baja"}   # пишутся в три колонки


def _norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.replace("_", " ").replace("-", " ").replace(".", " ").split())


def _norm_date(v) -> str:
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", str(v or ""))
    if not m:
        return ""
    d, mo, y = m.groups()
    y = "20" + y if len(y) == 2 else y
    return f"{int(d):02d}.{int(mo):02d}.{y}"


# ---------------- GEMINI ----------------

def _gemini(data: bytes, mime: str) -> dict:
    global _model
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("нет GEMINI_API_KEY")
    body = {"contents": [{"parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(data).decode()}}]}],
            "generationConfig": {"temperature": 0,
                                 "response_mime_type": "application/json"}}
    order = ([_model] if _model else []) + [m for m in MODELS if m != _model]
    last = None
    for model in order:
        try:
            r = requests.post(ENDPOINT.format(model=model),
                              headers={"x-goog-api-key": key,
                                       "Content-Type": "application/json"},
                              json=body, timeout=TIMEOUT)
            if r.status_code == 404:
                last = f"модель {model} недоступна"
                continue
            if r.status_code != 200:
                last = f"Gemini {r.status_code}: {r.text[:200]}"
                continue
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            start, end = txt.find("{"), txt.rfind("}")
            _model = model
            return json.loads(txt[start:end + 1])
        except Exception as e:
            last = str(e)
    raise RuntimeError(last or "Gemini не ответил")


MIME = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp", "heic": "image/heic"}


def _pick_file(files: list):
    """Главный документ папки: сначала контракт, потом любой читаемый."""
    def score(f):
        n = _norm(f["name"])
        s = 0
        if "contrato" in n or "contract" in n:
            s += 3
        if n.endswith(".pdf") or ".pdf" in n:
            s += 1
        return s
    ok = [f for f in files if f["name"].rsplit(".", 1)[-1].lower() in MIME]
    if not ok:
        return None
    return sorted(ok, key=score, reverse=True)[0]


def read_folder(folder: dict) -> dict:
    """Данные сотрудника из документов его папки."""
    import drive_docs
    f = _pick_file(folder.get("files") or [])
    if not f:
        return {"error": "в папке нет PDF или фото"}
    mime = MIME[f["name"].rsplit(".", 1)[-1].lower()]
    raw = drive_docs.download(f["id"])
    if len(raw) > 18 * 1024 * 1024:
        return {"error": f"{f['name']} слишком большой"}
    data = _gemini(raw, mime)
    out = {k: str(v).strip() for k, v in data.items() if v not in (None, "", "null")}
    for k in ("alta", "baja", "nacimiento"):
        if out.get(k):
            out[k] = _norm_date(out[k])
    out["_file"] = f["name"]
    return out


# ---------------- REGISTRO ----------------

def _registro_ctx():
    import hr_web
    if hr_web.M is None:
        hr_web.M = M
    w = M.registro_ws()
    header_row = M.registro_header_row(w)
    headers = w.row_values(header_row)
    cols = hr_web.map_columns(headers)
    parts = {
        "alta": hr_web._date_parts_cols(headers, cols.get("alta")),
        "baja": hr_web._date_parts_cols(headers, cols.get("baja")),
    }
    return w, headers, cols, parts


def fill_row(w, row_idx: int, cols: dict, parts: dict, data: dict) -> list:
    """Заполняет ТОЛЬКО пустые ячейки строки. Возвращает список полей."""
    values = w.row_values(row_idx)
    get = lambda i: (values[i].strip() if i is not None and i < len(values) else "")
    done = []

    for field, key in FIELD_TO_COL.items():
        i = cols.get(key)
        if i is None or not data.get(field) or get(i):
            continue
        M.sheets_write_retry(w.update_cell, row_idx, i + 1, data[field])
        done.append(field)

    for field, key in DATE_FIELDS.items():
        i = cols.get(key)
        d = data.get(field)
        if i is None or not d or get(i):
            continue
        day, mo, year = d.split(".")
        M.sheets_write_retry(w.update_cell, row_idx, i + 1, str(int(day)))
        mes_c, ano_c = parts.get(field, (None, None))
        if mes_c is not None:
            M.sheets_write_retry(w.update_cell, row_idx, mes_c + 1, str(int(mo)))
        if ano_c is not None:
            M.sheets_write_retry(w.update_cell, row_idx, ano_c + 1, year)
        done.append(field)
    return done


def add_row(data: dict, name: str, local_tag: str) -> bool:
    """Новая строка в Registro для сотрудника, которого там не было."""
    vals = {"Nombre y Apellidos": name}
    if local_tag:
        vals["Local"] = local_tag
    pairs = [("puesto", "Puesto"), ("horas", "Horas"), ("tipo", "Tipo de contrato"),
             ("iban", "IBAN"), ("domicilio", "Domicilio"), ("telefono", "Teléfono Móvil"),
             ("email", "Dirección e-mail"), ("nie", "TIE/NIE"),
             ("nacimiento", "Fecha Nacimiento")]
    for k, col in pairs:
        if data.get(k):
            vals[col] = data[k]
    if data.get("alta"):
        d, mo, y = data["alta"].split(".")
        vals.update({"Fecha de Alta": str(int(d)), "Mes": str(int(mo)), "Año": y})
    M.registro_append(vals, first_col_value=name)
    return True


LOCAL_BY_FOLDER = {"reina": "REINA", "francia": "FRANCIA", "fransia": "FRANCIA",
                   "bakary": "BAKERY", "bakery": "BAKERY", "panaderia": "BAKERY",
                   "boi": "BOI BOI", "oficina": "OFFICE", "office": "OFFICE"}


def local_tag(folder_local: str) -> str:
    n = _norm(folder_local)
    for k, v in LOCAL_BY_FOLDER.items():
        if k in n:
            return v
    return ""


# ---------------- СВЕРКА ----------------

def audit() -> dict:
    """Кто связался, кого нет в Registro, у кого нет папки."""
    import hr_web, drive_docs
    if hr_web.M is None:
        hr_web.M = M
    folders = drive_docs.tree(force=True)
    data = hr_web._load_sync()
    people = [r for r in data["records"] if r.get("row", 0) > 0]

    _, headers, cols, _ = _registro_ctx()
    key_cols = [c for c in ("nie", "alta", "puesto", "horas", "iban", "tel", "email") if c in cols]

    matched, no_person = [], []
    used = set()
    for f in folders:
        want = {w for w in _norm(f["name"]).split() if len(w) > 2}
        best, score = None, 0
        for p in people:
            have = {w for w in _norm(p["name"]).split() if len(w) > 2}
            s = len(want & have)
            if s > score:
                best, score = p, s
        if best and (score >= 2 or (score == 1 and len(want) == 1)):
            matched.append((f, best))
            used.add(best["row"])
        else:
            no_person.append(f)

    empties = []
    for f, p in matched:
        gaps = [c for c in key_cols if not str(p.get(
            {"nie": "nie", "alta": "alta", "puesto": "puesto", "horas": "horas",
             "iban": "iban", "tel": "tel", "email": "email"}[c], "")).strip()]
        if gaps:
            empties.append((f, p, gaps))
    no_folder = [p for p in people if p["row"] not in used]
    return {"folders": folders, "matched": matched, "no_person": no_person,
            "no_folder": no_folder, "empties": empties, "people": people}


# ---------------- ТЕЛЕГРАМ ----------------

def _kb(rows):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = [[InlineKeyboardButton(text=t, callback_data=d)] for t, d in rows]
    if hasattr(M, "nav_row"):
        kb.append(M.nav_row())
    return InlineKeyboardMarkup(inline_keyboard=kb)


def setup(dp, main_module):
    global M
    M = main_module
    from aiogram import F
    from aiogram.filters import Command

    def patron(uid) -> bool:
        try:
            return M.is_patron(M.get_user(uid))
        except Exception:
            return False

    async def report(chat_id: int):
        import drive_docs
        if not drive_docs.enabled():
            await M.bot.send_message(chat_id, "⚠️ Не задан <code>DRIVE_ROOT_ID</code> — "
                                              "папка с Диска не подключена.")
            return
        await M.bot.send_message(chat_id, "🗂 Смотрю Диск и Registro, это займёт полминуты…")
        try:
            a = await asyncio.to_thread(audit)
        except Exception as e:
            log.error("drive_import: сверка упала: %s", e, exc_info=True)
            await M.bot.send_message(chat_id, f"⚠️ Не получилось: <code>{M.html_lib.escape(str(e))}</code>")
            return
        esc = M.html_lib.escape
        lines = [
            "🗂 <b>Сверка Диска и Registro</b>\n",
            f"Папок сотрудников на Диске: <b>{len(a['folders'])}</b>",
            f"Связались с Registro: <b>{len(a['matched'])}</b>",
            f"Папка есть, а в Registro нет: <b>{len(a['no_person'])}</b>",
            f"В Registro есть, папки нет: <b>{len(a['no_folder'])}</b>",
            f"Связаны, но с пустыми полями: <b>{len(a['empties'])}</b>",
        ]
        if a["no_person"]:
            lines.append("\n<b>Нет в Registro:</b>")
            lines += [f"• {esc(f['name'])} ({esc(f['local'])})" for f in a["no_person"][:15]]
            if len(a["no_person"]) > 15:
                lines.append(f"…и ещё {len(a['no_person']) - 15}")
        if a["no_folder"]:
            lines.append("\n<b>Нет папки на Диске:</b>")
            lines += [f"• {esc(p['name'])}" for p in a["no_folder"][:15]]
            if len(a["no_folder"]) > 15:
                lines.append(f"…и ещё {len(a['no_folder']) - 15}")
        rows = []
        if a["empties"]:
            rows.append((f"✏️ Заполнить пустые поля ({len(a['empties'])})", "drvfill"))
        if a["no_person"]:
            rows.append((f"➕ Добавить в Registro ({len(a['no_person'])})", "drvadd"))
        rows.append(("🔄 Пересчитать", "drvimp"))
        await M.bot.send_message(chat_id, "\n".join(lines)[:4000], reply_markup=_kb(rows))

    @dp.message(Command("restaurar"))
    async def cmd_restaurar(m):
        if m.chat.type == "private" and patron(m.from_user.id):
            await report(m.chat.id)

    @dp.callback_query(F.data == "drvimp")
    async def cb_report(c):
        if not patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        await report(c.from_user.id)

    @dp.callback_query(F.data == "drvfill")
    async def cb_fill(c):
        if not patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer("Читаю документы…")
        uid = c.from_user.id
        a = await asyncio.to_thread(audit)
        targets = a["empties"][:BATCH]
        if not targets:
            await M.bot.send_message(uid, "Пустых полей не осталось.")
            return
        w, headers, cols, parts = await asyncio.to_thread(_registro_ctx)
        esc, done, fails = M.html_lib.escape, [], []
        for f, p, gaps in targets:
            try:
                data = await asyncio.to_thread(read_folder, f)
                if data.get("error"):
                    fails.append(f"{p['name']} — {data['error']}")
                    continue
                filled = await asyncio.to_thread(fill_row, w, p["row"], cols, parts, data)
                done.append(f"{p['name']} — {', '.join(filled) if filled else 'нечего дописывать'}")
            except Exception as e:
                log.error("drive_import: %s — %s", p["name"], e)
                fails.append(f"{p['name']} — {type(e).__name__}: {e}")
            await asyncio.sleep(1.5)
        text = [f"✏️ <b>Заполнено по документам: {len(done)}</b>\n"]
        text += [f"• {esc(x)}" for x in done[:25]]
        if fails:
            text.append("\n<b>Не получилось:</b>")
            text += [f"• {esc(x)}" for x in fails[:15]]
        left = len(a["empties"]) - len(targets)
        if left > 0:
            text.append(f"\nОсталось: <b>{left}</b> — нажмите ещё раз.")
        await M.bot.send_message(uid, "\n".join(text)[:4000],
                                 reply_markup=_kb([("✏️ Ещё", "drvfill"), ("🔄 Сверка", "drvimp")]
                                                  if left > 0 else [("🔄 Сверка", "drvimp")]))

    @dp.callback_query(F.data == "drvadd")
    async def cb_add(c):
        if not patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer("Читаю документы…")
        uid = c.from_user.id
        a = await asyncio.to_thread(audit)
        targets = a["no_person"][:BATCH]
        if not targets:
            await M.bot.send_message(uid, "Все папки уже есть в Registro.")
            return
        esc, done, fails = M.html_lib.escape, [], []
        for f in targets:
            try:
                data = await asyncio.to_thread(read_folder, f)
                if data.get("error"):
                    fails.append(f"{f['name']} — {data['error']}")
                    continue
                name = data.get("nombre") or f["name"]
                await asyncio.to_thread(add_row, data, name, local_tag(f["local"]))
                done.append(f"{name} ({f['local']})")
            except Exception as e:
                log.error("drive_import: %s — %s", f["name"], e)
                fails.append(f"{f['name']} — {type(e).__name__}: {e}")
            await asyncio.sleep(2)
        text = [f"➕ <b>Добавлено в Registro: {len(done)}</b>\n"]
        text += [f"• {esc(x)}" for x in done[:25]]
        if fails:
            text.append("\n<b>Не получилось:</b>")
            text += [f"• {esc(x)}" for x in fails[:15]]
        left = len(a["no_person"]) - len(targets)
        if left > 0:
            text.append(f"\nОсталось: <b>{left}</b> — нажмите ещё раз.")
        await M.bot.send_message(uid, "\n".join(text)[:4000],
                                 reply_markup=_kb([("➕ Ещё", "drvadd"), ("🔄 Сверка", "drvimp")]
                                                  if left > 0 else [("🔄 Сверка", "drvimp")]))

    log.info("%s подключён", VERSION)
