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

VERSION = "hr_web 1.0 · 18.09.2026"

M = None          # модуль bot.py — берём оттуда таблицы, роли, bot
_runner = None
TOKEN_TTL = 24 * 3600
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


def _is_patron(uid: int) -> bool:
    try:
        return M.is_patron(M.get_user(uid))
    except Exception as e:
        log.error("hr_web: не удалось проверить роль %s: %s", uid, e)
        return False


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
]


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


def build_records(values: list, formulas: list, header_row: int) -> dict:
    """Из сырых значений листа — список сотрудников для страницы."""
    headers = values[header_row - 1] if len(values) >= header_row else []
    cols = map_columns(headers)
    records = []
    for r_i in range(header_row, len(values)):
        row = values[r_i]
        name = str(row[0]).strip() if row else ""
        if not name:
            continue

        def g(key):
            i = cols.get(key)
            return str(row[i]).strip() if i is not None and i < len(row) else ""

        alta, baja = parse_date(g("alta")), parse_date(g("baja"))
        tipo = g("tipo")
        fired = bool(baja) or any(w in _norm(tipo) for w in ("despedid", "baja"))
        link = ""
        ci = cols.get("contrato")
        if ci is not None and r_i < len(formulas) and ci < len(formulas[r_i]):
            link = _hyperlink_url(formulas[r_i][ci])

        extra = {}
        mapped = set(cols.values()) | {0}
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
            "extra": extra,
        })
    return {
        "records": records,
        "columns_found": sorted(cols.keys()),
        "columns_missing": [k for k, _ in FIELDS if k not in cols],
        "header_row": header_row,
    }


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
    data["updated"] = M.now_local().strftime("%d.%m.%Y %H:%M")
    return data


async def load_data(force=False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    data = await asyncio.to_thread(_load_sync)
    _cache.update(ts=time.time(), data=data)
    return data


# ---------------- ВЕБ-СЕРВЕР ----------------

COOKIE = "hrw"


def _page_html() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hr_web_page.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _build_app():
    from aiohttp import web

    def _uid_from(request):
        uid = check_token(request.cookies.get(COOKIE, ""))
        return uid if uid and _is_patron(uid) else None

    async def page(request):
        t = request.query.get("t")
        if t:
            uid = check_token(t)
            if not uid or not _is_patron(uid):
                return web.Response(text=_denied(), content_type="text/html", status=403)
            resp = web.HTTPFound("/hr")
            resp.set_cookie(COOKIE, t, max_age=TOKEN_TTL, httponly=True,
                            secure=True, samesite="Lax")
            raise resp
        if not _uid_from(request):
            return web.Response(text=_denied(), content_type="text/html", status=403)
        return web.Response(text=_page_html(), content_type="text/html",
                            headers={"Cache-Control": "no-store",
                                     "X-Robots-Tag": "noindex"})

    async def api(request):
        if not _uid_from(request):
            return web.json_response({"error": "auth"}, status=401)
        try:
            data = await load_data(force=request.query.get("force") == "1")
        except Exception as e:
            log.error("hr_web: ошибка чтения Registro: %s", e, exc_info=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def health(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/hr", page)
    app.router.add_get("/hr/api/data", api)
    return app


def _denied() -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Архив сотрудников</title>"
            "<body style='font:16px system-ui;padding:40px 16px;max-width:520px;margin:auto'>"
            "<h2>Ссылка устарела</h2><p>Откройте бота и отправьте <b>/archivo</b> — "
            "придёт новая ссылка на 24 часа.</p></body>")


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
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    dp.startup.register(_start_web)

    async def send_link(chat_id: int, uid: int):
        base = web_url()
        if not base:
            await M.bot.send_message(
                chat_id,
                "⚠️ Веб-архив ещё не настроен: в Railway нужна переменная "
                "<code>HR_WEB_URL</code> — публичный адрес сервиса.")
            return
        link = f"{base}/hr?t={make_token(uid)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌐 Открыть архив сотрудников", url=link)]])
        await M.bot.send_message(
            chat_id,
            "🌐 <b>Архив сотрудников</b>\n\nЛичная ссылка, действует 24 часа. "
            "Не пересылайте её — по ней открываются данные сотрудников.",
            reply_markup=kb)

    @dp.message(Command("archivo"))
    async def cmd_archivo(m):
        if m.chat.type != "private" or not _is_patron(m.from_user.id):
            return
        await send_link(m.chat.id, m.from_user.id)

    @dp.callback_query(F.data == "hrweb")
    async def cb_hrweb(c):
        if not _is_patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        await send_link(c.from_user.id, c.from_user.id)

    log.info("%s подключён", VERSION)
