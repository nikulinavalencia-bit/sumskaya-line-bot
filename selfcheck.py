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
import logging
import os
from datetime import datetime

from aiogram.filters import Command
from aiogram.types import (Message, BotCommand, BotCommandScopeDefault,
                           BotCommandScopeChat, MenuButtonCommands)

log = logging.getLogger("selfcheck")

try:
    import ritmo_bridge
except Exception as _e:
    ritmo_bridge = None
    log.warning("ritmo_bridge не подключён: %s", _e)

core = None
STARTED_AT = None


def owner_ids() -> set:
    """Кто видит /version. Переменная Railway OWNER_IDS (через запятую);
    если не задана — все Патроны."""
    raw = os.environ.get("OWNER_IDS", "").replace(" ", "")
    ids = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    if ids:
        return ids
    try:
        return set(core.patrons())
    except Exception:
        return set()


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
        ("🌐 Веб-архив сотрудников подключён (/archivo)",
         lambda: bool(getattr(sys.modules.get("hr_web"), "M", None))),
        ("🌐 Веб-архив: сервер запущен и задан HR_WEB_URL",
         lambda: bool(getattr(sys.modules.get("hr_web"), "_runner", None))
         and bool(os.environ.get("HR_WEB_URL"))),
        ("📤 Файл для Control Laboral подключён (/controllaboral)",
         lambda: bool(getattr(sys.modules.get("cl_export"), "M", None))),
        ("🏖 Расчёт отпуска подключён (/vacaciones)",
         lambda: bool(getattr(sys.modules.get("vacaciones"), "M", None))),
        ("📝 Заявки Alta/Baja/Médico/Cambio подключены",
         lambda: bool(getattr(sys.modules.get("solicitudes"), "M", None))),
        ("🗂 Восстановление архива с Диска (/restaurar)",
         lambda: bool(getattr(sys.modules.get("drive_import"), "M", None))),
        ("📁 Папки сотрудников с Google Диска (DRIVE_ROOT_ID)",
         lambda: bool(getattr(sys.modules.get("drive_docs"), "M", None))
         and bool(os.environ.get("DRIVE_ROOT_ID"))),
        ("📧 Почта Histora по IMAP (пароль приложения)", _imap_on),
        ("🔗 Ritmo OPS подключён (мост фактур из групп)",
         lambda: bool(getattr(sys.modules.get("ritmo_bridge"), "core", None))),
    ]


def _imap_on() -> bool:
    try:
        mod = getattr(core, "mail_imap", None) or sys.modules.get("mail_imap")
        return bool(mod and mod.enabled())
    except Exception:
        return False


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


async def cmd_chatid(m: Message):
    """/chatid — показать ID текущего чата (для заполнения листа Groups).
    Отвечает только патронам/владельцам, чтобы не шуметь в группах сотрудникам."""
    try:
        if m.from_user.id not in owner_ids() and not core.is_patron(core.get_user(m.from_user.id)):
            return
    except Exception:
        return
    kind = "группа" if m.chat.type in ("group", "supergroup") else "личка"
    await m.answer(f"ChatID: <code>{m.chat.id}</code>\nТип чата: {kind}")


async def cmd_version(m: Message):
    if m.from_user.id not in owner_ids():
        return
    try:
        await m.answer(report())
    except Exception as ex:
        log.exception("отчёт не собрался: %s", ex)
        await m.answer(f"Не смог собрать отчёт: <code>{type(ex).__name__}: {ex}</code>")


async def _on_startup():
    global STARTED_AT
    STARTED_AT = core.now_local()
    # Кнопка «Меню»: всем — только «Обновить»; Патронам — рабочие команды;
    # «Что залито» — только владельцу (OWNER_IDS).
    start_cmd = BotCommand(command="start", description="🔄 Обновить / открыть меню")
    work = [
        BotCommand(command="archivo", description="🌐 Архив сотрудников"),
        BotCommand(command="vacaciones", description="🏖 Расчёт отпуска"),
        BotCommand(command="controllaboral", description="📤 Файл для Control Laboral"),
    ]
    try:
        await core.bot.set_my_commands([start_cmd], scope=BotCommandScopeDefault())
    except Exception as ex:
        log.warning("не смог обновить список команд: %s", ex)
    owners = owner_ids()
    try:
        patrons = set(core.patrons())
    except Exception:
        patrons = set()
    managers = set()
    try:
        for r in core.rows(core.USERS_WS, force=True):
            if (str(r.get("Роль", "")).strip() == core.ROLE_MANAGER
                    and str(r.get("Статус", "")).strip() == "active"):
                managers.add(int(str(r.get("ID")).strip()))
    except Exception as ex:
        log.warning("не смог собрать управляющих: %s", ex)
    archivo = BotCommand(command="archivo", description="📝 Заявки и архив")
    for pid in patrons | owners | managers:
        if pid in patrons:
            cmds = [start_cmd] + work
        elif pid in managers:
            cmds = [start_cmd, archivo]
        else:
            cmds = [start_cmd]
        if pid in owners:
            cmds.append(BotCommand(command="version", description="🧾 Что залито на сервер"))
        try:
            await core.bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=pid))
            # вернуть кнопку «Меню» на место (её временно занимал «Архив»)
            await core.bot.set_chat_menu_button(chat_id=pid, menu_button=MenuButtonCommands())
        except Exception as ex:
            log.warning("команды для %s не выставлены: %s", pid, ex)


def setup(dp, core_module):
    global core
    core = core_module
    dp.message.register(cmd_version, Command("version"))
    dp.message.register(cmd_chatid, Command("chatid"))
    try:
        dp.startup.register(_on_startup)
    except Exception as ex:
        log.warning("startup-хук недоступен: %s", ex)
    if ritmo_bridge:
        try:
            ritmo_bridge.setup(dp, core_module)
        except Exception as ex:
            log.error("ritmo_bridge.setup() не запустился: %s", ex, exc_info=True)
    log.info("selfcheck подключён: /version")
