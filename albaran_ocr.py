# -*- coding: utf-8 -*-
"""
🔍 albaran_ocr.py — полное чтение накладной/фактуры для склада: шапка + все строки.

Тот же Gemini и тот же ключ, что у финблока (invoice_ocr.py). Отличие: финблоку
хватает поставщика и итога, а складу нужна каждая строка — товар, количество,
единица, цена, сумма, IVA. Несколько фото одного документа (альбом) читаются
одним запросом как одна накладная.

Переменные Railway (необязательные):
    STOCK_OCR_MODEL  модель для строк. По умолчанию пробуются более сильные
                     flash-модели, потом те же, что у финблока.
"""

import os
import base64
import logging

import requests

import invoice_ocr as base

log = logging.getLogger("albaran_ocr")

TIMEOUT = max(base.TIMEOUT, 120)

MODELS = [m for m in [
    os.environ.get("STOCK_OCR_MODEL", "").strip(),
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
] + list(base.MODELS) if m]
MODELS = list(dict.fromkeys(MODELS))

_working = None

PROMPT = f"""Eres un extractor de datos de albaranes y facturas de proveedores de restaurantes en España.
Las imágenes pueden ser varias páginas del MISMO documento. Devuelve SOLO un objeto JSON.

Campos de cabecera:
- "tipo": "albaran", "factura", "ticket" u "otro".
- "proveedor": razón social de QUIEN EMITE el documento (el vendedor).
- "nif": NIF/CIF del emisor.
- "numero": número del documento tal cual aparece.
- "fecha": fecha del documento, dd.mm.aaaa.
- "base": base imponible total (sin IVA), número.
- "iva": importe total de IVA, número.
- "total": importe total con IVA, número.
- "lineas": lista de TODAS las líneas de producto, en el orden del documento.

Cada línea:
- "codigo": código/referencia del artículo del proveedor, o null.
- "descripcion": descripción exacta del artículo como aparece.
- "cantidad": cantidad facturada en la unidad a la que se aplica el precio (si hay cajas y kilos y el precio es por kilo, usa los kilos).
- "unidad": unidad de esa cantidad: "kg", "l", "ud", "caja", "paquete", "botella"… tal como se entienda del documento.
- "precio": precio unitario SIN IVA, ya con el descuento aplicado si lo hay.
- "importe": importe neto de la línea SIN IVA (después de descuentos).
- "iva_pct": porcentaje de IVA de la línea (4, 10, 21…) o null.
- "cargo": true si NO es un producto sino envases, depósito, portes, recargo de equivalencia, punto verde o similar; si no, false.

Reglas estrictas:
- El cliente es {base.OWN_NAME} con CIF {base.OWN_CIF}: NUNCA es el proveedor.
- No inventes líneas. No juntes líneas distintas. No repitas líneas de otra página.
- Ignora totales, subtotales, "suma y sigue", pies de página.
- Si una línea tiene precio con IVA incluido, calcula el precio y el importe sin IVA.
- Números en JSON con punto decimal, sin símbolo de moneda ni separador de miles.
- Si un dato no aparece, pon null.
"""


def enabled() -> bool:
    return base.enabled()


def _call(model: str, files: list) -> dict:
    parts = [{"text": PROMPT}]
    for data, mime in files:
        parts.append({"inline_data": {"mime_type": mime or "image/jpeg",
                                      "data": base64.b64encode(data).decode()}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json",
                             "maxOutputTokens": 32768},
    }
    r = requests.post(base.ENDPOINT.format(model=model),
                      headers={"x-goog-api-key": base.API_KEY,
                               "Content-Type": "application/json"},
                      json=body, timeout=TIMEOUT)
    if r.status_code == 404:
        raise LookupError(f"модель {model} недоступна")
    if r.status_code != 200:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    payload = r.json()
    try:
        text = "".join(p.get("text", "") for p in
                       payload["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise RuntimeError(f"неожиданный ответ: {str(payload)[:300]}")
    return base._clean_json(text)


def _num(v):
    if v is None or v == "":
        return None
    n = base._norm_amount(v)
    return n


def _qty(v):
    """Количество может быть дробным с 3 знаками — _norm_amount округляет до 2."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def recognize(files: list) -> dict:
    """files: [(bytes, mime)]. Синхронно — из бота через asyncio.to_thread.
    -> {"ok": True, шапка..., "lineas": [...]} или {"ok": False, "error": ...}"""
    global _working
    if not enabled():
        return {"ok": False, "error": "нет ключа GEMINI_API_KEY"}
    files = [(d, m) for d, m in files if d]
    if not files:
        return {"ok": False, "error": "пустой файл"}

    order = ([_working] if _working else []) + [m for m in MODELS if m != _working]
    raw, last = None, None
    for model in order:
        try:
            raw = _call(model, files)
            _working = model
            break
        except LookupError as ex:
            last = ex
        except Exception as ex:
            log.warning("albaran_ocr: %s — %s", model, ex)
            last = ex
    if raw is None:
        return {"ok": False, "error": str(last or "не получилось")}

    nif = str(raw.get("nif") or "").strip().upper()
    if nif and nif.replace("-", "") == base.OWN_CIF.replace("-", ""):
        nif = ""
    lines = []
    for i, l in enumerate(raw.get("lineas") or [], start=1):
        if not isinstance(l, dict):
            continue
        desc = str(l.get("descripcion") or "").strip()
        if not desc:
            continue
        qty = _qty(l.get("cantidad"))
        price = _qty(l.get("precio"))
        imp = _num(l.get("importe"))
        if imp is None and qty and price:
            imp = round(qty * price, 2)
        if price is None and qty and imp:
            price = round(imp / qty, 4)
        lines.append({
            "n": i,
            "code": str(l.get("codigo") or "").strip(),
            "name": desc,
            "qty": qty or 0,
            "unit": str(l.get("unidad") or "").strip().lower(),
            "price": price or 0,
            "sum": imp or 0,
            "vat": _num(l.get("iva_pct")),
            "cargo": bool(l.get("cargo")),
        })
    return {
        "ok": True,
        "tipo": str(raw.get("tipo") or "").strip().lower(),
        "proveedor": str(raw.get("proveedor") or "").strip(),
        "nif": nif.replace("-", ""),
        "numero": str(raw.get("numero") or "").strip(),
        "fecha": base._norm_date(raw.get("fecha")),
        "base": _num(raw.get("base")) or 0,
        "iva": _num(raw.get("iva")) or 0,
        "total": _num(raw.get("total")) or 0,
        "lineas": lines,
        "model": _working,
    }
