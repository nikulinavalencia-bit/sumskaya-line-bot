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

# через сколько секунд после старта возвращать кнопку «Меню».
# Ставим несколько раз: модуль архива сотрудников ставит свою кнопку WebApp,
# и кто последний — того и кнопка. Последняя попытка через две минуты после
# старта — к этому моменту все модули точно отработали.
try:
    MENU_RETRIES = tuple(int(x) for x in
                         os.environ.get("MENU_BUTTON_DELAY", "8,25,60,120").split(",") if x.strip())
except Exception:
    MENU_RETRIES = (8, 25, 60, 120)
MENU_DELAY = MENU_RETRIES[0] if MENU_RETRIES else 8

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
        ("🛡 Защита от лимита Google Sheets", _quota_on),
        ("🛡 Шапки листов проверяются пакетом", _headers_on),
    ]


def _quota_on() -> bool:
    try:
        from gspread.http_client import HTTPClient
        return bool(getattr(HTTPClient.request, "_quota_guard", False))
    except Exception:
        return False


def _headers_on() -> bool:
    return bool(getattr(getattr(core, "ensure_headers", None), "_fin_guard", False))


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


async def menu_status(chat_id) -> str:
    """Что за кнопка стоит слева от поля ввода прямо сейчас — по данным Telegram."""
    names = {"commands": "«Меню» (команды) — как надо",
             "web_app": "кнопка веб-приложения — она перебивает «Меню»",
             "default": "по умолчанию"}
    try:
        b = await core.bot.get_chat_menu_button(chat_id=chat_id)
        kind = getattr(b, "type", "?")
        label = names.get(kind, kind)
        text = getattr(b, "text", None)
        if text:
            label += f" — «{text}»"
        line = f"Кнопка слева от поля ввода: {label}"
    except Exception as ex:
        line = f"Кнопку проверить не удалось: {type(ex).__name__}"
    try:
        cmds = await core.bot.get_my_commands()
        line += "\nКоманды бота: " + (", ".join("/" + c.command for c in cmds) or "— пусто")
    except Exception:
        pass
    return line


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
        text = report()
        try:
            text += "\n\n" + await menu_status(m.chat.id)
        except Exception:
            pass
        await m.answer(text[:4000])
    except Exception as ex:
        log.exception("отчёт не собрался: %s", ex)
        await m.answer(f"Не смог собрать отчёт: <code>{type(ex).__name__}: {ex}</code>")


def _commands() -> list:
    """Список команд бота. Собираем по тому, какие модули реально подключены."""
    cmds = [BotCommand(command="start", description="🔄 Обновить / открыть меню"),
            BotCommand(command="menu", description="🔘 Вернуть кнопку «Меню»"),
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


def _menu_ids() -> set:
    """Чаты, где кнопку правим персонально: патроны и фин. директор."""
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
    return ids


async def set_menu_button() -> int:
    """Ставит кнопку «Меню» (команды) глобально и по чатам. Возвращает число чатов.

    Кнопка показывается только если у бота есть команды, поэтому список команд
    обновляем тем же заходом.
    """
    try:
        from aiogram.types import MenuButtonCommands
    except Exception as ex:
        log.warning("кнопка меню недоступна в этой версии aiogram: %s", ex)
        return 0
    try:
        await core.bot.set_my_commands(_commands())
    except Exception as ex:
        log.warning("не смог обновить список команд: %s", ex)
    try:
        await core.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as ex:
        log.warning("не смог поставить меню по умолчанию: %s", ex)
    ids = _menu_ids()
    for uid in ids:
        try:
            await core.bot.set_chat_menu_button(chat_id=uid,
                                                menu_button=MenuButtonCommands())
        except Exception as ex:
            log.warning("кнопка меню для %s не поставлена: %s", uid, ex)
    return len(ids)


async def _restore_menu_button():
    """Возвращаем кнопку «Меню» слева от поля ввода.

    Модуль веб-архива сотрудников ставит на её место кнопку WebApp, и команды
    становятся недоступны. Побеждает тот, кто поставил последним, а порядок
    запуска модулей зависит от того, как быстро отвечают таблицы. Поэтому
    ставим несколько раз с растущей паузой — последняя попытка заведомо позже
    всех остальных модулей. Выключается переменной MENU_BUTTON.
    """
    if os.environ.get("MENU_BUTTON", "commands").strip().lower() != "commands":
        return
    prev = 0
    for delay in MENU_RETRIES:
        await asyncio.sleep(max(delay - prev, 0))
        prev = delay
        try:
            n = await set_menu_button()
            log.info("selfcheck: кнопка «Меню» поставлена (через %d сек, чатов: %d)", delay, n)
        except Exception as ex:
            log.warning("selfcheck: попытка вернуть меню не удалась: %s", ex)


async def cmd_menu(m: Message):
    """Ручной возврат кнопки «Меню», если её опять перебили."""
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
        await set_menu_button()
    except Exception as ex:
        await m.answer(f"Не получилось: <code>{type(ex).__name__}: {ex}</code>")
        return
    await m.answer("Кнопка «Меню» возвращена.\n\n"
                   "Если слева от поля ввода её всё ещё нет — закрой и открой чат "
                   "с ботом: Telegram показывает кнопку по своей памяти и обновляет "
                   "её при входе в чат.")


def setup(dp, core_module):
    global core
    core = core_module
    dp.message.register(cmd_version, Command("version"))
    dp.message.register(cmd_menu, Command("menu"))
    try:
        dp.startup.register(_on_startup)
    except Exception as ex:
        log.warning("startup-хук недоступен: %s", ex)
    log.info("selfcheck подключён: /version, /menu")
