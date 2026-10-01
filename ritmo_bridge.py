# -*- coding: utf-8 -*-
"""
🔗 ritmo_bridge.py — мост из бота Sumskaya Line в Ritmo OPS.

Ritmo OPS — отдельный сервис (свой сайт, своя база, свои логины). Бот компании
для него просто один из источников накладных:

  1. Сотрудник кидает фактуру в группу Facturas своей локали — как всегда.
  2. bot.py сохраняет её в лист Docs. Мост подхватывает ту же фактуру,
     скачивает файл(ы) из Telegram и отправляет в Ritmo OPS по ключу точки.
     Несколько фото одним альбомом = одна накладная.
  3. Дальше всё происходит в Ritmo OPS: распознавание, сопоставление, Syrve.

Передачи: если сообщение в той же группе начинается со слова «Передача», «Отдали»,
«Traspaso» или с названия другой локали («В Рейну…»), Ritmo OPS делает из него
расходную накладную у отправителя и приходную у получателя.

Списания: сотрудник пишет обычным текстом в группу Bajas своей локали
(«2 кг помидоров испортились»). Мост пересылает это сообщение в Ritmo OPS,
там из сообщений за день собирается один акт списания, а вечером он уходит
в Syrve сам — если у точки включено «Выгружать готовые акты списания сами».

Для локалей, подключённых к Ritmo OPS, фактуры в Билз больше не пересылаются.
Фото и файлы в группе списаний идут в Билз как раньше.

Подключается сам из selfcheck.py — bot.py не меняется.

Переменные Railway (бот):
    RITMO_URL    адрес сайта Ritmo OPS, например https://ritmo-ops.up.railway.app
    RITMO_KEYS   ключи точек из Ritmo OPS (Настройки → Точки → Ключ для Telegram-бота),
                 через запятую: boiboi=rk_xxxx,reina=rk_yyyy
                 Код локали — как в боте: reina, fransia, panaderia, boiboi.
    RITMO_GROUPS отдельные группы под вид документа (необязательно):
                 -1001234567890=reina:tr,-1009876543210=panaderia:pr
                 tr — расходные (перемещения), pr — приготовление, wo — списания.
                 ChatID группы бот присылает в неё сам, когда его туда добавляют.
                 В группах tr и pr принимаются и ФОТОГРАФИИ бумажных бланков
                 («Traspaso entre Almacenes», лист приготовления): снимок уходит
                 в Ritmo OPS, там его читают и раскладывают по товарам. Альбом из
                 нескольких кадров одного бланка — один документ.
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timedelta

import requests
from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("ritmo_bridge")

VERSION = "ritmo_bridge 1.6 · 01.10.2026"
ALBUM_WAIT = 6          # сек — ждём остальные фото альбома
MARK_OK = "🔗 Ritmo OPS"        # отметка в листе Docs вместо «отправлено в Билз»
MARK_BAD = "⚠️ Ritmo OPS не принял"
TIMEOUT = 90

core = None
_routes_done = False
_meta = {}              # (chat_id, msg_id) -> {"mg": media_group_id, "mime": ...}
_albums = {}            # media_group_id -> {"items": [...], "task": Task}
_last_saved = {"loc": "", "typ": ""}
_sheets = {}            # media_group_id -> {"items": [...], "task": Task} — фото бланков
_seen_wo = set()          # сообщения списаний, уже отправленные в Ritmo OPS
_stats = {"sent": 0, "failed": 0, "wo": 0, "wo_failed": 0, "tr": 0, "tr_failed": 0,
          "pr": 0, "pr_failed": 0, "last_error": ""}


# ---------------- НАСТРОЙКИ ----------------

def ritmo_url() -> str:
    u = os.environ.get("RITMO_URL", "").strip().rstrip("/")
    if u and not u.startswith("http"):
        u = "https://" + u
    return u


def keys() -> dict:
    out = {}
    for part in os.environ.get("RITMO_KEYS", "").replace(";", ",").split(","):
        if "=" in part:
            loc, key = part.split("=", 1)
            if loc.strip() and key.strip():
                out[loc.strip().lower()] = key.strip()
    return out


KINDS = {"tr": "transfer", "traspaso": "transfer", "salida": "transfer", "расход": "transfer",
         "pr": "production", "prod": "production", "produccion": "production",
         "приготовление": "production",
         "wo": "writeoff", "baja": "writeoff", "списание": "writeoff"}


def groups() -> dict:
    """RITMO_GROUPS — отдельные группы под вид документа, без правки bot.py.

    Формат: chat_id=локаль:вид, через запятую. Вид: tr — расходные (перемещения),
    pr — акты приготовления, wo — списания. Например:
        -1001234567890=reina:tr,-1009876543210=panaderia:pr
    Локаль можно не указывать — тогда отправителя берём из первой строки
    сообщения («Reina / Para Francia»). Это для общей группы перемещений:
        -1001234567890=:tr
    Если группы здесь нет, мост работает как раньше: берёт группы списаний
    из листа Groups бота и различает вид документа по первому слову сообщения.
    """
    out = {}
    for part in os.environ.get("RITMO_GROUPS", "").replace(";", ",").split(","):
        if "=" not in part:
            continue
        cid, val = part.split("=", 1)
        loc, _, kind = val.partition(":")
        kind = KINDS.get(kind.strip().lower(), "writeoff")
        loc = loc.strip().lower()
        if loc in ("*", "-", "все", "any"):
            loc = ""
        if cid.strip():
            out[cid.strip()] = (loc, kind)
    return out


# как локали называют друг друга в переписке
LOC_ALIASES = {
    "reina": ("reina", "reyna", "рейна", "рейну", "рейне"),
    "fransia": ("francia", "fransia", "francya", "франция", "францию", "франции", "франсия"),
    "francia": ("francia", "fransia", "francya", "франция", "францию", "франции"),
    "panaderia": ("panaderia", "panadería", "bakery", "obrador", "пекарня", "панадерия", "обрадор"),
    "bakery": ("bakery", "panaderia", "panadería", "obrador", "пекарня"),
    "boiboi": ("boiboi", "boi boi", "boi-boi", "boi", "бой бой", "бойбой", "бой", "bb"),
}


def _loc_code(text: str) -> str:
    """Код локали по куску текста — только среди тех, у кого есть ключ Ritmo OPS."""
    t = " " + " ".join(str(text or "").lower().split()) + " "
    best, pos = "", 10 ** 6
    for code in keys():
        for a in LOC_ALIASES.get(code, (code,)):
            i = t.find(a)
            if i >= 0 and i < pos:
                best, pos = code, i
    return best


def _route(text: str) -> str:
    """Отправитель из первой строки: «Reina / Para Francia» → reina.

    Берём только часть до «/» или до «para»: после неё стоит получатель,
    его определит уже сам Ritmo OPS.
    """
    first = ""
    for row in str(text or "").split("\n"):
        if row.strip():
            first = row.strip()
            break
    low = " " + first.lower() + " "
    left = first
    if "/" in first:
        left = first.split("/", 1)[0]
    else:
        for w in (" para ", " в ", " to "):
            if w in low:
                left = low.split(w, 1)[0]
                break
        else:
            left = ""
    return _loc_code(left)


def _looks_like_doc(text: str) -> bool:
    """Похоже на список товаров: есть строка с количеством. Болтовню не трогаем."""
    for row in str(text or "").split("\n"):
        if re.search(r"\d", row) and re.search(r"[A-Za-zА-Яа-яЁёÁÉÍÓÚÑáéíóúñ]{2,}", row):
            return True
    return False


def enabled_for(loc: str) -> bool:
    return bool(ritmo_url()) and loc in keys()


# ---------------- ОТПРАВКА В RITMO OPS ----------------

def _post(loc: str, files: list, author: str, ref: str, extra: dict = None) -> dict:
    """files: [(bytes, mime, name)] — синхронно, из потока."""
    payload = [("files", (name, data, mime)) for data, mime, name in files]
    last = ""
    for attempt in range(3):
        try:
            data = {"author": author, "ref": ref, "source": "telegram"}
            data.update(extra or {})
            r = requests.post(f"{ritmo_url()}/api/intake",
                              headers={"X-Ritmo-Key": keys()[loc]},
                              data=data, files=payload or None, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (401, 403, 413):
                break                          # повтор не поможет
        except Exception as ex:
            last = f"{type(ex).__name__}: {ex}"
        import time
        time.sleep(3 * (attempt + 1))
    return {"ok": False, "error": last}


async def send(items: list) -> dict:
    """Одна накладная из одного или нескольких сообщений."""
    loc = items[0]["loc"]
    files = []
    for it in items:
        try:
            buf = await core.bot.download(it["file_id"])
            data = buf.read()
        except Exception as ex:
            log.warning("ritmo_bridge: файл не скачался: %s", ex)
            continue
        ext = "pdf" if data[:4] == b"%PDF" else "jpg"
        files.append((data, it.get("mime") or "", f"tg_{it['msg_id']}.{ext}"))
    if not files:
        return {"ok": False, "error": "файлы не скачались из Telegram"}
    ref = f"tg:{items[0]['chat_id']}:" + ",".join(str(it["msg_id"]) for it in items)
    res = await asyncio.to_thread(_post, loc, files, items[0].get("author", ""), ref)
    await _mark(items[0].get("row"), MARK_OK if res.get("ok") else MARK_BAD)
    if res.get("ok"):
        _stats["sent"] += 1
    else:
        _stats["failed"] += 1
        _stats["last_error"] = str(res.get("error"))[:300]
        log.error("ritmo_bridge: накладная не ушла в Ritmo OPS: %s", res.get("error"))
        for pid in core.patrons():
            try:
                await core.bot.send_message(
                    pid, f"⚠️ Фактура из группы ({loc}) не ушла в Ritmo OPS:\n"
                         f"<code>{core.html_lib.escape(str(res.get('error'))[:300])}</code>\n\n"
                         f"Её можно загрузить на сайте вручную — файл есть в группе.")
            except Exception:
                pass
    return res


async def _mark(row, mark: str):
    """Отметка в листе Docs (колонка «Письмо»). Её же бот показывает
    в карточке документа — вместо «отправлено в Билз» будет Ritmo OPS."""
    if not row:
        return
    try:
        await asyncio.to_thread(core.set_doc_email, int(row), mark)
    except Exception as ex:
        log.warning("ritmo_bridge: отметка в Docs не проставлена: %s", ex)


# ---------------- СПИСАНИЯ: ТЕКСТ ИЗ ГРУПП ----------------

async def send_writeoff(loc: str, text: str, author: str, ref: str) -> dict:
    """Одно сообщение из группы списаний. Ritmo OPS собирает из них акт дня."""
    res = await asyncio.to_thread(_post, loc, [], author, ref, {"kind": "writeoff", "text": text})
    if res.get("ok"):
        _stats["wo"] += 1
    else:
        _stats["wo_failed"] += 1
        _stats["last_error"] = str(res.get("error"))[:300]
        log.error("ritmo_bridge: списание не ушло в Ritmo OPS: %s", res.get("error"))
    return res


# Передача в другую локаль: сообщение начинается со слова-маркера или
# с названия локали-получателя. Список локалей — из RITMO_KEYS и LOCALES бота.
TR_WORDS = ("передача", "передали", "передаём", "передаем", "перемещение", "отдали", "отдаю",
            "traspaso", "traslado", "para ", "в рейну", "в францию", "в бакери", "в бойбой")


PR_WORDS = ("приготов", "испек", "выпек", "напек", "производ", "произвел", "произвели",
            "producci", "elabora", "horneado")


def _is_production(text: str) -> bool:
    first = " ".join(str(text or "").lower().split("\n")[0].split())
    return any(w in first for w in PR_WORDS)


async def send_production(loc: str, text: str, author: str, ref: str) -> dict:
    """Акт приготовления: изделия пекарни и кухни."""
    res = await asyncio.to_thread(_post, loc, [], author, ref, {"kind": "production", "text": text})
    if res.get("ok"):
        _stats["pr"] += 1
    else:
        _stats["pr_failed"] += 1
        _stats["last_error"] = str(res.get("error"))[:300]
        log.error("ritmo_bridge: приготовление не ушло в Ritmo OPS: %s", res.get("error"))
    return res


def _is_transfer(text: str) -> bool:
    t = " " + " ".join(str(text or "").lower().split())
    first = t.strip().split("\n")[0]
    if any(w in first for w in TR_WORDS):
        return True
    names = []
    for code in keys():
        names.append(code)
        loc = (getattr(core, "LOCALES", {}) or {}).get(code) or {}
        if loc.get("name"):
            names.append(str(loc["name"]).lower())
    return any(n and n in first for n in names)


async def _ask_sender(m):
    """В общей группе перемещений не понятно, кто отдаёт — просим написать."""
    try:
        await core.bot.send_message(
            m.chat.id,
            "Не понял, кто отправитель. Напишите первой строкой откуда и куда, "
            "например: <code>Reina / Para Francia</code>",
            reply_to_message_id=m.message_id)
    except Exception as ex:
        log.warning("ritmo_bridge: не ответил в группу: %s", ex)


async def send_transfer(loc: str, text: str, author: str, ref: str) -> dict:
    """Передача между локалями: расход у отправителя, приход у получателя."""
    res = await asyncio.to_thread(_post, loc, [], author, ref, {"kind": "transfer", "text": text})
    if res.get("ok"):
        _stats["tr"] += 1
    else:
        _stats["tr_failed"] += 1
        _stats["last_error"] = str(res.get("error"))[:300]
        log.error("ritmo_bridge: передача не ушла в Ritmo OPS: %s", res.get("error"))
    return res


def _file_of(m):
    """file_id и mime вложения сообщения: фото или документ (jpg/pdf)."""
    if getattr(m, "photo", None):
        return m.photo[-1].file_id, "image/jpeg"
    doc = getattr(m, "document", None)
    if doc:
        mime = str(doc.mime_type or "")
        name = (doc.file_name or "").lower()
        if not mime:
            mime = "application/pdf" if name.endswith(".pdf") else "image/jpeg"
        if mime.startswith("image/") or mime == "application/pdf":
            return doc.file_id, mime
    return "", ""


async def send_sheet(items: list, loc: str, kind: str) -> dict:
    """Фотографии бумажного бланка (traspaso или лист приготовления) — в Ritmo OPS.

    Альбом из нескольких снимков одного бланка уходит одним документом."""
    files = []
    for it in items:
        try:
            buf = await core.bot.download(it["file_id"])
            data = buf.read()
        except Exception as ex:
            log.warning("ritmo_bridge: бланк не скачался: %s", ex)
            continue
        ext = "pdf" if data[:4] == b"%PDF" else "jpg"
        files.append((data, it.get("mime") or "", f"tg_{it['msg_id']}.{ext}"))
    if not files:
        return {"ok": False, "error": "файлы не скачались из Telegram"}
    first = items[0]
    ref = f"tg:{first['chat_id']}:" + ",".join(str(it["msg_id"]) for it in items)
    extra = {"kind": kind, "text": first.get("caption", "")}
    res = await asyncio.to_thread(_post, loc, files, first.get("author", ""), ref, extra)
    key = "pr" if kind == "production" else "tr"
    if res.get("ok"):
        _stats[key] += 1
    else:
        _stats[key + "_failed"] += 1
        _stats["last_error"] = str(res.get("error"))[:300]
        log.error("ritmo_bridge: бланк не ушёл в Ritmo OPS: %s", res.get("error"))
    return res


async def _sheet_intake(item: dict, mg: str, loc: str, kind: str):
    """Ждём остальные снимки альбома — один бланк могут снять в несколько кадров."""
    if not mg:
        await send_sheet([item], loc, kind)
        return
    a = _sheets.setdefault(mg, {"items": [], "task": None})
    a["items"].append(item)
    if a["task"] is None:
        async def later():
            await asyncio.sleep(ALBUM_WAIT)
            items = _sheets.pop(mg, {}).get("items", [])
            items.sort(key=lambda x: int(x["msg_id"]))
            if items:
                await send_sheet(items, loc, kind)
        a["task"] = asyncio.create_task(later())


def _wo_text(m) -> str:
    """Текст сообщения, если его стоит считать списанием."""
    t = (getattr(m, "text", "") or getattr(m, "caption", "") or "").strip()
    if not t or t.startswith("/") or len(t) < 3:
        return ""
    for a in ("photo", "document", "video", "voice", "audio"):
        if getattr(m, a, None):
            return ""        # фото/файл в группе списаний — как раньше, в Билз
    return t[:2000]


async def _writeoff_watch(m):
    """Сообщение из группы → в Ritmo OPS: списание, расходная или приготовление."""
    if not m.chat or m.chat.type not in ("group", "supergroup"):
        return
    fixed = groups().get(str(m.chat.id))          # группа с заданным видом документа
    if fixed:
        loc, want = fixed
        text0 = _wo_text(m)
        if want in ("transfer", "production") and text0:
            if want == "transfer" and not _looks_like_doc(text0):
                return                            # переписка без количеств — не документ
            # в общей группе отправителя пишут первой строкой: «Reina / Para Francia».
            # Для приготовления строка необязательна: нет её — берём локаль группы.
            sender = _route(text0)
            if sender:
                loc = sender
            elif not loc:
                await _ask_sender(m)
                return
    else:
        gmap = getattr(core, "group_map", None)
        gm = gmap(m.chat.id) if callable(gmap) else None
        if not gm or gm[1] != "baja":
            return
        loc, want = gm[0], ""
    if not enabled_for(loc):
        return
    # фотография бумажного бланка в группе перемещений или приготовления.
    # Бланк заполняют от руки, текста в сообщении обычно нет — читаем сам снимок.
    if fixed and want in ("transfer", "production"):
        fid, mime = _file_of(m)
        if fid:
            item = {"chat_id": m.chat.id, "msg_id": m.message_id, "file_id": fid, "mime": mime,
                    "author": m.from_user.full_name if m.from_user else "—",
                    "caption": (getattr(m, "caption", "") or "")[:500]}
            mg = str(getattr(m, "media_group_id", "") or "")
            res = {"ok": True}
            try:
                await _sheet_intake(item, mg, loc, want)
            except Exception as ex:
                res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            if not res.get("ok"):
                log.error("ritmo_bridge: бланк не поставлен в отправку: %s", res.get("error"))
            return
    text = _wo_text(m)
    if not text:
        return
    author = m.from_user.full_name if m.from_user else "—"
    ref = f"tg:{m.chat.id}:{m.message_id}"
    if want == "production" or (not want and _is_production(text)):
        kind, res = "Акт приготовления", await send_production(loc, text, author, ref)
    elif want == "transfer" or (not want and _is_transfer(text)):
        kind, res = "Расходная", await send_transfer(loc, text, author, ref)
    else:
        kind, res = "Списание", await send_writeoff(loc, text, author, ref)
    if not res.get("ok"):
        what = kind
        for pid in core.patrons():
            try:
                await core.bot.send_message(
                    pid, f"⚠️ {what} из группы ({loc}) не ушла в Ritmo OPS:\n"
                         f"<code>{core.html_lib.escape(str(res.get('error'))[:300])}</code>")
            except Exception:
                pass


# ---------------- ПЕРЕХВАТ ФАКТУР ИЗ ГРУПП ----------------

async def _observe(handler, event, data):
    """Мидлварь: только запоминает альбом и тип файла, ничего не меняет."""
    try:
        if event.chat and event.chat.type in ("group", "supergroup"):
            mime = "image/jpeg"
            if event.document:
                mime = str(event.document.mime_type or "")
                if not mime:
                    n = (event.document.file_name or "").lower()
                    mime = "application/pdf" if n.endswith(".pdf") else "image/jpeg"
            _meta[(str(event.chat.id), str(event.message_id))] = {
                "mg": event.media_group_id or "", "mime": mime}
            if len(_meta) > 2000:
                for k in list(_meta)[:1000]:
                    _meta.pop(k, None)
    except Exception:
        pass
    # списания приходят обычным текстом, а bot.py такие сообщения не сохраняет —
    # поэтому ловим их здесь, до обработчиков, и ничего в боте не меняем
    try:
        key = (str(event.chat.id), str(event.message_id)) if event.chat else None
        if key and key not in _seen_wo:
            _seen_wo.add(key)
            if len(_seen_wo) > 4000:
                _seen_wo.clear()
            asyncio.get_running_loop().create_task(_writeoff_watch(event))
    except Exception as ex:
        log.warning("ritmo_bridge: списание не поставлено в отправку: %s", ex)
    return await handler(event, data)


def _wrap_save_doc():
    orig = core.save_doc
    if getattr(orig, "_ritmo_wrapped", False):
        return

    def wrapped(loc, typ, author, chat_id, msg_id, file_id, text):
        _last_saved["loc"], _last_saved["typ"] = loc, typ
        res = orig(loc, typ, author, chat_id, msg_id, file_id, text)
        try:
            if typ == "factura" and file_id and enabled_for(loc):
                meta = _meta.get((str(chat_id), str(msg_id)), {})
                item = {"loc": loc, "author": author, "chat_id": chat_id, "msg_id": msg_id,
                        "file_id": file_id, "mime": meta.get("mime", "image/jpeg"),
                        "row": res}
                asyncio.get_running_loop().create_task(_intake(item, meta.get("mg", "")))
        except Exception as ex:
            log.error("ritmo_bridge: не поставил фактуру в отправку: %s", ex)
        return res

    wrapped._ritmo_wrapped = True
    core.save_doc = wrapped


def _wrap_billz_check():
    """bot.py сразу после save_doc спрашивает, пересылать ли фактуру в Билз.
    Для локалей Ritmo OPS отвечаем «нет» — в листе Docs будет отметка ➖."""
    orig = getattr(core, "caption_matches_billz_supplier", None)
    if not callable(orig) or getattr(orig, "_ritmo_wrapped", False):
        return

    def wrapped(text):
        if _last_saved.get("typ") == "factura" and enabled_for(_last_saved.get("loc")):
            return False
        return orig(text)

    wrapped._ritmo_wrapped = True
    core.caption_matches_billz_supplier = wrapped


async def _intake(item: dict, mg: str):
    if not mg:
        await send([item])
        return
    a = _albums.setdefault(mg, {"items": [], "task": None})
    a["items"].append(item)
    if a["task"] is None:
        async def later():
            await asyncio.sleep(ALBUM_WAIT)
            items = _albums.pop(mg, {}).get("items", [])
            items.sort(key=lambda x: int(x["msg_id"]))
            await send(items)
        a["task"] = asyncio.create_task(later())


# ---------------- ТЕЛЕГРАМ ----------------

def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def cb_stock_root(c: CallbackQuery):
    """Экран «Склад» — как в bot.py, плюс кнопка Ritmo OPS."""
    _ensure_routes()
    u = core.guard(c)
    if not u:
        await c.answer(core.t("no_access", core.DEFAULT_LANG), show_alert=True)
        return
    lang = core.ulang(u)
    core.nav_push(c.from_user.id, "d:stock")
    kb = []
    if ritmo_url():
        kb.append([InlineKeyboardButton(text="📦 Загрузить фактуру · Ritmo OPS", url=ritmo_url())])
    if core.is_patron(u):
        kb.append([btn(core.t("today", lang), "today")])
        kb.append([btn(f"{core.DOCTYPES['factura']['emoji']} {core.t('invoices', lang)} "
                       f"({len(core.pending_docs_by(typ='factura'))})", "inv")])
        kb.append([btn(f"{core.DOCTYPES['baja']['emoji']} {core.t('writeoffs', lang)} "
                       f"({len(core.pending_docs_by(typ='baja'))})", "wo")])
    kb.append([btn(f"{core.MENU_EMOJI} {core.dept_name('menu', lang)}", "menu")])
    kb.append([btn(core.t("back", lang), "bk")])
    await core.take_over(c, core.crumb("stock", lang), InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


async def cmd_almacen(m: Message):
    if m.chat.type != "private":
        return
    if not ritmo_url():
        await m.answer("Ritmo OPS ещё не подключён: в Railway бота нужна переменная RITMO_URL.")
        return
    await m.answer("📦 <b>Ritmo OPS</b> — приходные накладные.\n\nВход по email и паролю, "
                   "которые выдал администратор.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                       InlineKeyboardButton(text="Открыть Ritmo OPS", url=ritmo_url())]]))


def status_text() -> str:
    lines = ["🔗 <b>Ritmo OPS</b>", ""]
    url = ritmo_url()
    lines.append(f"Адрес: {core.html_lib.escape(url) if url else '— не задан (RITMO_URL)'}")
    if url:
        try:
            r = requests.get(f"{url}/health", timeout=10)
            lines.append("Сайт: ✅ отвечает" if r.status_code == 200 else f"Сайт: ❌ HTTP {r.status_code}")
        except Exception as ex:
            lines.append(f"Сайт: ❌ {type(ex).__name__}")
    k = keys()
    names = [core.LOCALES.get(x, {}).get("name", x) for x in k]
    lines.append(f"Локали с ключом: {', '.join(names) if names else '— нет (RITMO_KEYS)'}")
    unknown = [x for x in k if x not in core.LOCALES]
    if unknown:
        lines.append(f"⚠️ Неизвестные коды локалей: {', '.join(unknown)} "
                     f"(нужно: {', '.join(core.LOCALES)})")
    lines.append(f"Фактур отправлено с запуска: {_stats['sent']}, не ушло: {_stats['failed']}")
    lines.append(f"Сообщений списания: {_stats['wo']}, не ушло: {_stats['wo_failed']}")
    lines.append(f"Расходных между локалями: {_stats['tr']}, не ушло: {_stats['tr_failed']}")
    lines.append(f"Актов приготовления: {_stats['pr']}, не ушло: {_stats['pr_failed']}")
    g = groups()
    if g:
        names = {"transfer": "расходные", "production": "приготовление", "writeoff": "списания"}
        lines.append("Отдельные группы (RITMO_GROUPS): " +
                     ", ".join(f"{loc} — {names.get(k, k)}" for loc, k in g.values()))
    if _stats["last_error"]:
        lines.append(f"Последняя ошибка: <code>{core.html_lib.escape(_stats['last_error'])}</code>")
    lines.append("")
    lines.append("Фактуры этих локалей в Билз больше не пересылаются.")
    return "\n".join(lines)


async def cmd_ritmo(m: Message):
    if m.chat.type != "private" or not core.is_patron(core.get_user(m.from_user.id)):
        return
    await m.answer(await asyncio.to_thread(status_text))


async def cmd_here(m: Message):
    """/ritmo_here прямо в группе — показывает, как мост видит эту группу.

    Чаще всего «из группы ничего не приходит» означает одно: её chat_id не вписан
    в RITMO_GROUPS, и мост о ней не знает. Эта команда снимает все догадки."""
    if m.chat.type not in ("group", "supergroup"):
        await m.answer("Эту команду нужно отправить в саму группу.")
        return
    if not core.is_patron(core.get_user(m.from_user.id)):
        return
    cid = str(m.chat.id)
    names = {"transfer": "перемещения (расходные)", "production": "акты приготовления",
             "writeoff": "списания"}
    out = ["🔗 <b>Эта группа и Ritmo OPS</b>", "",
           f"ChatID: <code>{cid}</code>"]
    fixed = groups().get(cid)
    if fixed:
        loc, kind = fixed
        out.append(f"В RITMO_GROUPS: да — {names.get(kind, kind)}")
        out.append(f"Отправитель: {loc or 'берётся из первой строки сообщения'}")
        if loc and not enabled_for(loc):
            out.append(f"⚠️ У локали <code>{loc}</code> нет ключа в RITMO_KEYS — "
                       f"сообщения не уйдут.")
        elif loc:
            out.append("Ключ локали: ✅ есть")
    else:
        gmap = getattr(core, "group_map", None)
        gm = gmap(m.chat.id) if callable(gmap) else None
        if gm and gm[1] == "baja":
            out.append(f"В RITMO_GROUPS: нет, но это группа списаний локали {gm[0]} "
                       f"из листа Groups — сообщения уходят как списания.")
            if not enabled_for(gm[0]):
                out.append(f"⚠️ У локали <code>{gm[0]}</code> нет ключа в RITMO_KEYS.")
        else:
            out.append("В RITMO_GROUPS: <b>нет</b> — мост эту группу не слушает.")
            out.append("")
            out.append("Чтобы заработало, добавьте в Railway бота к переменной "
                       "RITMO_GROUPS через запятую:")
            out.append(f"<code>{cid}=ЛОКАЛЬ:ВИД</code>")
            out.append("ВИД: <code>tr</code> — перемещения, <code>pr</code> — приготовление, "
                       "<code>wo</code> — списания.")
            out.append(f"Локали с ключом: {', '.join(keys()) or '— нет (RITMO_KEYS)'}")
            out.append("Для общей группы перемещений локаль можно не писать: "
                       f"<code>{cid}=:tr</code> — тогда отправителя берём из первой строки "
                       "сообщения («Reina / Para Francia»).")
    if not ritmo_url():
        out.append("")
        out.append("⚠️ RITMO_URL не задан — мост выключен целиком.")
    await m.answer("\n".join(out))


async def cmd_import(m: Message):
    """/ritmo_import 3 [локаль] — отправить в Ritmo OPS фактуры из групп за N дней."""
    if m.chat.type != "private" or not core.is_patron(core.get_user(m.from_user.id)):
        return
    parts = (m.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
    only = parts[2].lower() if len(parts) > 2 else None
    since = core.now_local().date() - timedelta(days=days)
    todo = []
    for r in core.rows(core.DOCS_WS, force=True):
        loc = str(r.get("Локаль", "")).strip()
        if str(r.get("Тип", "")).strip() != "factura" or not enabled_for(loc) or (only and loc != only):
            continue
        if str(r.get("Статус", "")).strip() == "удалено" or not str(r.get("FileID", "")).strip():
            continue
        try:
            d = datetime.strptime(str(r.get("Дата", "")).strip(), "%d.%m.%Y").date()
        except ValueError:
            continue
        if d >= since:
            todo.append({"loc": loc, "author": r.get("Автор", ""), "chat_id": r.get("ChatID"),
                         "msg_id": r.get("MessageID"), "file_id": r.get("FileID"), "mime": ""})
    await m.answer(f"Отправляю в Ritmo OPS фактур: {len(todo)}. Повторы Ritmo OPS отсеет сам.")
    ok = 0
    for it in todo:
        res = await send([it])
        ok += 1 if res.get("ok") else 0
    await m.answer(f"Готово: принято {ok} из {len(todo)}.")


def _ensure_routes():
    global _routes_done
    if _routes_done:
        return
    table = getattr(core, "ROUTES", None)
    if table is None:
        return
    table.insert(0, ("d:stock", cb_stock_root))
    _routes_done = True


def setup(dp, core_module):
    global core
    core = core_module
    _wrap_save_doc()
    _wrap_billz_check()
    dp.message.outer_middleware(_observe)
    dp.callback_query.register(cb_stock_root, F.data == "d:stock")
    dp.message.register(cmd_almacen, Command("almacen"))
    dp.message.register(cmd_ritmo, Command("ritmo"))
    dp.message.register(cmd_here, Command("ritmo_here"))
    dp.message.register(cmd_import, Command("ritmo_import"))
    log.info("%s подключён: %s, локали %s", VERSION, ritmo_url() or "—", ", ".join(keys()) or "—")
