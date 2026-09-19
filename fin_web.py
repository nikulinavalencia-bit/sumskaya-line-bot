# -*- coding: utf-8 -*-
"""
🌐 fin_web.py — страница статуса платежей.

Кто загрузил документ на оплату — видит его и его статус. Фин. директор и
патрон видят все документы по всем локалям, суммы и итоги.

Отдельный файл финансового блока. Подключается сам из fin_block.py, в bot.py
ничего добавлять не нужно.

Веб-сервер общий с архивом сотрудников (hr_web): у Railway один открытый порт,
поэтому свои маршруты мы добавляем в уже существующее приложение. Если hr_web
в сборке нет — поднимаем сервер сами.

Маршруты:
    /pagos            страница
    /pagos/api/data   данные (после проверки подписи)
    /pagos/file/<id>  сам документ или хустификанте, потоком из Telegram

Переменные Railway:
    HR_WEB_URL — публичный адрес сервиса (тот же, что у архива сотрудников)
"""

import os
import re
import hmac
import time
import json
import base64
import hashlib
import logging

log = logging.getLogger("fin_web")

VERSION = "fin_web 1.0"

core = None                      # модуль bot.py
_runner = None
TOKEN_TTL = 24 * 3600            # ссылка из бота
COOKIE_TTL = 90 * 24 * 3600      # вход в браузере помнится
COOKIE = "finw"
CACHE_TTL = 30
_cache = {"ts": 0, "data": None}


# ---------------- ПОДПИСЬ ССЫЛКИ ----------------

def _secret() -> bytes:
    base = os.environ.get("HR_WEB_SECRET") or getattr(core, "BOT_TOKEN", "") or "x"
    return hashlib.sha256(("fin_web:" + base).encode()).digest()


def make_token(uid: int, ttl: int = TOKEN_TTL) -> str:
    payload = f"{uid}:{int(time.time()) + ttl}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def check_token(token: str):
    if not token:
        return None
    try:
        pad = "=" * (-len(token) % 4)
        uid, exp, sig = base64.urlsafe_b64decode(token + pad).decode().split(":")
        good = hmac.new(_secret(), f"{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


def check_webapp(init_data: str):
    """Вход из Telegram Mini App — подпись initData ключом бота."""
    if not init_data:
        return None
    try:
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", str(getattr(core, "BOT_TOKEN", "")).encode(),
                          hashlib.sha256).digest()
        good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, good):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 24 * 3600:
            return None
        return int(json.loads(pairs.get("user", "{}")).get("id"))
    except Exception:
        return None


def web_url() -> str:
    return os.environ.get("HR_WEB_URL", "").rstrip("/")


# ---------------- ДАННЫЕ ----------------

def _fin():
    import fin_block
    return fin_block


def _viewer(uid: int) -> dict:
    """Кто смотрит: сотрудник видит своё, фин. дир и патрон — всё."""
    try:
        u = core.get_user(uid)
    except Exception:
        u = None
    if not u or str(u.get("Статус", "")).strip() != "active":
        return {}
    full = False
    try:
        full = _fin().is_findir(u, uid)
    except Exception:
        pass
    return {"uid": uid, "name": str(u.get("Имя", "")).strip(), "full": full}


def _rows_sync() -> list:
    fin = _fin()
    out = []
    for _, r in fin.facturas(force=True):
        loc = str(r.get("Локаль", "")).strip()
        l = core.LOCALES.get(loc, {})
        total = fin.parse_amount(r.get("Total"))
        out.append({
            "id": str(r.get("ID", "")).strip(),
            "fecha": str(r.get("Дата", "")).strip(),
            "hora": str(r.get("Время", "")).strip(),
            "local": f"{l.get('emoji', '')} {l.get('name', loc)}".strip(),
            "local_code": loc,
            "autor": str(r.get("Автор", "")).strip(),
            "autor_id": str(r.get("AuthorID", "")).strip(),
            "proveedor": str(r.get("Поставщик", "")).strip(),
            "numero": str(r.get("Номер", "")).strip(),
            "fecha_factura": str(r.get("Дата фактуры", "")).strip(),
            "total": total,
            "estado": str(r.get("Статус", "")).strip(),
            "justificante": str(r.get("Хустификанте", "")).strip().lower() == "да",
            "pagado": str(r.get("Дата оплаты", "")).strip(),
            "has_doc": bool(str(r.get("FileID", "")).strip()),
            "has_just": bool(str(r.get("ХустификантеFileID", "")).strip()),
        })
    out.sort(key=lambda x: (x["fecha"].split(".")[::-1], x["hora"]), reverse=True)
    return out


async def load_rows(force=False) -> list:
    import asyncio
    if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    data = await asyncio.to_thread(_rows_sync)
    _cache.update(ts=time.time(), data=data)
    return data


def _page_html() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fin_web_page.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------- МАРШРУТЫ ----------------

def add_routes(app):
    """Добавляет наши маршруты в готовое приложение aiohttp."""
    from aiohttp import web

    def _set_cookie(resp, uid):
        resp.set_cookie(COOKIE, make_token(uid, COOKIE_TTL), max_age=COOKIE_TTL,
                        httponly=True, secure=True, samesite="None")

    def _uid_from(request):
        uid = check_token(request.cookies.get(COOKIE, "")) \
            or check_webapp(request.headers.get("X-TG-Init-Data", ""))
        return uid

    async def page(request):
        t = request.query.get("t")
        if t:
            uid = check_token(t)
            if not uid or not _viewer(uid):
                return web.Response(text=_denied(), content_type="text/html", status=403)
            resp = web.HTTPFound("/pagos")
            _set_cookie(resp, uid)
            raise resp
        return web.Response(text=_page_html(), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    async def api(request):
        uid = _uid_from(request)
        v = _viewer(uid) if uid else {}
        if not v:
            return web.json_response({"error": "auth"}, status=401)
        try:
            rows = await load_rows(force=request.query.get("force") == "1")
        except Exception as ex:
            log.error("fin_web: не прочитал таблицу: %s", ex, exc_info=True)
            return web.json_response({"error": f"{type(ex).__name__}: {ex}"}, status=500)
        if not v["full"]:
            rows = [r for r in rows if r["autor_id"] == str(uid)]
        payload = {
            "rows": rows,
            "viewer": {"name": v["name"], "full": v["full"]},
            "updated": core.now_local().strftime("%d.%m.%Y %H:%M"),
        }
        resp = web.json_response(payload, headers={"Cache-Control": "no-store"})
        if not check_token(request.cookies.get(COOKIE, "")):
            _set_cookie(resp, uid)
        return resp

    async def file(request):
        """Отдаём сам документ: тянем из Telegram и сразу отдаём браузеру."""
        uid = _uid_from(request)
        v = _viewer(uid) if uid else {}
        if not v:
            return web.Response(text="auth", status=401)
        fid = request.match_info.get("fid", "")
        kind = request.query.get("k", "doc")
        fin = _fin()
        _, r = fin.find_factura(fid)
        if not r:
            return web.Response(text="not found", status=404)
        if not v["full"] and str(r.get("AuthorID", "")).strip() != str(uid):
            return web.Response(text="forbidden", status=403)
        key = "ХустификантеFileID" if kind == "just" else "FileID"
        file_id = str(r.get(key, "")).strip()
        if not file_id:
            return web.Response(text="no file", status=404)
        try:
            buf = await core.bot.download(file_id)
            data = buf.read()
        except Exception as ex:
            log.warning("fin_web: файл %s не скачался: %s", fid, ex)
            return web.Response(text="file unavailable", status=502)
        ctype = "application/pdf" if data[:4] == b"%PDF" else "image/jpeg"
        name = f"{kind}-{fid}." + ("pdf" if ctype.endswith("pdf") else "jpg")
        return web.Response(body=data, content_type=ctype,
                            headers={"Content-Disposition": f'inline; filename="{name}"',
                                     "Cache-Control": "no-store"})

    app.router.add_get("/pagos", page)
    app.router.add_get("/pagos/api/data", api)
    app.router.add_get("/pagos/file/{fid}", file)
    log.info("%s: маршруты /pagos добавлены", VERSION)


def _denied() -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Статус платежей</title>"
            "<body style='font:16px system-ui;padding:40px 16px;max-width:520px;margin:auto'>"
            "<h2>Ссылка устарела</h2><p>Откройте страницу заново из бота: "
            "💶 Финансы → <b>Статус платежей</b>.</p></body>")


# ---------------- ЗАПУСК ----------------

async def _start_own_web(*args, **kwargs):
    """Если архива сотрудников в сборке нет — поднимаем сервер сами."""
    global _runner
    if _runner is not None:
        return
    from aiohttp import web
    app = web.Application()

    async def health(request):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    add_routes(app)
    port = int(os.environ.get("PORT", "8080"))
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    log.info("fin_web: свой веб-сервер на порту %s", port)


def _attach_to_hr_web() -> bool:
    """Встраиваемся в приложение архива сотрудников, не меняя его файл."""
    try:
        import hr_web
    except Exception:
        return False
    orig = getattr(hr_web, "_build_app", None)
    if not callable(orig) or getattr(orig, "_fin_patched", False):
        return bool(orig)

    def build_with_fin():
        app = orig()
        try:
            add_routes(app)
        except Exception as ex:
            log.error("fin_web: маршруты не добавились: %s", ex, exc_info=True)
        return app

    build_with_fin._fin_patched = True
    hr_web._build_app = build_with_fin
    log.info("fin_web: подключусь к веб-серверу архива сотрудников")
    return True


def setup(core_module):
    global core
    core = core_module
    if not _attach_to_hr_web():
        # своего сервера ещё нет — поднимем при старте бота
        try:
            import sys
            dp = getattr(sys.modules.get("fin_block"), "_dp", None)
            if dp is not None:
                dp.startup.register(_start_own_web)
        except Exception as ex:
            log.warning("fin_web: не смог зарегистрировать старт сервера: %s", ex)
    log.info("%s подключён", VERSION)


# ---------------- ССЫЛКА ИЗ БОТА ----------------

async def send_link(chat_id: int, uid: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    base = web_url()
    if not base:
        await core.bot.send_message(
            chat_id,
            "⚠️ Страница статуса ещё не настроена: в Railway нужна переменная "
            "<code>HR_WEB_URL</code> — публичный адрес сервиса.")
        return
    v = _viewer(uid)
    link = f"{base}/pagos?t={make_token(uid)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть статус платежей",
                              web_app=WebAppInfo(url=f"{base}/pagos"))],
        [InlineKeyboardButton(text="💻 Открыть в браузере", url=link)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="bk")],
    ])
    what = ("Видно всё: все локали, суммы и итоги." if v.get("full")
            else "Видно то, что загрузил ты, и статус оплаты по каждому документу.")
    await core.bot.send_message(
        chat_id,
        f"🌐 <b>Статус платежей</b>\n\n{what}\n\n"
        f"Кнопка ниже открывает страницу прямо в Telegram. Ссылка «в браузере» "
        f"помнит вход 90 дней — можно сохранить в закладки.",
        reply_markup=kb)
