# -*- coding: utf-8 -*-
"""
/version — бот сам рассказывает, какая версия кода залита на сервер.

Отдельный файл, ничего в боте не меняет. Подключается одной строкой рядом с
финблоком (см. README_fin.md). Смотрит на живой код прямо в памяти процесса,
поэтому отвечает не «как должно быть», а как есть на сервере прямо сейчас.

Команда доступна патрону и фин. директору.
"""

import ast
import sys
import asyncio
import logging
import os
from datetime import datetime

# через сколько секунд после старта возвращать кнопку «Меню»
MENU_DELAY = int(os.environ.get("MENU_BUTTON_DELAY", "8") or 8)

from aiogram.filters import Command
from aiogram.types import Message, BotCommand

log = logging.getLogger("selfcheck")

core = None
STARTED_AT = None


def _src() -> str:
    """Исходник файла бота целиком — читаем прямо с диска сервера."""
    try:
        path = getattr(core, "__file__", None)
        if not path:
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as ex:
        log.warning("не смог прочитать исходник: %s", ex)
        return ""


def _func_src(whole: str, name: str) -> str:
    """Кусок исходника одной функции по имени — через разбор синтаксиса,
    без зависимости от того, как модуль загружен."""
    try:
        tree = ast.parse(whole)
    except Exception:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            try:
                return ast.get_source_segment(whole, node) or ""
            except Exception:
                return ""
    return ""


def _has(name: str) -> bool:
    return hasattr(core, name)


# Проверки: (что проверяем, функция -> bool, что это за пункт договорённостей)
def build_checks():
    whole = _src()

    def registro_last_row():
        s = _func_src(whole, "registro_append")
        return bool(s) and "registro_last_filled_row" in s

    def empleados_manual():
        # уведомление должно стоять именно в ручном продвижении этапа (cb_hr_next),
        # а не только в автоматическом поиске контракта по почте
        s = _func_src(whole, "cb_hr_next")
        return bool(s) and "notify_empleados" in s

    def locales_ok():
        tags = {str(v.get("tag", "")).strip() for v in getattr(core, "LOCALES", {}).values()}
        return {"REINA", "FRANCIA", "BAKERY", "BOI BOI"} <= tags

    def depts_ok():
        d = getattr(core, "DEPARTAMENTOS", [])
        return len(d) == 10

    return [
        ("HR 1a. Уведомление в Empleados по почте (авто)",
         lambda: "notify_empleados" in _func_src(whole, "notify_new_contracts")),
        ("HR 1б. Уведомление в Empleados при ручном этапе", empleados_manual),
        ("HR 2. Подсказка пола по имени", lambda: _has("guess_sex")),
        ("HR 3. Локали Registro (REINA/FRANCIA/BAKERY/BOI BOI)", locales_ok),
        ("HR 4. Departamento — список из 10 значений", depts_ok),
        ("HR 5. Puesto приводится к тексту из списка", lambda: _has("normalize_puesto")),
        ("HR 6. Tipo de contrato = Contratado Nuevo зашит", lambda: "Contratado Nuevo" in whole),
        ("HR 7. Колонка «Fecha Nacimiento Titular»", lambda: "Fecha Nacimiento Titular" in whole),
        ("HR 8. Запись под последнюю заполненную строку", registro_last_row),
        ("HR 7б. Кириллица в заголовках Registro не мешает",
         lambda: "_CYR_LOOKALIKE" in whole),
        ("HR 10. Уведомление о новой заявке Solicitud",
         lambda: bool(getattr(sys.modules.get("solicitud_watch"), "core", None))),
        # 11 и 12: пока опрос спрашивает эти поля руками — значит, правки нет
        ("HR 11. Departamento определяется по должности",
         lambda: "2) Departamento" not in whole),
        ("HR 12. Fecha de Alta берётся из заявки",
         lambda: "Fecha de Alta — дд.мм.гггг" not in whole),
        ("💶 Финблок подключён",
         lambda: bool(getattr(sys.modules.get("fin_block"), "core", None))),
        ("💶 Справочник поставщиков с правкой в боте",
         lambda: hasattr(sys.modules.get("fin_block"), "cb_prov_list")),
        ("🔍 Распознавание фактур включено (есть ключ Gemini)", _ocr_on),
    ]


def _ocr_on() -> bool:
    try:
        import invoice_ocr
        return invoice_ocr.enabled()
    except Exception:
        return False


def _sheets_line() -> str:
    try:
        titles = [w.title for w in core._all_sheets()]
    except Exception as ex:
        return f"Листы таблицы: не смог прочитать ({type(ex).__name__})"
    fin = [t for t in titles if t.startswith("FIN_")]
    return (f"Листов в таблице: {len(titles)}\n"
            f"Листы финблока: {', '.join(fin) if fin else '— ещё не созданы'}")


def report() -> str:
    lines = ["🧾 <b>Что залито на сервер</b>", ""]

    path = getattr(core, "__file__", "") or ""
    try:
        mt = datetime.fromtimestamp(os.path.getmtime(path), core.TZ)
        lines.append(f"Файл бота: <code>{os.path.basename(path)}</code> от {mt.strftime('%d.%m.%Y %H:%M')}")
    except Exception:
        lines.append(f"Файл бота: <code>{os.path.basename(path) or '?'}</code>")
    if STARTED_AT:
        lines.append(f"Бот запущен: {STARTED_AT.strftime('%d.%m.%Y %H:%M')}")
    lines.append(f"Python {sys.version.split()[0]}")
    lines.append("")

    ok = bad = 0
    for title, fn in build_checks():
        try:
            good = bool(fn())
        except Exception as ex:
            log.warning("проверка «%s» упала: %s", title, ex)
            good = False
        lines.append(f"{'✅' if good else '❌'} {title}")
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)

    lines.append("")
    lines.append(f"Итого: залито <b>{ok}</b>, не хватает <b>{bad}</b>")
    lines.append("")
    lines.append(_sheets_line())
    lines.append("")
    lines.append("❌ — этой правки в залитом коде нет. ✅ — она на сервере.")
    return "\n".join(lines)[:4000]


async def cmd_version(m: Message):
    u = core.get_user(m.from_user.id)
    allowed = core.is_patron(u)
    try:
        import fin_block
        allowed = allowed or fin_block.is_findir(u, m.from_user.id)
    except Exception:
        pass
    if not allowed:
        return
    try:
        await m.answer(report())
    except Exception as ex:
        log.exception("отчёт не собрался: %s", ex)
        await m.answer(f"Не смог собрать отчёт: <code>{type(ex).__name__}: {ex}</code>")


def _commands() -> list:
    """Список команд бота. Собираем по тому, какие модули реально подключены."""
    cmds = [BotCommand(command="start", description="🔄 Обновить / открыть меню"),
            BotCommand(command="version", description="🧾 Что залито на сервер")]
    if "hr_web" in sys.modules:
        cmds.insert(1, BotCommand(command="archivo",
                                  description="👥 Архив сотрудников"))
    return cmds


async def _on_startup():
    global STARTED_AT
    STARTED_AT = core.now_local()
    try:
        await core.bot.set_my_commands(_commands())
    except Exception as ex:
        log.warning("не смог обновить список команд: %s", ex)
    asyncio.create_task(_restore_menu_button())


async def _restore_menu_button():
    """Возвращаем кнопку «Меню» слева от поля ввода.

    Модуль веб-архива сотрудников ставит на её место кнопку WebApp, и команды
    становятся недоступны. Делаем это последним — с небольшой паузой, чтобы
    перекрыть чужую установку, и только если MENU_BUTTON=commands (по умолчанию).
    """
    if os.environ.get("MENU_BUTTON", "commands").strip().lower() != "commands":
        return
    await asyncio.sleep(MENU_DELAY)
    try:
        from aiogram.types import MenuButtonCommands
    except Exception as ex:
        log.warning("кнопка меню недоступна в этой версии aiogram: %s", ex)
        return
    try:
        await core.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as ex:
        log.warning("не смог поставить меню по умолчанию: %s", ex)
    # у кого кнопка уже была подменена — правим персонально
    ids = set()
    try:
        ids.update(core.patrons())
    except Exception:
        pass
    try:
        import fin_block
        ids.update(fin_block.findir_ids())
    except Exception:
        pass
    for uid in ids:
        try:
            await core.bot.set_chat_menu_button(chat_id=uid,
                                                menu_button=MenuButtonCommands())
        except Exception as ex:
            log.warning("кнопка меню для %s не поставлена: %s", uid, ex)
    log.info("selfcheck: кнопка «Меню» восстановлена для %d чатов", len(ids))


def setup(dp, core_module):
    global core
    core = core_module
    dp.message.register(cmd_version, Command("version"))
    try:
        dp.startup.register(_on_startup)
    except Exception as ex:
        log.warning("startup-хук недоступен: %s", ex)
    log.info("selfcheck подключён: /version")
