# =========================================================
#  hr_web.py — веб-архив сотрудников по таблице Registro
#
#  Бот поднимает рядом с собой маленький веб-сервер (aiohttp уже стоит
#  вместе с aiogram — requirements.txt НЕ меняется). Страница читает
#  Registro через тот же сервисный аккаунт и даёт отчёты с фильтрами:
#  сколько человек в каком локале, по отделам, на любую дату, приёмы и
#  увольнения по месяцам, карточка сотрудника, выгрузка CSV.
#
#  Вход — только по личной ссылке из бота (команда /archivo или кнопка
#  «🌐 Архив сотрудников» в HR). Ссылка подписана и живёт 24 часа;
#  права Патрона перепроверяются при каждом запросе.
#
#  Подключение в bot.py (сразу после остальных модулей):
#      try:
#          import sys as _sys
#          import hr_web
#          hr_web.setup(dp, _sys.modules[__name__])
#      except Exception as _e:
#          log.error("hr_web не подключён: %s", _e, exc_info=True)
#
#  Переменные Railway:
#      HR_WEB_URL  — публичный адрес сервиса, например
#                    https://sumskaya-line-bot-production.up.railway.app
#      PORT        — Railway ставит сам
# =========================================================

import os
import re
import hmac
import json
import time
import base64
import asyncio
import hashlib
import logging
from datetime import datetime, date

log = logging.getLogger("sumskaya.hr_web")

VERSION = "hr_web 1.5 · 24.09.2026"

M = None          # модуль bot.py — берём оттуда таблицы, роли, bot
_runner = None
TOKEN_TTL = 24 * 3600           # ссылка из бота
COOKIE_TTL = 90 * 24 * 3600     # вход в браузере помнится 90 дней
CACHE_TTL = 60
_cache = {"ts": 0, "data": None}


# ---------------- ПОДПИСЬ ССЫЛКИ ----------------

def _secret() -> bytes:
    base = os.environ.get("HR_WEB_SECRET") or getattr(M, "BOT_TOKEN", "") or "x"
    return hashlib.sha256(("hr_web:" + base).encode()).digest()


def make_token(uid: int, ttl: int = TOKEN_TTL) -> str:
    payload = f"{uid}:{int(time.time()) + ttl}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    raw = f"{payload}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def check_token(token: str):
    """uid, если подпись верна и срок не вышел, иначе None."""
    if not token:
        return None
    try:
        pad = "=" * (-len(token) % 4)
        uid, exp, sig = base64.urlsafe_b64decode(token + pad).decode().split(":")
        good = hmac.new(_secret(), f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        if int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


def check_webapp(init_data: str):
    """uid из Telegram Mini App (кнопка внутри Telegram), если подпись
    initData верна (ключ — токен бота) и ей не больше суток."""
    if not init_data:
        return None
    try:
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", str(getattr(M, "BOT_TOKEN", "")).encode(),
                          hashlib.sha256).digest()
        good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, good):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 24 * 3600:
            return None
        return int(json.loads(pairs.get("user", "{}")).get("id"))
    except Exception:
        return None


def _is_patron(uid: int) -> bool:
    try:
        return M.is_patron(M.get_user(uid))
    except Exception as e:
        log.error("hr_web: не удалось проверить роль %s: %s", uid, e)
        return False


def user_role(uid: int) -> str:
    """'patron' — видит всё; 'manager' — только подача заявок; '' — нет доступа."""
    try:
        u = M.get_user(uid)
        if not u or str(u.get("Статус", "")).strip() != "active":
            return ""
        role = str(u.get("Роль", "")).strip()
        if role == M.ROLE_PATRON:
            return "patron"
        if role == M.ROLE_MANAGER:
            return "manager"
    except Exception as e:
        log.error("hr_web: не удалось проверить роль %s: %s", uid, e)
    return ""


def user_name(uid: int) -> str:
    try:
        return str((M.get_user(uid) or {}).get("Имя", "")).strip() or str(uid)
    except Exception:
        return str(uid)


# Другие модули (solicitudes) добавляют сюда свои маршруты: func(app).
EXTRA_ROUTES = []


# ---------------- ЧТЕНИЕ REGISTRO ----------------

# Какие колонки Registro нам нужны и как их узнать по заголовку.
# Сверка через _reg_norm из bot.py (терпит кириллические двойники).
FIELDS = [
    # ключ,       варианты начала заголовка (после нормализации)
    ("local",     ["local"]),
    ("dep",       ["departamento"]),
    ("puesto",    ["puesto"]),
    ("tipo",      ["tipo de contrato", "tipo contrato", "tipo"]),
    ("alta",      ["fecha de alta", "fecha alta", "alta"]),
    ("baja",      ["fecha de baja", "fecha baja"]),
    ("periodo",   ["periodo de despido", "período de despido", "periodo", "período"]),
    ("motivo",    ["motivo"]),
    ("sex",       ["sex", "sexo"]),
    ("horas",     ["horas"]),
    ("horario",   ["horario"]),
    ("nac",       ["fecha nacimiento", "fecha de nacimiento"]),
    ("nie",       ["tie/nie", "nie/tie", "nie", "tie", "dni"]),
    ("tel",       ["telefono", "teléfono"]),
    ("email",     ["direccion e-mail", "dirección e-mail", "correo", "e-mail", "email"]),
    ("iban",      ["iban"]),
    ("domicilio", ["domicilio"]),
    ("contrato",  ["contrato"]),
    ("nss",       ["numero seguridad social", "número seguridad social",
                   "numero de seguridad social", "n seguridad social", "nss"]),
]

# Поля карточки, которые Патрон может править прямо с сайта. «name» —
# особый случай (колонка A, туда пишем напрямую по имени столбца нет).
EDITABLE_FIELDS = {"local", "dep", "puesto", "tipo", "horas", "horario", "nie",
                   "tel", "email", "domicilio", "iban", "periodo", "motivo",
                   "sex", "nss"}
EDITABLE_DATE_FIELDS = {"alta", "baja", "nac"}


def _norm(s) -> str:
    fn = getattr(M, "_reg_norm", None)
    if fn:
        return fn(s)
    return " ".join(str(s or "").split()).strip().lower()


def map_columns(headers: list) -> dict:
    """{ключ: индекс колонки}. Сначала точные совпадения, потом по началу."""
    normed = [_norm(h) for h in headers]
    out, used = {}, set()
    for exact in (True, False):
        for key, variants in FIELDS:
            if key in out:
                continue
            for v in variants:
                v = _norm(v)
                for i, h in enumerate(normed):
                    if i in used or i == 0 or not h:
                        continue
                    hit = (h == v) if exact else h.startswith(v)
                    if hit:
                        out[key] = i
                        used.add(i)
                        break
                if key in out:
                    break
    # «contrato» не должен съесть «tipo de contrato»
    return out


_DATE_FORMATS = ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%y")


def parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    s = s.split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # серийный номер даты Google Sheets
    if re.fullmatch(r"\d{5}", s):
        try:
            return date.fromordinal(date(1899, 12, 30).toordinal() + int(s))
        except Exception:
            return None
    return None


_LOCAL_CANON = {"reina": "REINA", "francia": "FRANCIA", "fransia": "FRANCIA",
                "bakery": "BAKERY", "panaderia": "BAKERY", "panadería": "BAKERY",
                "boi": "BOI BOI"}


def canon_local(s: str) -> str:
    n = _norm(s)
    for k, v in _LOCAL_CANON.items():
        if k in n:
            return v
    return str(s or "").strip().upper()


def mask_iban(s: str) -> str:
    s = re.sub(r"\s+", "", str(s or ""))
    return f"•••• {s[-4:]}" if len(s) > 4 else s


def _hyperlink_url(formula: str) -> str:
    m = re.search(r'HYPERLINK\(\s*"([^"]+)"', str(formula or ""), re.I)
    return m.group(1) if m else ""


def _iso(d) -> str:
    return d.isoformat() if d else ""


def _date_parts_cols(headers: list, day_col):
    """В Registro дата разбита на три колонки: «Fecha de Alta» (число),
    «Mes», «Año». Ищем Mes/Año сразу справа от колонки с числом."""
    if day_col is None:
        return None, None
    mes = ano = None
    for j in range(day_col + 1, min(day_col + 4, len(headers))):
        h = _norm(headers[j])
        if mes is None and h.startswith("mes"):
            mes = j
        elif ano is None and (h.startswith("año") or h.startswith("ano")):
            ano = j
    return mes, ano


def _date_from_parts(row: list, day_col, mes_col, ano_col):
    """Дата из «число + Mes + Año»; если в колонке числа уже целая дата —
    берём её как есть."""
    def cell(i):
        return str(row[i]).strip() if i is not None and i < len(row) else ""
    day = cell(day_col)
    if not day:
        return None
    full = parse_date(day)
    if full and not re.fullmatch(r"\d{5}", day):
        return full
    try:
        d = int(float(day.replace(",", ".")))
        m = int(float(cell(mes_col).replace(",", ".")))
        y = int(float(cell(ano_col).replace(",", ".")))
        if y < 100:
            y += 2000
        return date(y, m, d)
    except Exception:
        return full


def build_records(values: list, formulas: list, header_row: int) -> dict:
    """Из сырых значений листа — список сотрудников для страницы."""
    headers = values[header_row - 1] if len(values) >= header_row else []
    cols = map_columns(headers)
    alta_mes, alta_ano = _date_parts_cols(headers, cols.get("alta"))
    baja_mes, baja_ano = _date_parts_cols(headers, cols.get("baja"))
    part_cols = {c for c in (alta_mes, alta_ano, baja_mes, baja_ano) if c is not None}
    records = []
    for r_i in range(header_row, len(values)):
        row = values[r_i]
        name = str(row[0]).strip() if row else ""
        if not name:
            continue

        def g(key):
            i = cols.get(key)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        alta = _date_from_parts(row, cols.get("alta"), alta_mes, alta_ano)
        baja = _date_from_parts(row, cols.get("baja"), baja_mes, baja_ano)
        tipo = g("tipo")
        fired = bool(baja) or any(w in _norm(tipo) for w in ("despedid", "baja"))
        link = ""
        ci = cols.get("contrato")
        if ci is not None and r_i < len(formulas) and ci < len(formulas[r_i]):
            link = _hyperlink_url(formulas[r_i][ci])

        extra = {}
        mapped = set(cols.values()) | {0} | part_cols
        for i, h in enumerate(headers):
            if i in mapped or not str(h).strip() or i >= len(row):
                continue
            v = str(row[i]).strip()
            if v:
                extra[str(h).strip()] = v

        records.append({
            "row": r_i + 1,
            "name": name,
            "local": canon_local(g("local")),
            "dep": g("dep"),
            "puesto": g("puesto"),
            "tipo": tipo,
            "estado": "baja" if fired else "activo",
            "alta": _iso(alta),
            "baja": _iso(baja),
            "periodo": g("periodo"),
            "motivo": g("motivo"),
            "sex": g("sex").upper()[:1],
            "horas": g("horas"),
            "horario": g("horario"),
            "nac": _iso(parse_date(g("nac"))),
            "nie": g("nie"),
            "tel": g("tel"),
            "email": g("email"),
            "iban": mask_iban(g("iban")),
            "domicilio": g("domicilio"),
            "contrato": link,
            "nss": g("nss"),
            "seguro": bool(g("nss")),
            "extra": extra,
        })
    return {
        "records": records,
        "columns_found": sorted(cols.keys()),
        "columns_missing": [k for k, _ in FIELDS if k not in cols],
        "header_row": header_row,
    }


def _in_progress() -> list:
    """Кандидаты из чек-листа HR, которые ещё не дошли до «Активен» —
    чтобы новые люди были видны в архиве сразу, до записи в Registro."""
    out = []
    try:
        final = M.HR_STAGES[-1]
        apps = M.applicants_rows()
        for i, r in enumerate(M.rows(M.HR_WS, force=True), start=2):
            stage = str(r.get("Статус", "")).strip()
            if not str(r.get("ФИО", "")).strip() or stage == final:
                continue
            app = {}
            try:
                app = apps[int(str(r.get("RowKey", "")).strip()) - 2]
            except Exception:
                pass
            inicio = parse_date(app.get("Fecha de inicio", ""))
            out.append({
                "row": -i, "name": str(r.get("ФИО", "")).strip(),
                "local": canon_local(r.get("Локаль", "")), "dep": "",
                "puesto": str(r.get("Должность", "")).strip(),
                "tipo": "", "estado": "proceso", "etapa": stage,
                "alta": "", "baja": "", "inicio": _iso(inicio),
                "periodo": "", "motivo": "", "sex": "",
                "horas": str(app.get("Número de horas bajo contrato", "")).strip(),
                "horario": "", "nac": _iso(parse_date(app.get("Fecha de nacimiento", ""))),
                "nie": str(app.get("NIE/TIE", "")).strip(),
                "tel": str(app.get("Teléfono", "")).strip(),
                "email": str(app.get("Correo electrónico", "")).strip(),
                "iban": mask_iban(app.get("IBAN", "")), "domicilio": str(app.get("Domicilio", "")).strip(),
                "contrato": "",
                "nss": str(app.get("Numero seguridad social", "")).strip(),
                "seguro": bool(str(app.get("Numero seguridad social", "")).strip()),
                "extra": {"Заявка от": str(r.get("Дата заявки", "")).strip()},
            })
    except Exception as e:
        log.warning("hr_web: чек-лист не прочитан: %s", e)
    return out


def _load_sync() -> dict:
    w = M.registro_ws()
    header_row = M.registro_header_row(w)
    values = w.get_all_values()
    try:
        formulas = w.get_all_values(value_render_option="FORMULA")
    except Exception as e:
        log.warning("hr_web: формулы не прочитались (%s) — без ссылок на контракты", e)
        formulas = []
    data = build_records(values, formulas, header_row)
    in_reg = {_norm(r["name"]) for r in data["records"]}
    data["records"] += [r for r in _in_progress() if _norm(r["name"]) not in in_reg]
    data["updated"] = M.now_local().strftime("%d.%m.%Y %H:%M")
    return data


async def load_data(force=False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    data = await asyncio.to_thread(_load_sync)
    _cache.update(ts=time.time(), data=data)
    return data


# ---------------- ПРАВКА КАРТОЧКИ С САЙТА ----------------

def _write_field_sync(row: int, field: str, value: str):
    """Пишет одно поле сотрудника в Registro. «alta»/«baja» — по трём
    колонкам (число/Mes/Año), «name» — колонка A напрямую, остальное —
    в свою колонку по map_columns. Пустое значение стирает ячейку."""
    w = M.registro_ws()
    header_row = M.registro_header_row(w)
    headers = w.row_values(header_row)
    cols = map_columns(headers)

    if field == "name":
        if not value:
            raise ValueError("имя не может быть пустым")
        M.sheets_write_retry(w.update_cell, row, 1, value)
        return

    if field in EDITABLE_DATE_FIELDS:
        i = cols.get(field)
        if i is None:
            raise ValueError(f"колонка «{field}» не найдена в Registro")
        if field == "nac":
            if not value:
                M.sheets_write_retry(w.update_cell, row, i + 1, "")
                return
            d = parse_date(value)
            if not d:
                raise ValueError(f"не разобрала дату «{value}»")
            M.sheets_write_retry(w.update_cell, row, i + 1, d.strftime("%d.%m.%Y"))
            return
        mes_c, ano_c = _date_parts_cols(headers, i)
        if not value:
            M.sheets_write_retry(w.update_cell, row, i + 1, "")
            if mes_c is not None:
                M.sheets_write_retry(w.update_cell, row, mes_c + 1, "")
            if ano_c is not None:
                M.sheets_write_retry(w.update_cell, row, ano_c + 1, "")
            return
        d = parse_date(value)
        if not d:
            raise ValueError(f"не разобрала дату «{value}»")
        M.sheets_write_retry(w.update_cell, row, i + 1, str(d.day))
        if mes_c is not None:
            M.sheets_write_retry(w.update_cell, row, mes_c + 1, str(d.month))
        if ano_c is not None:
            M.sheets_write_retry(w.update_cell, row, ano_c + 1, str(d.year))
        return

    if field not in EDITABLE_FIELDS:
        raise ValueError(f"поле «{field}» нельзя редактировать с сайта")
    i = cols.get(field)
    if i is None:
        raise ValueError(f"колонка «{field}» не найдена в Registro")
    if field == "local" and value:
        value = canon_local(value)
    M.sheets_write_retry(w.update_cell, row, i + 1, value)


# ---------------- ВЕБ-СЕРВЕР ----------------

COOKIE = "hrw"


def _page_html() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hr_web_page.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _build_app():
    from aiohttp import web

    def _set_cookie(resp, uid):
        resp.set_cookie(COOKIE, make_token(uid, COOKIE_TTL), max_age=COOKIE_TTL,
                        httponly=True, secure=True, samesite="None")

    def _uid_from(request):
        uid = check_token(request.cookies.get(COOKIE, "")) \
            or check_webapp(request.headers.get("X-TG-Init-Data", ""))
        return uid if uid and user_role(uid) else None

    async def page(request):
        # Страница сама по себе без данных — отдаём всегда; данные даёт
        # только /hr/api/data после проверки (cookie или Telegram Mini App).
        t = request.query.get("t")
        if t:
            uid = check_token(t)
            if not uid or not user_role(uid):
                return web.Response(text=_denied(), content_type="text/html", status=403)
            resp = web.HTTPFound("/hr")
            _set_cookie(resp, uid)
            raise resp
        return web.Response(text=_page_html(), content_type="text/html",
                            headers={"Cache-Control": "no-store",
                                     "X-Robots-Tag": "noindex"})

    async def api(request):
        uid = _uid_from(request)
        if not uid:
            return web.json_response({"error": "auth"}, status=401)
        role = user_role(uid)
        if role == "manager":
            # Управляющему — только его имя и вкладка заявок, без личных данных.
            data = {"records": [], "role": role, "me": user_name(uid),
                    "updated": M.now_local().strftime("%d.%m.%Y %H:%M"),
                    "columns_missing": []}
        else:
            try:
                data = await load_data(force=request.query.get("force") == "1")
            except Exception as e:
                log.error("hr_web: ошибка чтения Registro: %s", e, exc_info=True)
                return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
            data = dict(data, role=role, me=user_name(uid))
        resp = web.json_response(data, headers={"Cache-Control": "no-store"})
        if not check_token(request.cookies.get(COOKIE, "")):
            _set_cookie(resp, uid)   # вошли из Telegram — запомним и для браузера
        return resp

    async def health(request):
        return web.Response(text="ok")

    async def update_employee(request):
        uid = _uid_from(request)
        if not uid or user_role(uid) != "patron":
            return web.json_response({"error": "auth"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "битый запрос"}, status=400)
        try:
            row = int(body.get("row") or 0)
        except Exception:
            row = 0
        fields = body.get("fields") or {}
        if row <= 0:
            return web.json_response(
                {"error": "эта запись ещё не в Registro (в оформлении) — редактирование "
                          "с сайта появится, когда карточка сотрудника создастся"}, status=400)
        if not isinstance(fields, dict) or not fields:
            return web.json_response({"error": "нечего сохранять"}, status=400)
        changed, errors = [], []
        for field, value in fields.items():
            try:
                await asyncio.to_thread(_write_field_sync, row, str(field), str(value).strip())
                changed.append(field)
            except Exception as e:
                log.error("hr_web: правка не сохранилась (row=%s field=%s): %s", row, field, e)
                errors.append(f"{field}: {e}")
        if changed:
            _cache["ts"] = 0   # следующее /hr/api/data перечитает Registro заново
        return web.json_response({"ok": not errors, "changed": changed, "errors": errors})

    async def logo(request):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sl_logo.svg")
        if not os.path.exists(path):
            return web.Response(status=404)
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/hr", page)
    app.router.add_get("/hr/logo.svg", logo)
    app.router.add_get("/hr/api/data", api)
    app.router.add_post("/hr/api/employee/update", update_employee)
    app["uid_from"] = _uid_from
    for add in EXTRA_ROUTES:
        try:
            add(app)
        except Exception as e:
            log.error("hr_web: маршрут модуля не добавлен: %s", e, exc_info=True)
    return app


def _denied() -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Архив сотрудников</title>"
            "<body style='font:16px system-ui;padding:40px 16px;max-width:520px;margin:auto'>"
            "<h2>Ссылка устарела</h2><p>Откройте архив из бота: HR → «🌐 Архив сотрудников» "
            "или команда <b>/archivo</b>.</p></body>")


async def _start_web(*args, **kwargs):
    global _runner
    if _runner is not None:
        return
    from aiohttp import web
    port = int(os.environ.get("PORT", "8080"))
    _runner = web.AppRunner(_build_app())
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    log.info("hr_web: веб-архив слушает порт %s", port)


def web_url() -> str:
    return os.environ.get("HR_WEB_URL", "").rstrip("/")


# ---------------- ТЕЛЕГРАМ ----------------

def setup(dp, main_module):
    global M
    M = main_module
    from aiogram import F
    from aiogram.filters import Command
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

    dp.startup.register(_start_web)

    # Кнопку меню не трогаем — там остаются команды бота (решение 19.09).

    async def send_link(chat_id: int, uid: int):
        base = web_url()
        if not base:
            await M.bot.send_message(
                chat_id,
                "⚠️ Веб-архив ещё не настроен: в Railway нужна переменная "
                "<code>HR_WEB_URL</code> — публичный адрес сервиса.")
            return
        link = f"{base}/hr?t={make_token(uid)}"
        rows = [[InlineKeyboardButton(text="🌐 Открыть архив сотрудников",
                                      web_app=WebAppInfo(url=f"{base}/hr"))],
                [InlineKeyboardButton(text="💻 Открыть в браузере", url=link)]]
        if hasattr(M, "nav_row"):
            rows.append(M.nav_row())
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await M.bot.send_message(
            chat_id,
            "🌐 <b>Архив сотрудников</b>\n\n"
            "• Внутри Telegram — кнопка ниже, открывается сразу.\n"
            "• В браузере компьютера — «💻 Открыть в браузере» один раз, дальше адрес "
            "можно сохранить в закладки: вход помнится 90 дней.",
            reply_markup=kb)

    @dp.message(Command("archivo"))
    async def cmd_archivo(m):
        if m.chat.type != "private" or not user_role(m.from_user.id):
            return
        await send_link(m.chat.id, m.from_user.id)

    @dp.callback_query(F.data == "hrweb")
    async def cb_hrweb(c):
        if not user_role(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        await send_link(c.from_user.id, c.from_user.id)

    log.info("%s подключён", VERSION)
