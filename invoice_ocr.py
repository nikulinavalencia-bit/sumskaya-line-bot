# -*- coding: utf-8 -*-
"""
🔍 Распознавание фактур через Gemini.

Отдельный файл, ни от чего в боте не зависит: на вход байты фото или PDF,
на выходе словарь с полями фактуры. Никаких ключей в коде — только окружение.

Переменные Railway:
    GEMINI_API_KEY   — ключ из Google AI Studio. Нет ключа — распознавание
                       просто выключено, бот работает как раньше (ручной ввод).
    GEMINI_MODEL     — модель, если захочется поменять. По умолчанию перебираются
                       несколько доступных, первая ответившая запоминается.
    OCR_TIMEOUT      — таймаут запроса в секундах, по умолчанию 60.
"""

import os
import re
import json
import base64
import logging

import requests

log = logging.getLogger("ocr")

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "60") or 60)
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Пробуем по очереди — какая ответит, ту и запоминаем на весь запуск.
MODELS = [m for m in [
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
] if m]

_working_model = None

# CIF нашей компании — чтобы модель не приняла клиента за поставщика
OWN_CIF = os.environ.get("COMPANY_CIF", "B13942826").strip()
OWN_NAME = os.environ.get("COMPANY_NAME", "SUMSKAYA LINE S.L.").strip()

PROMPT = f"""Eres un extractor de datos de facturas españolas. Analiza la factura y devuelve SOLO un objeto JSON, sin texto alrededor.

Campos:
- "proveedor": razón social de QUIEN EMITE la factura (el vendedor).
- "nif": NIF/CIF del proveedor emisor.
- "iban": IBAN de cobro que aparece en la factura (forma de pago, domiciliación, "CC:"), sin espacios. null si no aparece.
- "numero": número de factura tal cual aparece.
- "fecha": fecha de la factura en formato dd.mm.aaaa.
- "base": base imponible total, número.
- "iva": importe total del IVA, número.
- "total": TOTAL FACTURA a pagar, con IVA incluido, número.

Reglas estrictas:
- El cliente/destinatario es {OWN_NAME} con CIF {OWN_CIF}. NUNCA lo devuelvas como proveedor ni su CIF como nif.
- "total" es el importe final a pagar de toda la factura. No confundir con "Suma y sigue", "Total albarán" ni subtotales de página.
- Números en formato JSON con punto decimal, sin símbolo de moneda ni separador de miles.
- Si un dato no aparece en el documento, pon null. No inventes nada.
"""


def enabled() -> bool:
    return bool(API_KEY)


def _clean_json(text: str) -> dict:
    """Модель обычно отдаёт чистый JSON, но иногда оборачивает в ```json."""
    txt = (text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(txt[start:end + 1])


def _call(model: str, data: bytes, mime: str) -> dict:
    body = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(data).decode()}},
            ]
        }],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    # ключ передаём заголовком: так работают и старые ключи AIzaSy…,
    # и новые вида AQ.… (в адресе строки запроса они не принимаются)
    r = requests.post(ENDPOINT.format(model=model),
                      headers={"x-goog-api-key": API_KEY,
                               "Content-Type": "application/json"},
                      json=body, timeout=TIMEOUT)
    if r.status_code == 404:
        raise LookupError(f"модель {model} недоступна")
    if r.status_code != 200:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    payload = r.json()
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"неожиданный ответ: {str(payload)[:300]}")
    return _clean_json(text)


def _norm_date(value) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
    if not m:
        return ""
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{int(d):02d}.{int(mo):02d}.{y}"


def _norm_amount(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = str(value).replace("€", "").replace(" ", " ").strip()
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".,-")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return 0.0


def recognize(data: bytes, mime: str = "image/jpeg") -> dict:
    """Синхронный вызов — из бота дёргать через asyncio.to_thread.

    Возвращает {"ok": True, поля...} либо {"ok": False, "error": "..."}.
    """
    global _working_model
    if not enabled():
        return {"ok": False, "error": "нет ключа GEMINI_API_KEY"}
    if not data:
        return {"ok": False, "error": "пустой файл"}

    order = ([_working_model] if _working_model else []) + \
            [m for m in MODELS if m != _working_model]
    last_err = None
    raw = None
    for model in order:
        try:
            raw = _call(model, data, mime or "image/jpeg")
            if _working_model != model:
                log.info("ocr: работаю через модель %s", model)
            _working_model = model
            break
        except LookupError as ex:
            last_err = ex
            continue
        except Exception as ex:
            log.warning("ocr: %s — %s", model, ex)
            last_err = ex
            continue
    if raw is None:
        return {"ok": False, "error": str(last_err or "не получилось")}

    iban = "".join(str(raw.get("iban") or "").split()).upper()
    nif = str(raw.get("nif") or "").strip().upper()
    if nif and nif.replace("-", "") == OWN_CIF.replace("-", ""):
        nif = ""          # модель всё-таки взяла нашего клиента — отбрасываем
    return {
        "ok": True,
        "proveedor": str(raw.get("proveedor") or "").strip(),
        "nif": nif,
        "iban": iban,
        "numero": str(raw.get("numero") or "").strip(),
        "fecha": _norm_date(raw.get("fecha")),
        "base": _norm_amount(raw.get("base")),
        "iva": _norm_amount(raw.get("iva")),
        "total": _norm_amount(raw.get("total")),
        "model": _working_model,
    }
