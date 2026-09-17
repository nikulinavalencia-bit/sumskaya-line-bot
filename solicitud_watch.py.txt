# -*- coding: utf-8 -*-
"""
🆕 Уведомление о новой заявке Solicitud (пункт 10 договорённостей по HR).

Отдельный файл, HR-код не трогает. Раз в несколько минут заглядывает в таблицу
анкет и, если появилась новая строка, пишет в личку: кто, на какую должность,
в какую локаль и что делать дальше.

Подключается одной строкой рядом с финблоком (см. README_fin.md).

Настройки через переменные Railway (все необязательные):
    HR_NOTIFY_IDS        — кому слать, Telegram ID через запятую.
                           Не задано — уходит всем активным патронам.
    HR_WATCH_INTERVAL    — период проверки в секундах, по умолчанию 300 (5 минут).
    HR_NOTIFY_ON_START   — 1, если после перезапуска нужно сообщить про заявки,
                           пришедшие, пока бот лежал. По умолчанию выключено:
                           при старте бот просто запоминает, что уже есть.
"""

import os
import html
import asyncio
import logging

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("solicitud")

core = None
_seen = set()          # ключи строк анкет, про которые уже знаем
_started = False

NOTIFY_IDS = [int(x) for x in os.environ.get("HR_NOTIFY_IDS", "").replace(" ", "").split(",")
              if x.isdigit()]
INTERVAL = int(os.environ.get("HR_WATCH_INTERVAL", "300") or 300)
NOTIFY_ON_START = os.environ.get("HR_NOTIFY_ON_START", "").strip() in ("1", "true", "yes", "да")


def e(s) -> str:
    return html.escape(str(s or ""))


def targets() -> list:
    if NOTIFY_IDS:
        return NOTIFY_IDS
    try:
        return core.patrons()
    except Exception as ex:
        log.warning("не смог получить список патронов: %s", ex)
        return []


def applicant_rows() -> list:
    """[(ключ строки, запись)] — ключ тот же, что в чек-листе HR: номер строки."""
    out = []
    for idx, r in enumerate(core.applicants_rows(force=True), start=2):
        out.append((str(idx), r))
    return out


def card(r: dict) -> tuple:
    """Текст уведомления и код локали."""
    nombre = str(r.get("Nombre", "")).strip()
    apellido = str(r.get("Apellido", "")).strip()
    puesto = str(r.get("Título profesional", "")).strip()
    horas = str(r.get("Número de horas bajo contrato", "")).strip()
    inicio = str(r.get("Fecha de inicio", "")).strip()
    local_raw = str(r.get("Local", "")).strip()

    try:
        loc = core.match_locale_code(local_raw)
    except Exception:
        loc = None
    loc_label = local_raw
    if loc and loc in core.LOCALES:
        l = core.LOCALES[loc]
        loc_label = f"{l['emoji']} {l['name']}"

    lines = [
        "🆕 <b>Новая заявка Solicitud</b>",
        "",
        f"<b>{e(f'{nombre} {apellido}'.strip()) or 'Без имени'}</b>",
        f"Локаль: <b>{e(loc_label) or '—'}</b>",
    ]
    if puesto:
        lines.append(f"Должность: {e(puesto)}" + (f" · {e(horas)} ч" if horas else ""))
    if inicio:
        lines.append(f"Начало работы: {e(inicio)}")
    tel = str(r.get("Teléfono", "")).strip()
    if tel:
        lines.append(f"Телефон: {e(tel)}")
    lines += [
        "",
        "Что дальше: открыть заявку, проверить документы и добавить кандидата "
        "в чек-лист оформления — дальше бот поведёт по этапам.",
    ]
    return "\n".join(lines), loc


def keyboard(loc) -> InlineKeyboardMarkup:
    data = f"hrn:{loc}" if loc else "hrn"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть новые заявки", callback_data=data)]])


async def notify(r: dict):
    text, loc = card(r)
    kb = keyboard(loc)
    for uid in targets():
        try:
            await core.bot.send_message(uid, text, reply_markup=kb)
        except Exception as ex:
            log.warning("уведомление о заявке не ушло %s: %s", uid, ex)


async def check_once(first_run: bool = False) -> int:
    """Возвращает, сколько новых заявок нашли."""
    try:
        items = applicant_rows()
    except Exception as ex:
        log.warning("не смог прочитать таблицу анкет: %s", ex)
        return 0

    fresh = [(k, r) for k, r in items if k not in _seen]
    for k, _ in items:
        _seen.add(k)

    if first_run and not NOTIFY_ON_START:
        log.info("solicitud_watch: на старте в таблице %d заявок, запомнил без уведомлений",
                 len(items))
        return 0

    for _, r in fresh:
        await notify(r)
    if fresh:
        log.info("solicitud_watch: новых заявок %d", len(fresh))
    return len(fresh)


async def loop():
    await check_once(first_run=True)
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await check_once()
        except Exception as ex:
            log.exception("solicitud_watch: цикл упал, продолжаю: %s", ex)


async def _on_startup():
    global _started
    if _started:
        return
    _started = True
    asyncio.create_task(loop())
    log.info("solicitud_watch запущен, проверка раз в %d сек", INTERVAL)


def setup(dp, core_module):
    global core
    core = core_module
    try:
        dp.startup.register(_on_startup)
    except Exception as ex:
        log.warning("startup-хук недоступен, слежение за заявками не включено: %s", ex)
        return
    log.info("solicitud_watch подключён")
