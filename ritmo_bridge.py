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
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta

import requests
from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("ritmo_bridge")

VERSION = "ritmo_bridge 1.1 · 23.09.2026"
ALBUM_WAIT = 6          # сек — ждём остальные фото альбома
MARK_OK = "🔗 Ritmo OPS"        # отметка в листе Docs вместо «отправлено в Билз»
MARK_BAD = "⚠️ Ritmo OPS не принял"
TIMEOUT = 90

core = None
_routes_done = False
_meta = {}              # (chat_id, msg_id) -> {"mg": media_group_id, "mime": ...}
_albums = {}            # media_group_id -> {"items": [...], "task": Task}
_last_saved = {"loc": "", "typ": ""}
_seen_wo = set()          # сообщения списаний, уже отправленные в Ritmo OPS
_stats = {"sent": 0, "failed": 0, "wo": 0, "wo_failed": 0, "last_error": ""}


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
    """Сообщение в группе списаний → в Ritmo OPS, в акт сегодняшнего дня."""
    if not m.chat or m.chat.type not in ("group", "supergroup"):
        return
    gmap = getattr(core, "group_map", None)
    gm = gmap(m.chat.id) if callable(gmap) else None
    if not gm:
        return
    loc, typ = gm
    if typ != "baja" or not enabled_for(loc):
        return
    text = _wo_text(m)
    if not text:
        return
    author = m.from_user.full_name if m.from_user else "—"
    ref = f"tg:{m.chat.id}:{m.message_id}"
    res = await send_writeoff(loc, text, author, ref)
    if not res.get("ok"):
        for pid in core.patrons():
            try:
                await core.bot.send_message(
                    pid, f"⚠️ Списание из группы ({loc}) не ушло в Ritmo OPS:\n"
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
    if _stats["last_error"]:
        lines.append(f"Последняя ошибка: <code>{core.html_lib.escape(_stats['last_error'])}</code>")
    lines.append("")
    lines.append("Фактуры этих локалей в Билз больше не пересылаются.")
    return "\n".join(lines)


async def cmd_ritmo(m: Message):
    if m.chat.type != "private" or not core.is_patron(core.get_user(m.from_user.id)):
        return
    await m.answer(await asyncio.to_thread(status_text))


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
    dp.message.register(cmd_import, Command("ritmo_import"))
    log.info("%s подключён: %s, локали %s", VERSION, ritmo_url() or "—", ", ".join(keys()) or "—")
