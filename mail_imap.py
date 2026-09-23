# =========================================================
#  mail_imap.py — почта Histora через IMAP, без Google Cloud и OAuth
#
#  Причина: в режиме «Prueba» refresh token Google живёт 7 дней, а кнопка
#  «Publicar app» у личного аккаунта не активируется. IMAP с паролем
#  приложения работает годами и ничего не требует от Google Cloud.
#
#  Что нужно один раз:
#   1) у аккаунта sl.valencia.resta@gmail.com включить двухэтапную
#      проверку (myaccount.google.com → Безопасность);
#   2) создать пароль приложения: myaccount.google.com/apppasswords —
#      16 букв без пробелов;
#   3) в Railway задать:
#        GMAIL_USER         = sl.valencia.resta@gmail.com
#        GMAIL_APP_PASSWORD = <16 букв>
#      (необязательно: GMAIL_IMAP_DAYS — за сколько дней смотреть письма,
#       по умолчанию 30; GMAIL_IMAP_HOST — если почта не в Gmail)
#
#  Пока GMAIL_APP_PASSWORD не задан, бот работает по-старому, через OAuth.
# =========================================================

import os
import ssl
import email
import imaplib
import logging
from email.header import decode_header, make_header
from datetime import timedelta

log = logging.getLogger("sumskaya.mail_imap")

VERSION = "mail_imap 1.0 · 23.09.2026"

HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com").strip()
USER = os.environ.get("GMAIL_USER", "sl.valencia.resta@gmail.com").strip()
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
DAYS = int(os.environ.get("GMAIL_IMAP_DAYS", "30") or 30)
FOLDER = os.environ.get("GMAIL_IMAP_FOLDER", "INBOX").strip()


def enabled() -> bool:
    return bool(PASSWORD and USER)


def _decode(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return str(s or "")


def _connect():
    box = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context())
    box.login(USER, PASSWORD)
    box.select(FOLDER, readonly=True)
    return box


def _since(days: int = None):
    from datetime import datetime
    d = datetime.utcnow() - timedelta(days=days or DAYS)
    return d.strftime("%d-%b-%Y")


def _attachments(msg) -> list:
    """[(имя файла, part)] — всё, что приложено к письму."""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _decode(part.get_filename())
        if name:
            out.append((name, part))
    return out


def fetch_recent(keywords, days: int = None, limit: int = 200) -> list:
    """Письма за период, где есть вложение с одним из ключевых слов в имени.

    Возвращает [{id, from, subject, date, attachments: [(filename, filename)]}],
    id — Message-ID письма (стабильный, годится как ключ в таблице).
    """
    box = _connect()
    try:
        typ, data = box.search(None, "SINCE", _since(days))
        if typ != "OK":
            return []
        nums = data[0].split()[-limit:]
        found = []
        for num in reversed(nums):
            typ, raw = box.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            atts = [n for n, _ in _attachments(msg)
                    if any(k.lower() in n.lower() for k in keywords)]
            if not atts:
                continue
            found.append({
                "id": (msg.get("Message-ID") or f"imap-{num.decode()}").strip(),
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
                "attachments": [(n, n) for n in atts],
            })
        return found
    finally:
        try:
            box.logout()
        except Exception:
            pass


def download(message_id: str, filename: str):
    """Байты вложения по Message-ID письма и имени файла."""
    box = _connect()
    try:
        typ, data = box.search(None, "HEADER", "Message-ID", message_id)
        nums = data[0].split() if typ == "OK" else []
        if not nums:                      # письма со странным Message-ID
            typ, data = box.search(None, "SINCE", _since())
            nums = data[0].split() if typ == "OK" else []
        for num in reversed(nums):
            typ, raw = box.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            if nums and (msg.get("Message-ID") or "").strip() not in (message_id, ""):
                if len(nums) > 1:
                    continue
            for name, part in _attachments(msg):
                if name == filename:
                    return part.get_payload(decode=True)
        return None
    finally:
        try:
            box.logout()
        except Exception:
            pass


def check() -> dict:
    """Проверка связи: сколько писем с нужными вложениями видно."""
    try:
        n = len(fetch_recent(("CONTRATO", "BAJA", "CAMBIO")))
        return {"ok": True, "count": n}
    except Exception as e:
        log.error("mail_imap: %s", e)
        return {"ok": False, "error": str(e)}
