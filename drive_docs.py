# =========================================================
#  drive_docs.py — папки сотрудников с Google Диска в веб-архиве
#
#  На Диске лежит папка «CONTRATOS de TRABAJO» → подпапки по локалям
#  (REINA, Francia, Bakary, Oficina…) → папка на каждого сотрудника с его
#  документами. Модуль читает это дерево сервисным аккаунтом бота и
#  подшивает файлы в карточку сотрудника на сайте: список документов,
#  ссылка на папку и кнопка «прислать в Telegram».
#
#  Что нужно один раз:
#   1) открыть папку «CONTRATOS de TRABAJO» на Диске для сервисного
#      аккаунта бота (роль «Читатель»):
#      techcards-bot@techcards-506711.iam.gserviceaccount.com
#   2) в Railway задать DRIVE_ROOT_ID — id папки из её адреса:
#      drive.google.com/drive/folders/<ЭТО_ID>
#
#  Права: только чтение (drive.readonly), бот ничего не меняет на Диске.
#
#  Подключение в bot.py (после hr_web/solicitudes):
#      try:
#          import sys as _sys
#          import drive_docs
#          drive_docs.setup(dp, _sys.modules[__name__])
#      except Exception as _e:
#          log.error("drive_docs не подключён: %s", _e, exc_info=True)
# =========================================================

import os
import time
import logging
import unicodedata

import requests

log = logging.getLogger("sumskaya.drive_docs")

VERSION = "drive_docs 1.1 · 23.09.2026"

M = None
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
API = "https://www.googleapis.com/drive/v3/files"
CACHE_TTL = 15 * 60

_creds = None
_tree = {"ts": 0, "folders": []}     # [{id, name, path, files:[...]}]


def root_id() -> str:
    return os.environ.get("DRIVE_ROOT_ID", "").strip()


def enabled() -> bool:
    return bool(root_id())


# ---------------- ДИСК ----------------

def _token() -> str:
    global _creds
    from google.oauth2.service_account import Credentials
    import google.auth.transport.requests as gar
    if _creds is None:
        _creds = Credentials.from_service_account_info(M.GOOGLE_CREDS, scopes=SCOPES)
    if not _creds.valid or _creds.expired:
        _creds.refresh(gar.Request())
    return _creds.token


def _list(parent: str) -> list:
    """Содержимое папки: [{id, name, mimeType, modifiedTime, webViewLink}]."""
    out, page = [], None
    while True:
        params = {
            "q": f"'{parent}' in parents and trashed=false",
            "fields": "nextPageToken, files(id,name,mimeType,modifiedTime,webViewLink,size)",
            "pageSize": 200, "orderBy": "name",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        r = requests.get(API, params=params,
                         headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Drive {r.status_code}: {r.text[:200]}")
        data = r.json()
        out.extend(data.get("files", []))
        page = data.get("nextPageToken")
        if not page:
            return out


FOLDER = "application/vnd.google-apps.folder"
MAX_DEPTH = 5   # локаль → (категория: Cocina/Bar/Camareros/…) → сотрудник —
                # вложенность разная в разных локалях, поэтому спускаемся,
                # пока в папке есть подпапки, а не фиксируем число уровней


def _scan_folder(node: dict, local_name: str, depth: int, out: list):
    """Лист (папка без вложенных папок) — это папка сотрудника, на каком бы
    уровне она ни лежала. Папка с подпапками — категория, спускаемся глубже."""
    items = _list(node["id"])
    subfolders = [it for it in items if it.get("mimeType") == FOLDER]
    files = [it for it in items if it.get("mimeType") != FOLDER]
    if subfolders and depth < MAX_DEPTH:
        for sf in subfolders:
            _scan_folder(sf, local_name, depth + 1, out)
        return
    out.append({
        "id": node["id"], "name": node["name"], "local": local_name,
        "link": node.get("webViewLink", ""),
        "files": [{"id": f["id"], "name": f["name"],
                   "fecha": str(f.get("modifiedTime", ""))[:10],
                   "link": f.get("webViewLink", "")} for f in files],
    })


def _scan_sync() -> list:
    """Дерево «локаль → (категория →)* сотрудник → файлы». Между локалью и
    сотрудником может быть промежуточная папка-категория (Cocina, Bar,
    Camareros, Manager, Ayudantes, Limpieza, DESPEDIDO…) — глубина не
    фиксирована, поэтому спускаемся, пока встречаются подпапки."""
    folders = []
    for lvl1 in _list(root_id()):
        if lvl1.get("mimeType") != FOLDER:
            continue
        for lvl2 in _list(lvl1["id"]):
            if lvl2.get("mimeType") != FOLDER:
                continue
            _scan_folder(lvl2, lvl1["name"], 2, folders)
    log.info("drive_docs: найдено папок сотрудников — %d", len(folders))
    return folders


def tree(force=False) -> list:
    if not enabled():
        return []
    if not force and _tree["folders"] and time.time() - _tree["ts"] < CACHE_TTL:
        return _tree["folders"]
    try:
        _tree["folders"] = _scan_sync()
        _tree["ts"] = time.time()
    except Exception as e:
        log.error("drive_docs: не прочитала Диск: %s", e)
        if not _tree["folders"]:
            raise
    return _tree["folders"]


def _norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.replace("_", " ").replace("-", " ").replace(".", " ").split())


def folder_for(name: str):
    """Папка сотрудника по ФИО: совпадение по словам имени и фамилии."""
    want = {w for w in _norm(name).split() if len(w) > 2}
    if not want:
        return None
    best, score = None, 0
    for f in tree():
        have = {w for w in _norm(f["name"]).split() if len(w) > 2}
        s = len(want & have)
        if s > score:
            best, score = f, s
    return best if score >= 2 or (score == 1 and len(want) == 1) else None


def docs_for(name: str) -> dict:
    """Для карточки сотрудника: ссылка на папку и список файлов."""
    if not enabled():
        return {}
    try:
        f = folder_for(name)
    except Exception as e:
        return {"error": str(e)}
    if not f:
        return {}
    return {"folder": f["name"], "local": f["local"], "link": f["link"],
            "files": f["files"]}


def download(file_id: str) -> bytes:
    r = requests.get(f"{API}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                     headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Drive {r.status_code}: {r.text[:200]}")
    return r.content


# ---------------- ВЕБ ----------------

def _routes(app):
    from aiohttp import web
    import asyncio
    import hr_web

    async def send(request):
        uid = app["uid_from"](request)
        if not uid or hr_web.user_role(uid) != "patron":
            return web.json_response({"error": "auth"}, status=401)
        body = await request.json()
        fid, fname = str(body.get("id", "")), str(body.get("name", "файл"))
        if not fid:
            return web.json_response({"error": "нет файла"}, status=400)
        asyncio.create_task(_send_tg(uid, fid, fname))
        return web.json_response({"ok": True})

    async def rescan(request):
        uid = app["uid_from"](request)
        if not uid or hr_web.user_role(uid) != "patron":
            return web.json_response({"error": "auth"}, status=401)
        try:
            n = len(await asyncio.to_thread(tree, True))
            return web.json_response({"ok": True, "folders": n})
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    app.router.add_post("/hr/api/drive/send", send)
    app.router.add_post("/hr/api/drive/rescan", rescan)


async def _send_tg(uid: int, file_id: str, filename: str):
    import asyncio
    from aiogram.types import BufferedInputFile
    try:
        raw = await asyncio.to_thread(download, file_id)
        await M.bot.send_document(uid, BufferedInputFile(raw, filename=filename))
    except Exception as e:
        log.error("drive_docs: файл не отправлен: %s", e)
        try:
            await M.bot.send_message(uid, f"⚠️ Не удалось прислать {M.html_lib.escape(filename)}: {e}")
        except Exception:
            pass


def setup(dp, main_module):
    global M
    M = main_module
    import hr_web
    hr_web.EXTRA_ROUTES.append(_routes)
    if not enabled():
        log.warning("drive_docs: не задан DRIVE_ROOT_ID — папки с Диска не подключены")
    log.info("%s подключён", VERSION)
