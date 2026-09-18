# =========================================================
#  vacaciones.py — расчёт остатка отпуска при увольнении
#
#  Правило (Sumskaya Line): отпуск начисляется с даты подписания
#  договора (Fecha de Alta в Registro), 2,5 дня за каждый отработанный
#  месяц = 30 дней в год. Неполный месяц считается пропорционально дням.
#
#      остаток = отработано_месяцев × 2,5 − использовано_дней
#
#  В боте: HR → «🏖 Расчёт отпуска» или команда /vacaciones.
#   1) пишете часть имени → бот находит сотрудника в Registro;
#   2) бот показывает Alta и Baja (если Baja пустая — считает на сегодня);
#   3) вы пишете, сколько дней отпуска уже использовано → остаток.
#  Дату ухода можно указать вручную: «5 30.09.2026» = 5 дней использовано,
#  считать до 30.09.2026.
#
#  Читает Registro через hr_web (тот же разбор колонок, что в веб-архиве).
# =========================================================

import re
import logging
import calendar
from datetime import date, datetime, timedelta

log = logging.getLogger("sumskaya.vacaciones")

VERSION = "vacaciones 1.1 · 18.09.2026"

DAYS_PER_MONTH = 2.5

M = None
_awaiting = {}   # uid -> {"stage": "name"|"used", "row": int}


# ---------------- РАСЧЁТ ----------------

def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_worked(alta: date, last_day: date) -> float:
    """Месяцы с alta по last_day включительно; неполный месяц — долей по дням."""
    end = last_day + timedelta(days=1)
    if end <= alta:
        return 0.0
    full = 0
    while _add_months(alta, full + 1) <= end:
        full += 1
    start = _add_months(alta, full)
    nxt = _add_months(alta, full + 1)
    frac = (end - start).days / (nxt - start).days
    return full + frac


def calc(alta: date, last_day: date, used: float) -> dict:
    months = months_worked(alta, last_day)
    accrued = months * DAYS_PER_MONTH
    return {
        "months": months,
        "full_months": int(months),
        "extra_days": (last_day + timedelta(days=1) - _add_months(alta, int(months))).days,
        "accrued": round(accrued, 2),
        "used": used,
        "left": round(accrued - used, 2),
    }


def _num(s: str) -> str:
    v = f"{s:.2f}".rstrip("0").rstrip(".")
    return v.replace(".", ",")


def _parse_date(s: str):
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _fmt(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


# ---------------- ДАННЫЕ ----------------

async def _records():
    import hr_web
    if hr_web.M is None:
        hr_web.M = M
    data = await hr_web.load_data(force=True)
    return data["records"]


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


# ---------------- ТЕЛЕГРАМ ----------------

def _nav(rows=None):
    """Клавиатура: свои кнопки + ряд «Назад в HR / В начало»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    if hasattr(M, "nav_row"):
        nav = M.nav_row()
    else:
        nav = [InlineKeyboardButton(text="⬅️ Назад", callback_data="d:hr"),
               InlineKeyboardButton(text="🏠 В начало", callback_data="home")]
    return InlineKeyboardMarkup(inline_keyboard=(rows or []) + [nav])


def _is_patron(uid) -> bool:
    try:
        return M.is_patron(M.get_user(uid))
    except Exception:
        return False


async def _start(chat_id: int, uid: int):
    _awaiting[uid] = {"stage": "name"}
    await M.bot.send_message(
        chat_id,
        "🏖 <b>Расчёт отпуска</b>\n\nНапишите имя или фамилию сотрудника "
        "(можно часть) — найду в Registro.\n\n"
        "<i>Правило: 2,5 дня за месяц с даты подписания договора, 30 дней в год.</i>",
        reply_markup=_nav())


def setup(dp, main_module):
    global M
    M = main_module
    from aiogram import F
    from aiogram.filters import Command
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    @dp.message(Command("vacaciones"))
    async def cmd_vac(m):
        if m.chat.type != "private" or not _is_patron(m.from_user.id):
            return
        await _start(m.chat.id, m.from_user.id)

    @dp.callback_query(F.data == "vac")
    async def cb_vac(c):
        if not _is_patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        await _start(c.from_user.id, c.from_user.id)

    @dp.callback_query(F.data.startswith("vacr:"))
    async def cb_vac_row(c):
        if not _is_patron(c.from_user.id):
            await c.answer("Только Патрон", show_alert=True)
            return
        await c.answer()
        row = int(c.data.split(":")[1])
        recs = await _records()
        r = next((x for x in recs if x["row"] == row), None)
        if not r:
            await M.bot.send_message(c.from_user.id, "Не нашла эту строку в Registro, попробуйте ещё раз.",
                                     reply_markup=_nav())
            return
        _awaiting[c.from_user.id] = {"stage": "used", "row": row}
        esc = M.html_lib.escape
        baja = r.get("baja")
        baja_txt = (_fmt(date.fromisoformat(baja)) if baja
                    else f"не указана — считаю на сегодня ({_fmt(M.now_local().date())})")
        await M.bot.send_message(
            c.from_user.id,
            f"👤 <b>{esc(r['name'])}</b> · {esc(r.get('local', ''))} · {esc(r.get('puesto', ''))}\n"
            f"Fecha de Alta: <b>{_fmt(date.fromisoformat(r['alta'])) if r.get('alta') else '— нет в Registro'}</b>\n"
            f"Fecha de Baja: <b>{baja_txt}</b>\n\n"
            "Сколько дней отпуска уже <b>использовано</b>? Напишите число (например <code>7</code>).\n"
            "Другая дата ухода — через пробел: <code>7 30.09.2026</code>",
            reply_markup=_nav())

    @dp.message(F.chat.type == "private", F.text,
                lambda m: m.from_user and m.from_user.id in _awaiting
                and not m.text.startswith("/"))
    async def on_text(m):
        uid = m.from_user.id
        st = _awaiting.get(uid) or {}
        esc = M.html_lib.escape

        if st.get("stage") == "name":
            q = _norm(m.text)
            recs = await _records()
            found = [r for r in recs if q and q in _norm(r["name"])]
            if not found:
                await m.answer("Никого не нашла. Напишите по-другому (часть имени или фамилии).",
                               reply_markup=_nav())
                return
            found.sort(key=lambda r: (r.get("estado") != "baja", r["name"]))
            kb = [[InlineKeyboardButton(
                text=f"{'🔴' if r.get('estado') == 'baja' else '🟢'} {r['name']} · {r.get('local', '')}"[:60],
                callback_data=f"vacr:{r['row']}")] for r in found[:20]]
            await m.answer(f"Нашла {len(found)}. Выберите:" + (" (показаны первые 20)" if len(found) > 20 else ""),
                           reply_markup=_nav(kb))
            return

        if st.get("stage") == "used":
            parts = m.text.replace(",", ".").split()
            try:
                used = float(parts[0])
            except (ValueError, IndexError):
                await m.answer("Нужно число — сколько дней отпуска использовано. Например <code>7</code>.",
                               reply_markup=_nav())
                return
            override = _parse_date(parts[1]) if len(parts) > 1 else None
            if len(parts) > 1 and not override:
                await m.answer("Дата не распознана, формат дд.мм.гггг. Например <code>7 30.09.2026</code>.",
                               reply_markup=_nav())
                return
            recs = await _records()
            r = next((x for x in recs if x["row"] == st.get("row")), None)
            if not r or not r.get("alta"):
                _awaiting.pop(uid, None)
                await m.answer("У сотрудника нет Fecha de Alta в Registro — посчитать не могу. "
                               "Заполните дату в таблице и попробуйте снова.", reply_markup=_nav())
                return
            alta = date.fromisoformat(r["alta"])
            if override:
                last, src = override, "указана вручную"
            elif r.get("baja"):
                last, src = date.fromisoformat(r["baja"]), "Fecha de Baja из Registro"
            else:
                last, src = M.now_local().date(), "сегодня (Baja не указана)"
            if last < alta:
                await m.answer("Дата ухода раньше даты прихода — проверьте даты.", reply_markup=_nav())
                return
            res = calc(alta, last, used)
            _awaiting.pop(uid, None)
            sign = "🟢" if res["left"] >= 0 else "🔴"
            tail = ("к выплате" if res["left"] >= 0
                    else "использовано больше, чем начислено — перерасход")
            await m.answer(
                f"🏖 <b>Отпуск — {esc(r['name'])}</b>\n\n"
                f"Пришёл: {_fmt(alta)}\n"
                f"Ушёл: {_fmt(last)} ({src})\n"
                f"Отработано: {res['full_months']} мес. {res['extra_days']} дн. "
                f"(= {_num(res['months'])} мес.)\n\n"
                f"Начислено: {_num(res['months'])} × 2,5 = <b>{_num(res['accrued'])}</b> дн.\n"
                f"Использовано: <b>{_num(res['used'])}</b> дн.\n"
                f"{sign} Остаток: <b>{_num(res['left'])}</b> дн. — {tail}",
                reply_markup=_nav([[InlineKeyboardButton(text="🔁 Посчитать ещё", callback_data="vac")]]))
            return

    log.info("%s подключён", VERSION)
