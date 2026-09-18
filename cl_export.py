# =========================================================
#  cl_export.py — файл для импорта сотрудников в Control Laboral
#
#  Control Laboral (controllaboral.es) не даёт открытого API, но умеет
#  «Importar empleados» из Excel по своему шаблону (лист «Hoja1»,
#  25 колонок, поля со * обязательны). Этот модуль собирает такой файл
#  из анкеты Solicitud + чек-листа HR и присылает его в личку.
#
#  Когда приходит файл:
#   • автоматически — как только сотрудник в чек-листе переходит на этап
#     «Подписано» (следующий шаг — «Заведено в Control Laboral»);
#   • по команде /controllaboral — один файл на всех, кто сейчас
#     на этапе «Подписано» (Horario там пустой — вписать в файле);
#   • для одного сотрудника бот сначала спрашивает Horario кнопками
#     (шаблоны из HORARIOS с тем же числом часов, что в анкете);
#   • по кнопке «📤 Файл для Control Laboral» в карточке чек-листа
#     (callback «hrcl:<строка>», кнопку добавить в bot.py одной строкой).
#
#  Без новых библиотек: xlsx пишется вручную (zip + xml), requirements.txt
#  не меняется.
#
#  Подключение в bot.py (рядом с остальными модулями):
#      try:
#          import sys as _sys
#          import cl_export
#          cl_export.setup(dp, _sys.modules[__name__])
#      except Exception as _e:
#          log.error("cl_export не подключён: %s", _e, exc_info=True)
#
#  Настройки (Railway, необязательно): CL_DEFAULTS — JSON, перекрывает
#  значения из DEFAULTS ниже, например
#      {"horario": "General",
#       "politica": "General", "centro": {"boiboi": "Boi Boi Gran Vía"}}
# =========================================================

import io
import os
import re
import json
import zipfile
import logging
from datetime import datetime, date
from xml.sax.saxutils import escape as xml_escape

log = logging.getLogger("sumskaya.cl_export")

VERSION = "cl_export 1.3 · 18.09.2026"

M = None

# Колонки строго как в plantilla.xlsx Control Laboral (порядок важен).
HEADERS = [
    "Nombre *", "Movil *", "Correo", "Alta *", "Baja", "Nif", "Convenio *",
    "Horas semanales *", "Horario *", "Puesto", "Centro",
    "Notificar Email (0/1) *", "Corregir (0/1) *", "Bajas Ausencias (0/1) *",
    "Admitir Bolsa (0/1) *", "Marcar Inicio (0/1) *", "Sin Notificar (0/1) *",
    "Geolocalizable (0/1) *", "Fichaje (0/1) *",
    "Periodo Firma (1 = Diario, 2 = Semanal, 3 = Mensual) *", "CCC",
    "Politica Vacaciones *", "Fecha Nacimiento (dd/mm/yyyy)",
    "Genero (1 = Masculino, 2 = Femenino, 3 = Prefiero no decirlo)",
    "Acceso App (0/1) *",
]
DATE_COLS = {"Alta *", "Baja"}      # в шаблоне — настоящие даты Excel
NUM_COLS = {"Horas semanales *", "Periodo Firma (1 = Diario, 2 = Semanal, 3 = Mensual) *",
            "Genero (1 = Masculino, 2 = Femenino, 3 = Prefiero no decirlo)"} | {
    h for h in HEADERS if "(0/1)" in h}

# ⚠️ ЗНАЧЕНИЯ «ПО УМОЛЧАНИЮ» — сверить с карточкой любого действующего
# сотрудника в Control Laboral (кнопка «Editar»). Названия convenio,
# horario и política должны совпадать с кабинетом буква в букву.
DEFAULTS = {
    # Convenio 2026: у пекарни свой, у ресторанов свой (названия как в
    # выпадающем списке Control Laboral).
    "convenio": {
        "reina": "2026 - Convenio Colectivo de Hostelería de la provincia de Valencia",
        "fransia": "2026 - Convenio Colectivo de Hostelería de la provincia de Valencia",
        "boiboi": "2026 - Convenio Colectivo de Hostelería de la provincia de Valencia",
        "panaderia": "2026 - Convenio Colectivo de Panadería y Pastelería de la Comunidad Valenciana",
    },
    # Из карточки действующего сотрудника (18.09.2026):
    "horario": "",             # выбирается кнопкой для каждого сотрудника (см. HORARIOS)
    "politica": "POLITICA EMPLEADOS NUEVOS",
    "ccc": "",
    "centro": {                # локаль бота → «Centro» в Control Laboral
        "reina": "P+S Reina",
        "fransia": "P+S Francia",
        "panaderia": "P+S Bakery",
        "boiboi": "Boi Boi",   # центр заводится в Control Laboral вручную, имя ровно «Boi Boi»
    },
    "flags": {
        "Notificar Email (0/1) *": 1,
        "Corregir (0/1) *": 0,
        "Bajas Ausencias (0/1) *": 1,
        "Admitir Bolsa (0/1) *": 0,
        "Marcar Inicio (0/1) *": 0,
        "Sin Notificar (0/1) *": 0,
        "Geolocalizable (0/1) *": 0,
        "Fichaje (0/1) *": 0,          # метод «Validación de la jornada», не фичахе
        "Acceso App (0/1) *": 1,       # App móvil — да
    },
    "periodo_firma": 3,        # 1 диарио, 2 семанал, 3 менсуаль
}


# Шаблоны «Horario laboral» из Control Laboral — названия буква в букву.
# Бот показывает кнопками те, что совпадают по часам из анкеты.
HORARIOS = [
    "16 horas 2 dias lunes-martes",
    "20 horas 11-15 mar-sab",
    "20 horas 14-18 mie-dom",
    "20 horas 16-20mie-dom",
    "20 horas 4.00-08.00 lu-vie",
    "20 horas 7-11 lu-vie",
    "20 horas 9-13 mar-sab",
    "25 horas 12 - 17",
    "25 horas 18-23 lu-vie",
    "30 horas 10-16 lu-vie",
    "30 horas 12 - 18",
    "30 horas 9-15 lu-vie",
    "30 horas 9-15 Mie-Dom",
    "30 horas lu-vie 14-20",
    "35 horas 16-22",
    "35 horas 7-14 lu-vie",
    "40 horas 10-18 lu-vie",
    "40 horas 10-18 Mie-Dom",
    "40 horas 7-15 mar-sab",
    "40 horas lu-vie 09:00-17:00",
]


# Обычные графики 40 ч у ресторанов — показываем первыми.
RESTAURANT_40_FIRST = ["40 horas lu-vie 09:00-17:00", "40 horas 10-18 lu-vie"]


def _start_hour(name: str):
    """Час начала смены из названия: «20 horas 4.00-08.00» → 4, «30 horas lu-vie 14-20» → 14."""
    m = re.search(r"(\d{1,2})(?:[.:]\d{2})?\s*-\s*\d", name)
    return int(m.group(1)) if m else None


def horarios_for(hours, loc_code: str = "") -> list:
    """[(индекс, название, рекомендован)] шаблонов с тем же числом часов
    (если таких нет — все), в удобном порядке:
      • пекарня — сначала ранние смены;
      • рестораны — сначала смены с 9 утра и позже, для 40 ч первыми
        «9–17» и «10–18»."""
    try:
        h = int(float(str(hours).replace(",", ".")))
    except Exception:
        h = None
    all_ = list(enumerate(HORARIOS))
    same = [(i, n) for i, n in all_ if h is not None and n.strip().startswith(f"{h} horas")]
    opts = same or all_

    def key(item):
        i, n = item
        st = _start_hour(n)
        st_sort = st if st is not None else 99
        if loc_code == "panaderia":
            return (0 if st is not None and st < 9 else 1, st_sort, n)
        pref = RESTAURANT_40_FIRST.index(n) if n in RESTAURANT_40_FIRST else 99
        return (pref, 0 if st is None or st >= 9 else 1, st_sort, n)

    opts = sorted(opts, key=key)
    out = []
    for i, n in opts:
        st = _start_hour(n)
        if loc_code == "panaderia":
            rec = st is not None and st < 9
        else:
            rec = n in RESTAURANT_40_FIRST or (h != 40 and st is not None and st >= 9)
        out.append((i, n, rec))
    return out


def config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    raw = os.environ.get("CL_DEFAULTS", "").strip()
    if raw:
        try:
            over = json.loads(raw)
            for k, v in over.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log.error("CL_DEFAULTS не читается как JSON: %s", e)
    return cfg


# ---------------- ДАННЫЕ ----------------

def parse_date(s):
    s = str(s or "").strip().split(" ")[0]
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def clean_phone(s: str) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d.startswith("34"):
        d = d[2:]
    if len(d) == 13 and d.startswith("0034"):
        d = d[4:]
    return d


def clean_hours(s: str):
    m = re.search(r"\d+(?:[.,]\d+)?", str(s or ""))
    if not m:
        return ""
    v = float(m.group(0).replace(",", "."))
    return int(v) if v.is_integer() else v


def build_row(hr_row: dict, applicant: dict, cfg: dict):
    """Строка шаблона + список проблем (пустые обязательные поля)."""
    nombre = str(applicant.get("Nombre", "")).strip()
    apellido = str(applicant.get("Apellido", "")).strip()
    full = f"{nombre} {apellido}".strip() or str(hr_row.get("ФИО", "")).strip()
    loc_code = M.match_locale_code(str(applicant.get("Local", "") or hr_row.get("Локаль", "")))
    puesto = str(applicant.get("Título profesional", "") or hr_row.get("Должность", "")).strip()
    alta = parse_date(applicant.get("Fecha de inicio", ""))
    nac = parse_date(applicant.get("Fecha de nacimiento", ""))

    row = {h: "" for h in HEADERS}
    row.update({
        "Nombre *": full,
        "Movil *": clean_phone(applicant.get("Teléfono", "")),
        "Correo": str(applicant.get("Correo electrónico", "")).strip(),
        "Alta *": alta or "",
        "Nif": str(applicant.get("NIE/TIE", "")).strip().upper().replace(" ", ""),
        "Convenio *": (cfg.get("convenio", {}).get(loc_code, "")
                       if isinstance(cfg.get("convenio"), dict) else cfg.get("convenio", "")),
        "Horas semanales *": clean_hours(applicant.get("Número de horas bajo contrato", "")),
        "Horario *": cfg.get("horario", ""),
        "Puesto": M.normalize_puesto(puesto) if puesto else "",
        "Centro": cfg.get("centro", {}).get(loc_code, ""),
        "Periodo Firma (1 = Diario, 2 = Semanal, 3 = Mensual) *": cfg.get("periodo_firma", 3),
        "CCC": cfg.get("ccc", ""),
        "Politica Vacaciones *": cfg.get("politica", ""),
        "Fecha Nacimiento (dd/mm/yyyy)": nac.strftime("%d/%m/%Y") if nac else "",
    })
    row.update(cfg.get("flags", {}))
    # Género не заполняем догадкой по имени — поле необязательное.

    problems = [h.replace(" *", "") for h in HEADERS
                if h.endswith("*") and row[h] in ("", None)]
    return row, problems


# ---------------- XLSX БЕЗ БИБЛИОТЕК ----------------

def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _excel_serial(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def make_xlsx(rows: list) -> bytes:
    """Минимальный xlsx: один лист «Hoja1», шапка + строки."""
    def cell(ref, v, header=False):
        if isinstance(v, date):
            return f'<c r="{ref}" s="2"><v>{_excel_serial(v)}</v></c>'
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f'<c r="{ref}"><v>{v}</v></c>'
        if v in ("", None):
            return ""
        st = ' s="1"' if header else ""
        return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{xml_escape(str(v))}</t></is></c>'

    xml_rows = []
    all_rows = [dict(zip(HEADERS, HEADERS))] + rows
    for ri, r in enumerate(all_rows, start=1):
        cells = "".join(cell(f"{_col(ci)}{ri}", r.get(h, ""), header=(ri == 1))
                        for ci, h in enumerate(HEADERS, start=1))
        xml_rows.append(f'<row r="{ri}">{cells}</row>')
    last = f"{_col(len(HEADERS))}{len(all_rows)}"

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<dimension ref="A1:{last}"/>'
             '<sheetFormatPr defaultRowHeight="15"/>'
             f'<cols><col min="1" max="{len(HEADERS)}" width="20" customWidth="1"/></cols>'
             f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Hoja1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
              '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
              '</Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ---------------- СБОРКА ПО ЧЕК-ЛИСТУ ----------------

STAGE_SIGNED = "Подписано"


def _applicant_for(hr_row: dict) -> dict:
    try:
        return M.applicants_rows()[int(str(hr_row.get("RowKey", "")).strip()) - 2]
    except Exception:
        return {}


def collect(hr_indexes=None, horario=None):
    """[(fio, row, problems)] по заданным строкам чек-листа, либо по всем
    на этапе «Подписано». horario — выбранный кнопкой шаблон."""
    cfg = config()
    if horario:
        cfg["horario"] = horario
    data = M.rows(M.HR_WS, force=True)
    out = []
    for idx, r in enumerate(data, start=2):
        if hr_indexes is not None:
            if idx not in hr_indexes:
                continue
        elif str(r.get("Статус", "")).strip() != STAGE_SIGNED:
            continue
        row, problems = build_row(r, _applicant_for(r), cfg)
        out.append((str(r.get("ФИО", "")).strip(), row, problems))
    return out


async def ask_horario(chat_ids, hr_idx: int):
    """Кнопки выбора Horario для одного сотрудника, потом — файл."""
    import asyncio
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    data = await asyncio.to_thread(M.rows, M.HR_WS, True)
    try:
        r = data[hr_idx - 2]
    except IndexError:
        return
    app = await asyncio.to_thread(_applicant_for, r)
    hours = clean_hours(app.get("Número de horas bajo contrato", ""))
    loc_code = M.match_locale_code(str(app.get("Local", "") or r.get("Локаль", "")))
    opts = horarios_for(hours, loc_code)
    kb = [[InlineKeyboardButton(text=(f"⭐ {n}" if rec else n)[:60],
                                callback_data=f"hrclh:{hr_idx}:{i}")]
          for i, n, rec in opts]
    kb.append([InlineKeyboardButton(text="✏️ Другой — впишу в файле сам",
                                    callback_data=f"hrclh:{hr_idx}:x")])
    if hasattr(M, "nav_row"):
        kb.append(M.nav_row())
    fio = M.html_lib.escape(str(r.get("ФИО", "")).strip())
    text = (f"📤 <b>{fio}</b> — готовлю файл для Control Laboral.\n"
            f"Часов по анкете: <b>{hours or '—'}</b>\n\nВыберите Horario laboral:")
    for cid in chat_ids:
        try:
            await M.bot.send_message(cid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        except Exception as e:
            log.error("cl_export: не удалось спросить horario %s: %s", cid, e)


async def send_file(chat_ids, hr_indexes=None, silent_if_empty=False, horario=None):
    import asyncio
    from aiogram.types import BufferedInputFile
    items = await asyncio.to_thread(collect, hr_indexes, horario)
    if not items:
        if not silent_if_empty:
            for cid in chat_ids:
                kb = None
                if hasattr(M, "nav_row"):
                    from aiogram.types import InlineKeyboardMarkup
                    kb = InlineKeyboardMarkup(inline_keyboard=[M.nav_row()])
                await M.bot.send_message(cid, "Нет сотрудников на этапе «Подписано» — "
                                              "в Control Laboral заводить некого.", reply_markup=kb)
        return
    raw = make_xlsx([row for _, row, _ in items])
    stamp = M.now_local().strftime("%d-%m-%Y_%H%M")
    fname = f"ControlLaboral_import_{stamp}.xlsx"
    lines = [f"📤 <b>Файл для Control Laboral</b> — {len(items)} чел.\n"]
    for fio, _, problems in items:
        esc = M.html_lib.escape
        if problems:
            lines.append(f"⚠️ {esc(fio)} — не заполнено: {esc(', '.join(problems))}")
        else:
            lines.append(f"✅ {esc(fio)}")
    lines.append("\nEmpleados → <b>Importar empleados</b> → выбрать этот файл → Aceptar.\n"
                 "✔️ Поставьте галочку «Generar y enviar las contraseñas por email y sms».\n"
                 "После загрузки передвиньте сотрудника в чек-листе на «Заведено в Control Laboral».")
    if any(p for _, _, p in items):
        lines.append("\n<i>Пустые обязательные поля Control Laboral не примет — "
                     "допишите их в файле перед загрузкой.</i>")
    for cid in chat_ids:
        try:
            await M.bot.send_document(cid, BufferedInputFile(raw, filename=fname),
                                      caption="\n".join(lines)[:1024])
        except Exception as e:
            log.error("cl_export: не удалось отправить файл %s: %s", cid, e)


# ---------------- ПОДКЛЮЧЕНИЕ ----------------

def _wrap_set_hr_stage():
    """Перехватываем смену этапа: на «Подписано» — сразу шлём файл
    патронам. bot.py ищет set_hr_stage по имени при каждом вызове,
    поэтому подмены в модуле достаточно, bot.py править не нужно."""
    import asyncio
    orig = M.set_hr_stage
    if getattr(orig, "_cl_wrapped", False):
        return

    def wrapped(hr_row_idx, stage, author):
        res = orig(hr_row_idx, stage, author)
        if stage == STAGE_SIGNED:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(ask_horario(M.patrons(), hr_row_idx))
            except RuntimeError:
                pass
            except Exception as e:
                log.error("cl_export: авто-файл не отправлен: %s", e)
        return res

    wrapped._cl_wrapped = True
    M.set_hr_stage = wrapped


def setup(dp, main_module):
    global M
    M = main_module
    from aiogram import F
    from aiogram.filters import Command

    _wrap_set_hr_stage()

    @dp.message(Command("controllaboral"))
    async def cmd_cl(m):
        if m.chat.type != "private" or not M.is_patron(M.get_user(m.from_user.id)):
            return
        await send_file([m.chat.id])

    @dp.callback_query(F.data.startswith("hrcl:"))
    async def cb_cl(c):
        if not M.is_patron(M.get_user(c.from_user.id)):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        await ask_horario([c.from_user.id], int(c.data.split(":")[1]))

    @dp.callback_query(F.data.startswith("hrclh:"))
    async def cb_cl_horario(c):
        if not M.is_patron(M.get_user(c.from_user.id)):
            await c.answer("Только Патрон", show_alert=True)
            return
        _, idx, hi = c.data.split(":")
        horario = HORARIOS[int(hi)] if hi.isdigit() and int(hi) < len(HORARIOS) else None
        await c.answer("Собираю файл…")
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_file([c.from_user.id], hr_indexes={int(idx)}, horario=horario)

    log.info("%s подключён", VERSION)
