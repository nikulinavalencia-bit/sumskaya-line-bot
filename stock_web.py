# -*- coding: utf-8 -*-
"""
🌐 stock_web.py — Ritmo OPS: веб-страница приходных накладных товарного блока.

Список накладных по локалям со статусами, карточка накладной со строками и
сопоставлением с Syrve, выгрузка, динамика цен, остатки на складах.

Подключается сам из stock_block.py. Веб-сервер общий с архивом сотрудников
(hr_web) — у Railway один порт, поэтому маршруты добавляются в его приложение.

Маршруты:
    /almacen                      страница
    /almacen/api/data             список накладных
    /almacen/api/doc/<id>         одна накладная со строками
    /almacen/api/ref              номенклатура, склады, поставщики Syrve
    /almacen/api/doc/<id>/save    сохранить сопоставление (POST)
    /almacen/api/doc/<id>/upload  выгрузить в Syrve (POST)
    /almacen/api/doc/<id>/status  не для склада / удалить / вернуть (POST)
    /almacen/api/doc/<id>/reread  прочитать заново (POST)
    /almacen/api/upload_ready     выгрузить все готовые (POST)
    /almacen/api/upload           загрузить накладные с сайта (POST, файлы)
    /almacen/api/prices           динамика цен
    /almacen/api/balances         остатки Syrve
    /almacen/file/<id>            сам документ
"""

import os
import hmac
import time
import json
import base64
import asyncio
import hashlib
import logging
from datetime import datetime

log = logging.getLogger("stock_web")

VERSION = "stock_web 1.0 · 19.09.2026"

core = None
_runner = None
TOKEN_TTL = 24 * 3600
COOKIE_TTL = 90 * 24 * 3600
COOKIE = "stkw"
MAX_UPLOAD = 40 * 1024 * 1024     # загрузка накладных с сайта, всего за раз


def sb():
    import stock_block
    return stock_block


# ---------------- ВХОД ----------------

def _secret() -> bytes:
    base = os.environ.get("HR_WEB_SECRET") or getattr(core, "BOT_TOKEN", "") or "x"
    return hashlib.sha256(("stock_web:" + base).encode()).digest()


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

def _row_short(r: dict) -> dict:
    s = sb()
    lines = s.doc_lines(r)
    stores = sorted({l.get("store") for l in lines if l.get("store") and not l.get("skip")}
                    | ({str(r.get("Склад"))} if r.get("Склад") else set()))
    base = s.f2(r.get("База"))
    lsum = s.f2(r.get("Сумма строк"))
    tol = max(s.SUM_TOLERANCE, base * 0.005)
    return {
        "id": str(r.get("ID", "")),
        "fecha": str(r.get("Дата", "")), "hora": str(r.get("Время", "")),
        "loc": str(r.get("Локаль", "")).strip(),
        "autor": str(r.get("Автор", "")),
        "tipo": str(r.get("Тип", "")),
        "proveedor": str(r.get("Поставщик", "")),
        "nif": str(r.get("NIF", "")),
        "numero": str(r.get("Номер", "")),
        "fecha_doc": str(r.get("Дата документа", "")),
        "base": base, "iva": s.f2(r.get("IVA")), "total": s.f2(r.get("Total")),
        "lsum": lsum, "sum_ok": bool(base) and abs(lsum - base) <= tol,
        "n": int(s.f2(r.get("Строк"))), "m": int(s.f2(r.get("Сопоставлено"))),
        "estado": str(r.get("Статус", "")),
        "store": str(r.get("Склад", "")), "stores": stores,
        "supplier": str(r.get("Поставщик Syrve", "")),
        "syrve_num": str(r.get("Syrve №", "")),
        "error": str(r.get("Ошибка", "")),
        "uploaded": str(r.get("Выгружено", "")),
        "files": len([p for p in str(r.get("Файлы", "")).split(",") if p.strip()]),
        "ftypes": [p.partition("|")[2] for p in str(r.get("Файлы", "")).split(",") if p.strip()],
    }


def _list_sync(loc: str) -> list:
    out = []
    for _, r in sb().docs(force=True):
        if loc and str(r.get("Локаль", "")).strip() != loc:
            continue
        if str(r.get("Статус", "")) == sb().ST_DEL:
            continue
        out.append(_row_short(r))
    out.sort(key=lambda x: (x["fecha"].split(".")[::-1], x["hora"], x["id"]), reverse=True)
    return out


def _doc_sync(doc_id: str) -> dict:
    _, r = sb().find_doc(doc_id)
    if not r:
        return {}
    d = _row_short(r)
    d["lines"] = sb().doc_lines(r)
    d["need"] = [] if d["estado"] in (sb().ST_DONE, sb().ST_SKIP, sb().ST_DUP) else \
        sb().evaluate(d["loc"], r, d["lines"])["_need"]
    return d


def _ref_sync(loc: str) -> dict:
    s = sb()
    if not s.syrve_on(loc):
        return {"on": False, "products": [], "stores": [], "suppliers": []}
    api = s.syrve()
    return {"on": True, "products": api.products(loc), "stores": api.stores(loc),
            "suppliers": api.suppliers(loc),
            "default_store": s.setting(f"store:{loc}")}


def _prices_sync(loc: str) -> list:
    s = sb()
    hist = {}
    for _, r in s.docs(force=True):
        if loc and str(r.get("Локаль", "")).strip() != loc:
            continue
        if str(r.get("Статус", "")) in (s.ST_DEL, s.ST_DUP, s.ST_SKIP, s.ST_FAIL, s.ST_OCR):
            continue
        d = str(r.get("Дата документа") or r.get("Дата") or "")
        try:
            dt = datetime.strptime(d, "%d.%m.%Y").date()
        except ValueError:
            continue
        skey = s.sup_key(r.get("NIF"), r.get("Поставщик"))
        for l in s.doc_lines(r):
            if l.get("skip") or l.get("cargo"):
                continue
            q, sm = s.f2(l.get("qty")), s.f2(l.get("sum"))
            if q <= 0 or sm <= 0:
                continue
            k = (skey, s.key_name(l.get("name")))
            hist.setdefault(k, []).append({
                "date": dt.isoformat(), "price": round(sm / q, 4), "unit": l.get("unit", ""),
                "name": l.get("name", ""), "pname": l.get("pname", ""),
                "supplier": str(r.get("Поставщик", "")), "doc": str(r.get("ID", ""))})
    out = []
    for k, h in hist.items():
        h.sort(key=lambda x: x["date"])
        last = h[-1]
        prev = next((x for x in reversed(h[:-1]) if x["price"] != last["price"]), None) \
            if len(h) > 1 else None
        ch = round((last["price"] - prev["price"]) / prev["price"] * 100, 1) \
            if prev and prev["price"] else 0.0
        out.append({"supplier": last["supplier"], "name": last["name"],
                    "pname": last["pname"], "unit": last["unit"],
                    "price": last["price"], "date": last["date"],
                    "prev": prev["price"] if prev else None,
                    "prev_date": prev["date"] if prev else None,
                    "change": ch, "count": len(h),
                    "history": [{"d": x["date"], "p": x["price"]} for x in h[-8:]]})
    out.sort(key=lambda x: (-abs(x["change"]), x["supplier"], x["name"]))
    return out


def _balances_sync(loc: str) -> dict:
    s = sb()
    if not s.syrve_on(loc):
        return {"on": False, "rows": []}
    api = s.syrve()
    prods = {p["id"]: p for p in api.products(loc)}
    stores = {x["id"]: x for x in api.stores(loc)}
    rows = []
    for b in api.balances(loc):
        if abs(b["amount"]) < 1e-6 and abs(b["sum"]) < 0.005:
            continue
        p = prods.get(b["product"])
        if not p:
            continue
        st = stores.get(b["store"], {})
        rows.append({"store": st.get("name", "?"), "dep": st.get("dep", ""),
                     "product": p["name"], "unit": p.get("unit", ""),
                     "amount": round(b["amount"], 3), "sum": round(b["sum"], 2)})
    rows.sort(key=lambda x: (x["dep"], x["store"], x["product"].lower()))
    return {"on": True, "rows": rows,
            "at": core.now_local().strftime("%d.%m.%Y %H:%M")}


def _viewer(uid: int) -> dict:
    if not uid or not sb().can_use(uid):
        return {}
    u = core.get_user(uid) or {}
    return {"uid": uid, "name": str(u.get("Имя", "")).strip(),
            "patron": core.is_patron(u)}


def _page_html() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_web_page.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------- МАРШРУТЫ ----------------

def add_routes(app):
    from aiohttp import web

    def _set_cookie(resp, uid):
        resp.set_cookie(COOKIE, make_token(uid, COOKIE_TTL), max_age=COOKIE_TTL,
                        httponly=True, secure=True, samesite="None")

    def _uid(request):
        return check_token(request.cookies.get(COOKIE, "")) \
            or check_webapp(request.headers.get("X-TG-Init-Data", ""))

    def _json(data, status=200):
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"},
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def guarded(fn):
        async def inner(request):
            uid = _uid(request)
            v = await asyncio.to_thread(_viewer, uid)
            if not v:
                return _json({"error": "auth"}, 401)
            try:
                resp = await fn(request, v)
            except Exception as ex:
                log.error("stock_web %s: %s", request.path, ex, exc_info=True)
                return _json({"error": f"{type(ex).__name__}: {ex}"}, 500)
            if not check_token(request.cookies.get(COOKIE, "")):
                _set_cookie(resp, uid)
            return resp
        return inner

    async def page(request):
        t = request.query.get("t")
        if t:
            uid = check_token(t)
            if not uid or not await asyncio.to_thread(_viewer, uid):
                return web.Response(text=_denied(), content_type="text/html", status=403)
            resp = web.HTTPFound("/almacen")
            _set_cookie(resp, uid)
            raise resp
        return web.Response(text=_page_html(), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    async def data(request, v):
        loc = request.query.get("loc", "")
        rows = await asyncio.to_thread(_list_sync, loc)
        locs = [{"code": c, "name": l["name"], "emoji": l["emoji"],
                 "syrve": sb().syrve_on(c)} for c, l in core.LOCALES.items()
                if c in sb().locales_on()]
        return _json({"rows": rows, "viewer": v, "locales": locs,
                      "auto": sb().AUTO_UPLOAD, "draft": sb().DOC_STATUS == "NEW",
                      "updated": core.now_local().strftime("%d.%m.%Y %H:%M")})

    async def doc(request, v):
        d = await asyncio.to_thread(_doc_sync, request.match_info["id"])
        return _json(d) if d else _json({"error": "not found"}, 404)

    async def ref(request, v):
        return _json(await asyncio.to_thread(_ref_sync, request.query.get("loc", "")))

    async def save(request, v):
        payload = await request.json()
        res = await sb().save_edit(request.match_info["id"], payload, v["uid"])
        if res.get("ok") and payload.get("upload"):
            _, r = await asyncio.to_thread(sb().find_doc, request.match_info["id"])
            if r and str(r.get("Статус")) == sb().ST_READY:
                up = await sb().upload(request.match_info["id"], v["name"])
                res["upload"] = up
        return _json(res)

    async def upload(request, v):
        return _json(await sb().upload(request.match_info["id"], v["name"]))

    async def status(request, v):
        body = await request.json()
        st = {"skip": sb().ST_SKIP, "del": sb().ST_DEL,
              "restore": sb().ST_EDIT}.get(body.get("status"))
        if not st:
            return _json({"ok": False, "error": "bad status"}, 400)
        res = await sb().set_status(request.match_info["id"], st, v["uid"])
        if res.get("ok") and st == sb().ST_EDIT:
            _, r = await asyncio.to_thread(sb().find_doc, request.match_info["id"])
            if r:
                await sb().finish(str(r.get("ID")), {}, sb().doc_lines(r),
                                  str(r.get("Локаль", "")).strip())
        return _json(res)

    async def reread(request, v):
        return _json(await sb().reprocess(request.match_info["id"], v["uid"]))

    async def upload_ready(request, v):
        body = await request.json()
        loc = body.get("loc", "")
        ids = [x["id"] for x in await asyncio.to_thread(_list_sync, loc)
               if x["estado"] == sb().ST_READY]
        ok, errs = 0, []
        for i in ids:
            res = await sb().upload(i, v["name"])
            if res.get("ok"):
                ok += 1
            else:
                errs.append({"id": i, "error": res.get("error")})
        return _json({"ok": True, "uploaded": ok, "errors": errs})

    async def upload_files(request, v):
        """Загрузка накладных прямо с сайта. Файл отправляется ботом тому, кто
        загрузил (так у него появляется file_id в Telegram и квитанция в чате),
        дальше — та же обработка, что у фактур из групп."""
        from aiogram.types import BufferedInputFile
        form = await request.post()
        loc = str(form.get("loc", "")).strip()
        if loc not in sb().locales_on():
            return _json({"ok": False, "error": "эта локаль не подключена к Ritmo OPS"}, 400)
        one_doc = str(form.get("one", "")) == "1"
        files = [f for f in form.getall("files", []) if hasattr(f, "file")]
        if not files:
            return _json({"ok": False, "error": "нет файлов"}, 400)
        items = []
        for f in files[:20]:
            data = f.file.read()
            if not data:
                continue
            name = f.filename or "factura.jpg"
            mime = f.content_type or ("application/pdf" if name.lower().endswith(".pdf") else "image/jpeg")
            msg = await core.bot.send_document(
                v["uid"], BufferedInputFile(data, filename=name),
                caption=f"📎 Ritmo OPS: {name} загружен с сайта ({core.LOCALES.get(loc, {}).get('name', loc)})")
            items.append({"loc": loc, "author": v["name"] + " (сайт)", "chat_id": msg.chat.id,
                          "msg_id": msg.message_id, "file_id": msg.document.file_id,
                          "mime": mime, "text": ""})
        if not items:
            return _json({"ok": False, "error": "пустые файлы"}, 400)
        groups = [items] if one_doc else [[it] for it in items]

        async def run_all():
            for g_ in groups:
                try:
                    await sb().process(g_, notify=False)
                except Exception as ex:
                    log.error("stock_web: загрузка с сайта: %s", ex, exc_info=True)
        asyncio.create_task(run_all())
        return _json({"ok": True, "docs": len(groups), "files": len(items)})

    async def rematch(request, v):
        n = await sb().rematch()
        return _json({"ok": True, "n": n})

    async def prices(request, v):
        return _json({"rows": await asyncio.to_thread(_prices_sync, request.query.get("loc", ""))})

    async def balances(request, v):
        return _json(await asyncio.to_thread(_balances_sync, request.query.get("loc", "")))

    async def file(request, v):
        _, r = await asyncio.to_thread(sb().find_doc, request.match_info["id"])
        if not r:
            return web.Response(text="not found", status=404)
        parts = [p for p in str(r.get("Файлы", "")).split(",") if p.strip()]
        try:
            i = int(request.query.get("i", "0"))
            fid = parts[i].partition("|")[0]
        except (ValueError, IndexError):
            return web.Response(text="no file", status=404)
        try:
            buf = await core.bot.download(fid)
            body = buf.read()
        except Exception as ex:
            return web.Response(text=f"file unavailable: {ex}", status=502)
        ctype = "application/pdf" if body[:4] == b"%PDF" else \
            "image/png" if body[:4] == b"\x89PNG" else "image/jpeg"
        return web.Response(body=body, content_type=ctype,
                            headers={"Cache-Control": "private, max-age=3600"})

    g = guarded
    app.router.add_get("/almacen", page)

    async def ritmo(request):
        raise web.HTTPFound("/almacen" + (("?" + request.query_string) if request.query_string else ""))
    app.router.add_get("/ritmo", ritmo)
    app.router.add_get("/almacen/api/data", g(data))
    app.router.add_get("/almacen/api/doc/{id}", g(doc))
    app.router.add_get("/almacen/api/ref", g(ref))
    app.router.add_post("/almacen/api/doc/{id}/save", g(save))
    app.router.add_post("/almacen/api/doc/{id}/upload", g(upload))
    app.router.add_post("/almacen/api/doc/{id}/status", g(status))
    app.router.add_post("/almacen/api/doc/{id}/reread", g(reread))
    app.router.add_post("/almacen/api/upload_ready", g(upload_ready))
    app.router.add_post("/almacen/api/rematch", g(rematch))
    app.router.add_post("/almacen/api/upload", g(upload_files))
    app.router.add_get("/almacen/api/prices", g(prices))
    app.router.add_get("/almacen/api/balances", g(balances))
    app.router.add_get("/almacen/file/{id}", g(file))
    log.info("%s: маршруты /almacen добавлены", VERSION)


def _denied() -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Ritmo OPS</title>"
            "<body style='font:16px system-ui;padding:40px 16px;max-width:520px;margin:auto'>"
            "<h2>Ссылка устарела</h2><p>Откройте страницу заново из бота: "
            "📦 Склад → <b>Ritmo OPS</b> или команда /almacen.</p></body>")


# ---------------- ЗАПУСК ----------------

async def _start_own_web(*args, **kwargs):
    global _runner
    if _runner is not None:
        return
    from aiohttp import web
    app = web.Application(client_max_size=MAX_UPLOAD)

    async def health(request):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    add_routes(app)
    port = int(os.environ.get("PORT", "8080"))
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    log.info("stock_web: свой веб-сервер на порту %s", port)


def _attach_to_hr_web() -> bool:
    try:
        import hr_web
    except Exception:
        return False
    orig = getattr(hr_web, "_build_app", None)
    if not callable(orig):
        return False
    if getattr(orig, "_stock_patched", False):
        return True

    def build_with_stock():
        app = orig()
        # по умолчанию aiohttp принимает запрос до 1 МБ — фото накладной больше
        try:
            app._client_max_size = max(getattr(app, "_client_max_size", 0), MAX_UPLOAD)
        except Exception:
            pass
        try:
            add_routes(app)
        except Exception as ex:
            log.error("stock_web: маршруты не добавились: %s", ex, exc_info=True)
        return app

    # сохраняем чужие пометки (fin_web), чтобы он не встроился второй раз
    for attr in ("_fin_patched",):
        if getattr(orig, attr, False):
            setattr(build_with_stock, attr, True)
    build_with_stock._stock_patched = True
    hr_web._build_app = build_with_stock
    return True


def setup(core_module):
    global core
    core = core_module
    if not _attach_to_hr_web():
        try:
            import sys
            dp = getattr(sys.modules.get("stock_block"), "_dp", None)
            if dp is not None:
                dp.startup.register(_start_own_web)
        except Exception as ex:
            log.warning("stock_web: сервер не зарегистрирован: %s", ex)
    log.info("%s подключён", VERSION)


# ---------------- ССЫЛКА ИЗ БОТА ----------------

async def send_link(chat_id: int, uid: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    base = web_url()
    if not base:
        await core.bot.send_message(
            chat_id, "⚠️ Страница не настроена: в Railway нужна переменная "
                     "<code>HR_WEB_URL</code>.")
        return
    link = f"{base}/almacen?t={make_token(uid)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Открыть Ritmo OPS",
                              web_app=WebAppInfo(url=f"{base}/almacen"))],
        [InlineKeyboardButton(text="💻 Открыть в браузере", url=link)],
    ])
    await core.bot.send_message(
        chat_id,
        "📦 <b>Ritmo OPS · приходные накладные</b>\n\n"
        "Фактуры из групп локалей: что распознано, что нужно сопоставить с Syrve, "
        "что уже выгружено. Товар, выбранный один раз, бот запоминает.\n\n"
        "Ссылка «в браузере» помнит вход 90 дней — удобно с компьютера.",
        reply_markup=kb)
