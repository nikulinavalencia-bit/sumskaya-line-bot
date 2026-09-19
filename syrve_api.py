# -*- coding: utf-8 -*-
"""
🔌 syrve_api.py — связь бота с сервером Syrve (тот же API, что у iiko Server).

Отдельный файл, ничего в боте не меняет. Работает синхронно — из бота
вызывать через asyncio.to_thread.

Что умеет:
    products()       номенклатура: товары и заготовки (id, название, артикул, ед.)
    stores()         склады (Kitchen, Bar, Main warehouse…)
    suppliers()      поставщики, заведённые в Syrve
    send_invoice()   приходная накладная — черновиком или проведённой
    balances()       остатки по складам на дату
    check()          проверка входа: для /syrve

Переменные Railway:
    SYRVE_URL        адрес сервера, как в браузере при входе в Syrve Office,
                     например https://boiboi.syrve.online  (можно с :443 и /resto)
    SYRVE_LOGIN      логин
    SYRVE_PASSWORD   пароль (в запрос уходит только его SHA1, как требует Syrve)
    SYRVE_LOCALES    для каких локалей этот сервер, через запятую. По умолчанию boiboi.
                     Когда подключите остальные локали на том же сервере —
                     boiboi,reina,fransia,panaderia
    Если у локали свой сервер — SYRVE_URL_REINA / SYRVE_LOGIN_REINA /
    SYRVE_PASSWORD_REINA (суффикс — код локали из бота заглавными).

Каждый вход в API занимает в Syrve лицензию, поэтому после каждой операции
бот выходит (logout). Лучше завести в Syrve отдельного пользователя «bot»
с правами на приходные накладные — тогда вход бота не выбивает человека.
"""

import os
import re
import time
import hashlib
import logging
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from xml.sax.saxutils import escape as xesc

import requests

log = logging.getLogger("syrve")

VERSION = "syrve_api 1.0 · 19.09.2026"
TIMEOUT = int(os.environ.get("SYRVE_TIMEOUT", "40") or 40)
CACHE_TTL = 20 * 60
# Формат даты в приходной накладной. Если Syrve ругнётся на дату —
# меняется переменной, код трогать не нужно.
DATE_FMT = os.environ.get("SYRVE_DATE_FMT", "%Y-%m-%dT08:00:00")

_cache = {}          # (url, что) -> (ts, данные)
_lock = threading.Lock()


# ---------------- НАСТРОЙКИ ----------------

def _clean_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u
    # всё, что после /resto — отрезаем, добавим сами
    u = re.sub(r"/resto.*$", "", u)
    return u


def config(loc: str):
    """Настройки сервера Syrve для локали или None, если локаль не подключена."""
    suf = "_" + str(loc or "").upper()
    url = os.environ.get("SYRVE_URL" + suf, "")
    login = os.environ.get("SYRVE_LOGIN" + suf, "")
    pwd = os.environ.get("SYRVE_PASSWORD" + suf, "")
    if not url:
        locs = [x.strip().lower() for x in
                os.environ.get("SYRVE_LOCALES", "boiboi").split(",") if x.strip()]
        if str(loc).lower() not in locs:
            return None
        url = os.environ.get("SYRVE_URL", "")
        login = os.environ.get("SYRVE_LOGIN", "")
        pwd = os.environ.get("SYRVE_PASSWORD", "")
    url = _clean_url(url)
    if not (url and login and pwd):
        return None
    return {"url": url, "login": login.strip(), "pwd": pwd}


def enabled(loc: str) -> bool:
    return config(loc) is not None


# ---------------- ВХОД / ВЫХОД ----------------

def _auth(cfg) -> str:
    sha = hashlib.sha1(cfg["pwd"].encode("utf-8")).hexdigest()
    r = requests.get(f"{cfg['url']}/resto/api/auth",
                     params={"login": cfg["login"], "pass": sha}, timeout=TIMEOUT)
    if r.status_code != 200:
        txt = r.text.strip()[:200]
        if r.status_code in (401, 403):
            raise PermissionError(f"Syrve не пустил: неверный логин/пароль или нет прав ({txt})")
        raise RuntimeError(f"Syrve auth {r.status_code}: {txt}")
    token = r.text.strip().strip('"')
    if not token or len(token) > 100:
        raise RuntimeError(f"Syrve вернул странный ответ на вход: {token[:100]}")
    return token


def _logout(cfg, token):
    try:
        requests.get(f"{cfg['url']}/resto/api/logout", params={"key": token}, timeout=15)
    except Exception:
        pass


@contextmanager
def session(cfg):
    token = _auth(cfg)
    try:
        yield token
    finally:
        _logout(cfg, token)


def _get(cfg, token, path, params=None):
    p = {"key": token}
    p.update(params or {})
    r = requests.get(f"{cfg['url']}{path}", params=p, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Syrve {path} {r.status_code}: {r.text[:300]}")
    return r


def _cached(cfg, what, loader, force=False):
    key = (cfg["url"], what)
    with _lock:
        ts, data = _cache.get(key, (0, None))
    if not force and data is not None and time.time() - ts < CACHE_TTL:
        return data
    data = loader()
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def drop_cache():
    with _lock:
        _cache.clear()


# ---------------- СПРАВОЧНИКИ ----------------

def _xml_items(text: str, tag: str) -> list:
    """Список словарей {тег: текст} для каждого <tag> в XML-ответе."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Syrve прислал не XML: {ex}")
    out = []
    for el in root.iter(tag):
        out.append({ch.tag: (ch.text or "").strip() for ch in el})
    return out


def products(loc: str, force=False) -> list:
    """[{id, name, num, code, unit, type}] — только товары и заготовки, без удалённых."""
    cfg = config(loc)
    if not cfg:
        return []

    def load():
        with session(cfg) as tok:
            items = _get(cfg, tok, "/resto/api/v2/entities/products/list").json()
            try:
                units = _get(cfg, tok, "/resto/api/v2/entities/list",
                             {"rootType": "MeasureUnit"}).json()
            except Exception as ex:
                log.warning("syrve: единицы измерения не прочитались: %s", ex)
                units = []
        uname = {u.get("id"): u.get("name") for u in units if isinstance(u, dict)}
        out = []
        for p in items:
            if not isinstance(p, dict) or p.get("deleted"):
                continue
            typ = str(p.get("type") or "").upper()
            if typ and typ not in ("GOODS", "PREPARED"):
                continue
            out.append({
                "id": p.get("id"),
                "name": str(p.get("name") or "").strip(),
                "num": str(p.get("num") or "").strip(),
                "code": str(p.get("code") or "").strip(),
                "unit": uname.get(p.get("mainUnit"), "") or "",
                "type": typ,
            })
        out.sort(key=lambda x: x["name"].lower())
        log.info("syrve: номенклатура %s — %d позиций", loc, len(out))
        return out

    return _cached(cfg, "products", load, force)


def stores(loc: str, force=False) -> list:
    """[{id, name}] — склады. К названию склада дописываем подразделение."""
    cfg = config(loc)
    if not cfg:
        return []

    def load():
        with session(cfg) as tok:
            st = _xml_items(_get(cfg, tok, "/resto/api/corporation/stores").text,
                            "corporateItemDto")
            try:
                deps = _xml_items(_get(cfg, tok, "/resto/api/corporation/departments").text,
                                  "corporateItemDto")
            except Exception:
                deps = []
        dname = {d.get("id"): d.get("name") for d in deps}
        out = []
        for s in st:
            if s.get("type") and s.get("type") != "STORE":
                continue
            dep = dname.get(s.get("parentId"), "")
            out.append({"id": s.get("id"), "name": s.get("name", ""),
                        "dep": dep or ""})
        out.sort(key=lambda x: (x["dep"], x["name"]))
        return out

    return _cached(cfg, "stores", load, force)


def suppliers(loc: str, force=False) -> list:
    """[{id, name, code, nif}] — поставщики из Syrve."""
    cfg = config(loc)
    if not cfg:
        return []

    def load():
        with session(cfg) as tok:
            raw = _xml_items(_get(cfg, tok, "/resto/api/suppliers").text, "employee")
        out = []
        for s in raw:
            if str(s.get("deleted", "false")).lower() == "true":
                continue
            nif = ""
            for k, v in s.items():
                kl = k.lower()
                if ("taxpayer" in kl or kl in ("inn", "cif", "nif")) and v:
                    nif = v
                    break
            out.append({"id": s.get("id"), "name": s.get("name", ""),
                        "code": s.get("code", ""), "nif": nif.replace("-", "").upper()})
        out.sort(key=lambda x: x["name"].lower())
        return out

    return _cached(cfg, "suppliers", load, force)


def check(loc: str) -> dict:
    """Для /syrve: входим, считаем склады/товары/поставщиков."""
    cfg = config(loc)
    if not cfg:
        return {"ok": False, "error": "для этой локали Syrve не настроен"}
    try:
        drop_cache()
        return {"ok": True, "url": cfg["url"], "login": cfg["login"],
                "stores": stores(loc, force=True),
                "products": len(products(loc, force=True)),
                "suppliers": len(suppliers(loc, force=True))}
    except Exception as ex:
        return {"ok": False, "url": cfg["url"], "error": f"{type(ex).__name__}: {ex}"}


# ---------------- ПРИХОДНАЯ НАКЛАДНАЯ ----------------

def _num(v, nd=4) -> str:
    return f"{float(v or 0):.{nd}f}"


def invoice_xml(doc: dict) -> str:
    """doc: {number, supplier_number, date (date), supplier, store, status,
    comment, items: [{product, amount, price, sum, vat, store}]}"""
    items = []
    for i, it in enumerate(doc["items"], start=1):
        parts = [
            f"<num>{i}</num>",
            f"<product>{xesc(it['product'])}</product>",
            f"<amount>{_num(it['amount'])}</amount>",
            f"<price>{_num(it['price'])}</price>",
            f"<sum>{_num(it['sum'], 2)}</sum>",
            f"<store>{xesc(it.get('store') or doc['store'])}</store>",
        ]
        if it.get("vat") not in (None, ""):
            parts.append(f"<vatPercent>{_num(it['vat'], 2)}</vatPercent>")
        items.append("<item>" + "".join(parts) + "</item>")
    head = [
        f"<documentNumber>{xesc(doc['number'])}</documentNumber>",
        f"<dateIncoming>{doc['date'].strftime(DATE_FMT)}</dateIncoming>",
        "<useDefaultDocumentTime>true</useDefaultDocumentTime>",
        f"<incomingDocumentNumber>{xesc(doc.get('supplier_number') or '')}</incomingDocumentNumber>",
        f"<comment>{xesc(doc.get('comment') or '')}</comment>",
        f"<status>{xesc(doc.get('status') or 'NEW')}</status>",
        f"<supplier>{xesc(doc['supplier'])}</supplier>",
        f"<defaultStore>{xesc(doc['store'])}</defaultStore>",
    ]
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><document>" + "".join(head) +
            "<items>" + "".join(items) + "</items></document>")


def send_invoice(loc: str, doc: dict) -> dict:
    """-> {ok, number, error, raw}"""
    cfg = config(loc)
    if not cfg:
        return {"ok": False, "error": "Syrve для этой локали не настроен"}
    body = invoice_xml(doc)
    try:
        with session(cfg) as tok:
            r = requests.post(f"{cfg['url']}/resto/api/documents/import/incomingInvoice",
                              params={"key": tok}, data=body.encode("utf-8"),
                              headers={"Content-Type": "application/xml"}, timeout=TIMEOUT)
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    txt = r.text or ""
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {txt[:400]}", "raw": txt[:2000]}
    try:
        root = ET.fromstring(txt)
        vals = {el.tag: (el.text or "").strip() for el in root.iter()}
    except ET.ParseError:
        vals = {}
    valid = str(vals.get("valid", "")).lower() == "true"
    if not valid:
        err = vals.get("errorMessage") or vals.get("additionalInfo") or txt[:400]
        return {"ok": False, "error": err, "raw": txt[:2000]}
    return {"ok": True, "number": vals.get("documentNumber") or doc["number"],
            "warning": vals.get("warning") == "true", "raw": txt[:2000]}


# ---------------- ОСТАТКИ ----------------

def balances(loc: str, when=None) -> list:
    """[{store, product, amount, sum}] — остатки на момент when (datetime)."""
    cfg = config(loc)
    if not cfg:
        return []
    from datetime import datetime
    ts = (when or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    with session(cfg) as tok:
        data = _get(cfg, tok, "/resto/api/v2/reports/balance/stores",
                    {"timestamp": ts}).json()
    out = []
    for r in data if isinstance(data, list) else []:
        out.append({"store": r.get("store"), "product": r.get("product"),
                    "amount": float(r.get("amount") or 0),
                    "sum": float(r.get("sum") or 0)})
    return out
