"""NEUTRON · бот обліку оплачених уроків.

Керування — кнопками. Вводити ID не потрібно: учня або групу обираєш
натисканням, а в будь-який момент можна просто написати частину імені.
"""
import asyncio
import csv
import html
import io
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from telegram import (  # noqa: E402
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import BadRequest, Conflict  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from database.db import backup_db, init_db, name_key  # noqa: E402
from database.groups import (  # noqa: E402
    create_group,
    get_all_groups,
    get_archived_groups,
    get_group_by_id,
    rename_group,
    set_group_active,
    update_group_schedule,
)
from database.lessons import (  # noqa: E402
    cancel_lesson,
    complete_group_lesson,
    complete_individual_lesson,
    get_group_attendance,
    get_lesson,
    get_student_attendance_stats,
    get_student_lessons,
    lessons_on_date,
    set_attendance,
)
from database.payments import (  # noqa: E402
    add_payment,
    cancel_payment,
    get_low_balance_students,
    get_month_report,
    get_payment,
    get_payments_by_student,
    suggest_packages,
)
from database.settings import get_setting, set_setting  # noqa: E402
from database.students import (  # noqa: E402
    create_group_student,
    create_individual_student,
    get_all_students,
    get_inactive_students,
    get_individual_students,
    get_student_by_id,
    get_students_by_group_id,
    make_student_individual,
    move_student_to_group,
    rename_student,
    search_students_by_name,
    set_student_active,
    update_individual_schedule,
    update_student_balance,
    update_student_notes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = ZoneInfo("Europe/Kyiv")

# True — на груповому занятті всі одразу позначені присутніми, знімаєш відсутніх.
# False — ніхто не позначений, відмічаєш присутніх.
DEFAULT_ALL_PRESENT = True

# Залишок, з якого учень потрапляє в «Баланси» та автозведення.
LOW_BALANCE = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BACKGROUND_TASK_KEY = "background_task"

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]
DAY_ALIASES = {
    "понеділок": 0, "пн": 0, "пон": 0, "monday": 0, "mon": 0,
    "вівторок": 1, "вт": 1, "вів": 1, "tuesday": 1, "tue": 1,
    "середа": 2, "ср": 2, "сер": 2, "wednesday": 2, "wed": 2,
    "четвер": 3, "чт": 3, "чет": 3, "thursday": 3, "thu": 3,
    "п'ятниця": 4, "пятниця": 4, "пт": 4, "friday": 4, "fri": 4,
    "субота": 5, "сб": 5, "суб": 5, "saturday": 5, "sat": 5,
    "неділя": 6, "нд": 6, "нед": 6, "sunday": 6, "sun": 6,
}

MENU = [
    ["✅ Провести урок", "💳 Оплата"],
    ["👥 Учні", "🗂 Групи"],
    ["📊 Відвідуваність", "⚠️ Баланси"],
    ["📈 Звіт", "⚙️ Налаштування"],
]

# Кнопки старої версії бота: якщо в Telegram ще висить стара клавіатура,
# натискання просто відкриє нове меню.
OLD_BUTTONS = {
    "Учні", "Групи", "Оплати", "Уроки", "Нагадування", "Швидкі дії", "Пошук і фільтри",
    "Назад", "Додати групу", "Список груп", "Додати учня", "Список учнів", "Картка учня",
    "Історія оплат", "Змінити баланс", "Деактивувати учня", "Перевести в групу",
    "Змінити розклад", "Додати оплату", "Учні з малим залишком", "Індивідуальний урок",
    "Групове заняття", "Загальне зведення", "Індивідуальні нагадування",
    "Групові нагадування", "Боржники", "Увімкнути автонагадування",
    "Вимкнути автонагадування", "Змінити час автонагадування", "Сьогоднішні уроки",
    "Швидка оплата", "Швидке індивідуальне списання", "Швидке групове списання",
    "Пошук учня", "Учні групи", "Неактивні учні", "Проблемні баланси",
}


# ───────────────────────── Допоміжні функції ─────────────────────────

def now() -> datetime:
    return datetime.now(TIMEZONE)


def stamp(day: date | None = None) -> str:
    """Дата-час для запису в базу (київський час)."""
    current = now()
    if day is None or day == current.date():
        return current.strftime("%Y-%m-%d %H:%M:%S")
    return f"{day.isoformat()} 12:00:00"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([row for row in rows if row])


def chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def parse_days(text: str | None) -> list[int]:
    """'Понеділок', 'пн, ср', 'Пн/Чт' → [0], [0, 2], [0, 3]."""
    if not text:
        return []
    cleaned = text.lower().replace("ʼ", "'").replace("’", "'").replace("`", "'")
    found = []
    for token in re.split(r"[\s,;/.+]+", cleaned):
        day = DAY_ALIASES.get(token)
        if day is not None and day not in found:
            found.append(day)
    return sorted(found)


def format_days(days) -> str:
    return ", ".join(DAYS[d] for d in sorted(days))


def schedule_text(day_text: str | None, time_text: str | None) -> str:
    days = parse_days(day_text)
    day_part = format_days(days) if days else (day_text or "")
    result = f"{day_part} {time_text or ''}".strip()
    return result or "розклад не задано"


def parse_time(text: str) -> str | None:
    match = re.fullmatch(r"\s*(\d{1,2})(?:[:.\-\s]?(\d{2}))?\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def time_key(time_text: str | None) -> str:
    return parse_time(time_text or "") or "99:99"


def fmt_day(day: date) -> str:
    today = now().date()
    label = f"{day:%d.%m} ({DAYS[day.weekday()]})"
    if day == today:
        return f"сьогодні, {label}"
    if day == today - timedelta(days=1):
        return f"вчора, {label}"
    return label


def fmt_date(value: str | None, with_year: bool = True) -> str:
    """'2026-11-11 16:00:00' → '11.11.2026'."""
    if not value:
        return "—"
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    return parsed.strftime("%d.%m.%Y" if with_year else "%d.%m")


def num(amount: float) -> str:
    """Число для callback_data без втрати точності: 2400.0 → '2400'."""
    return str(int(amount)) if float(amount).is_integer() else repr(float(amount))


def fmt_money(amount: float) -> str:
    if float(amount).is_integer():
        return f"{int(amount):,}".replace(",", " ")
    return f"{amount:,.2f}".replace(",", " ")


def month_title(month: str) -> str:
    year, number = month.split("-")
    return f"{MONTHS[int(number) - 1]} {year}"


def shift_month(month: str, delta: int) -> str:
    year, number = map(int, month.split("-"))
    index = year * 12 + (number - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def current_month() -> str:
    return now().strftime("%Y-%m")


def bal_icon(balance: int) -> str:
    if balance < 0:
        return "🔴"
    if balance == 0:
        return "🟠"
    if balance <= LOW_BALANCE:
        return "🟡"
    return "🟢"


def bal_text(balance: int) -> str:
    if balance < 0:
        return f"борг {abs(balance)}"
    if balance == 0:
        return "уроки закінчились"
    return f"залишок {balance}"


def student_button(student, target: str) -> InlineKeyboardButton:
    balance = student["remaining_lessons"]
    return btn(f"{bal_icon(balance)} {student['name']} · {balance}", f"{target}:{student['id']}")


def student_place(student) -> str:
    if student["student_type"] == "group":
        return student["group_name"] or "без групи"
    return "індивідуально"


async def show(update: Update, text: str, rows=None) -> None:
    """Оновлює повідомлення з кнопками або надсилає нове."""
    reply_markup = markup(rows) if rows else None
    query = update.callback_query

    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            return
        except BadRequest as error:
            if "not modified" in str(error).lower():
                return
            logger.info("Не вдалося відредагувати повідомлення (%s), надсилаю нове", error)

    await update.effective_chat.send_message(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def alert(update: Update, text: str) -> None:
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    else:
        await update.effective_chat.send_message(text)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MENU, resize_keyboard=True)


# ───────────────────────── Доступ ─────────────────────────

def admin_ids() -> set[int]:
    env_ids = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMIN_IDS", ""))}
    if env_ids:
        return env_ids
    owner = get_setting("admin_chat_id", "")
    return {int(owner)} if owner else set()


def report_chat_id() -> int | None:
    owner = get_setting("admin_chat_id", "")
    if owner:
        return int(owner)
    ids = admin_ids()
    return next(iter(ids)) if ids else None


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бот приватний: працює лише для власника (або ID з ADMIN_IDS)."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        raise ApplicationHandlerStop

    allowed = admin_ids()
    if not allowed:
        set_setting("admin_chat_id", str(chat.id))
        logger.info("Власником бота призначено %s", chat.id)
        return

    if user.id in allowed or chat.id in allowed:
        return

    logger.warning("Відмовлено в доступі: user_id=%s (%s)", user.id, user.full_name)
    if update.callback_query:
        await update.callback_query.answer("Це приватний бот.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(
            "⛔ Це приватний бот.\n"
            f"Твій Telegram ID: {user.id}\n"
            "Якщо ти власник — додай змінну середовища ADMIN_IDS з цим ID і перезапусти бота."
        )
    raise ApplicationHandlerStop


# ───────────────────────── Головне меню ─────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    if not get_setting("admin_chat_id", ""):
        set_setting("admin_chat_id", str(update.effective_chat.id))

    await update.effective_chat.send_message(
        "<b>NEUTRON · облік уроків</b>\n\n"
        "Обери розділ кнопками нижче.\n"
        "Щоб швидко знайти учня — просто напиши частину імені.",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_chat.send_message("Скасовано.", reply_markup=main_keyboard())


# ───────────────────────── Уроки ─────────────────────────

async def lesson_hub(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str = "") -> None:
    context.user_data.pop("lesson", None)
    today = now().date()
    weekday = today.weekday()
    done_groups, done_students = lessons_on_date(today.isoformat())

    groups = [g for g in get_all_groups() if g["students_count"] > 0]
    individuals = get_individual_students()

    if not groups and not individuals:
        await show(update, "Поки немає ні груп з учнями, ні індивідуальних учнів. Додай їх у розділі «Учні».")
        return

    today_groups = [g for g in groups if weekday in parse_days(g["lesson_day"])]
    today_individuals = [s for s in individuals if weekday in parse_days(s["lesson_day"])]
    has_today = bool(today_groups or today_individuals)
    show_all = scope == "all" or not has_today

    if show_all:
        list_groups, list_individuals = groups, individuals
    else:
        list_groups = sorted(today_groups, key=lambda g: time_key(g["lesson_time"]))
        list_individuals = sorted(today_individuals, key=lambda s: time_key(s["lesson_time"]))

    rows = []
    for group in list_groups:
        mark = "✔️" if group["id"] in done_groups else "👥"
        label = f"{mark} {group['name']}"
        if group["lesson_time"]:
            label += f" · {group['lesson_time']}"
        label += f" · {group['students_count']} уч."
        rows.append([btn(label, f"gl:{group['id']}")])

    for student in list_individuals:
        mark = "✔️" if student["id"] in done_students else "👤"
        label = f"{mark} {student['name']}"
        if student["lesson_time"] and not show_all:
            label += f" · {student['lesson_time']}"
        label += f" · {student['remaining_lessons']}"
        rows.append([btn(label, f"il:{student['id']}")])

    if not show_all:
        rows.append([btn("Інші групи та учні", "L:all")])
    elif has_today:
        rows.append([btn("◀ Лише сьогоднішні", "L")])

    header = f"<b>Провести урок</b> · {DAYS[weekday]}, {today:%d.%m}\n"
    if show_all and not has_today:
        header += "Сьогодні за розкладом нічого немає — показую всіх.\n"
    elif show_all:
        header += "Усі групи та індивідуальні учні.\n"
    else:
        header += "Сьогодні за розкладом:\n"
    header += "\n👥 група — відкриє відмітку присутніх\n👤 учень — одразу спише 1 урок\n✔️ — сьогодні вже проведено"

    await show(update, header, rows)


async def group_lesson_start(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    students = get_students_by_group_id(int(group_id))
    if not students:
        await alert(update, "У цій групі поки немає учнів.")
        return

    context.user_data["lesson"] = {
        "gid": int(group_id),
        "date": now().date().isoformat(),
        "present": {s["id"] for s in students} if DEFAULT_ALL_PRESENT else set(),
    }
    await group_lesson_render(update, context)


async def group_lesson_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("lesson")
    if not draft:
        await show(
            update,
            "Ця відмітка вже неактуальна (заняття збережено або бот перезапускався).",
            [[btn("✅ Провести урок", "L")]],
        )
        return

    group = get_group_by_id(draft["gid"])
    students = get_students_by_group_id(draft["gid"])
    ids = {s["id"] for s in students}
    draft["present"] &= ids
    day = date.fromisoformat(draft["date"])

    text = (
        f"<b>{esc(group['name'])}</b> · групове заняття\n"
        f"📅 {fmt_day(day)}\n"
        f"Присутні: <b>{len(draft['present'])}</b> з {len(students)}\n\n"
        f"Тисни на учня, щоб змінити відмітку.\n"
        f"Урок спишеться з усіх {len(students)} учнів — і з присутніх, і з відсутніх."
    )

    buttons = [
        btn(
            f"{'✅' if s['id'] in draft['present'] else '❌'} {s['name']} · {s['remaining_lessons']}",
            f"gt:{s['id']}",
        )
        for s in students
    ]
    rows = chunk(buttons, 1 if len(buttons) <= 10 else 2)
    rows.append([btn("✅ Усі", "ga:1"), btn("❌ Нікого", "ga:0")])
    rows.append([btn("📅 Інша дата", "gd")])
    rows.append([btn("💾 Провести заняття", "gs:0")])
    rows.append([btn("✖ Скасувати", "L")])

    await show(update, text, rows)


async def group_lesson_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    draft = context.user_data.get("lesson")
    if draft:
        draft["present"] ^= {int(student_id)}
    await group_lesson_render(update, context)


async def group_lesson_all(update: Update, context: ContextTypes.DEFAULT_TYPE, flag: str) -> None:
    draft = context.user_data.get("lesson")
    if draft:
        students = get_students_by_group_id(draft["gid"])
        draft["present"] = {s["id"] for s in students} if flag == "1" else set()
    await group_lesson_render(update, context)


async def group_lesson_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("lesson"):
        await group_lesson_render(update, context)
        return

    today = now().date()
    buttons = []
    for offset in range(0, 10):
        day = today - timedelta(days=offset)
        label = "Сьогодні" if offset == 0 else "Вчора" if offset == 1 else f"{day:%d.%m} {DAYS[day.weekday()]}"
        buttons.append(btn(label, f"gD:{day.isoformat()}"))

    rows = chunk(buttons, 2)
    rows.append([btn("◀ Назад", "gb")])
    await show(update, "За яку дату провести заняття?", rows)


async def group_lesson_set_date(update: Update, context: ContextTypes.DEFAULT_TYPE, day: str) -> None:
    draft = context.user_data.get("lesson")
    if draft:
        draft["date"] = day
    await group_lesson_render(update, context)


def payment_alert_lines(rows) -> list[str]:
    """rows: [{name, balance}] → рядки про учнів, яким час платити."""
    lines = []
    for row in sorted(rows, key=lambda r: r["balance"]):
        if row["balance"] <= LOW_BALANCE:
            lines.append(f"{bal_icon(row['balance'])} {esc(row['name'])} — {bal_text(row['balance'])}")
    return lines


async def group_lesson_save(update: Update, context: ContextTypes.DEFAULT_TYPE, force: str = "0") -> None:
    draft = context.user_data.get("lesson")
    if not draft:
        await group_lesson_render(update, context)
        return

    day = date.fromisoformat(draft["date"])
    done_groups, _ = lessons_on_date(draft["date"])

    if draft["gid"] in done_groups and force != "1":
        await show(
            update,
            f"⚠️ У цієї групи вже є проведене заняття за {fmt_day(day)}.\n"
            "Провести ще одне і списати урок вдруге?",
            [[btn("Так, провести ще одне", "gs:1")], [btn("◀ Назад", "gb")]],
        )
        return

    context.user_data.pop("lesson", None)
    result = complete_group_lesson(draft["gid"], draft["present"], stamp(day))

    if not result["success"]:
        await show(update, esc(result["message"]))
        return

    students = result["students"]
    absent = [s["name"] for s in students if not s["present"]]
    present_count = len(students) - len(absent)

    text = (
        f"✅ <b>Заняття проведено</b>\n"
        f"{esc(result['group_name'])} · {fmt_day(day)}\n\n"
        f"Присутні: <b>{present_count}</b> з {len(students)}\n"
    )
    if absent:
        text += "Відсутні: " + ", ".join(esc(name) for name in absent) + "\n"
    text += f"Списано по 1 уроку з усіх {len(students)} учнів.\n"

    alerts = payment_alert_lines(students)
    if alerts:
        text += "\n<b>Час нагадати про оплату:</b>\n" + "\n".join(alerts)

    lesson_id = result["lesson_id"]
    await show(update, text, [
        [btn("✏️ Виправити відмітки", f"le:{lesson_id}")],
        [btn("↩️ Скасувати заняття", f"ul:{lesson_id}")],
    ])


async def individual_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str, force: str = "0") -> None:
    sid = int(student_id)
    student = get_student_by_id(sid)
    if not student:
        await alert(update, "Учня не знайдено.")
        return

    _, done_students = lessons_on_date(now().date().isoformat())
    if sid in done_students and force != "1":
        await show(
            update,
            f"⚠️ {esc(student['name'])}: сьогодні урок уже списано.\nСписати ще один?",
            [[btn("Так, списати ще один", f"il:{sid}:1")], [btn("◀ Назад", "L")]],
        )
        return

    result = complete_individual_lesson(sid, stamp())
    if not result["success"]:
        await show(update, esc(result["message"]))
        return

    balance = result["remaining_lessons"]
    text = (
        f"✅ <b>Урок проведено</b>\n"
        f"{esc(result['student_name'])} · {fmt_day(now().date())}\n\n"
        f"{bal_icon(balance)} {bal_text(balance).capitalize()}"
    )
    if balance <= LOW_BALANCE:
        text += "\nЧас нагадати про оплату."

    await show(update, text, [
        [btn("↩️ Скасувати урок", f"ul:{result['lesson_id']}"), btn("👤 Картка", f"sc:{sid}")],
        [btn("✅ До списку уроків", "L")],
    ])


async def lesson_view(update: Update, context: ContextTypes.DEFAULT_TYPE, lesson_id: str) -> None:
    lesson, rows = get_lesson(int(lesson_id))
    if not lesson:
        await alert(update, "Заняття не знайдено.")
        return

    day_label = fmt_date(lesson["lesson_date"])
    month = lesson["lesson_date"][:7]

    if lesson["lesson_type"] == "individual":
        text = f"<b>{esc(lesson['student_name'])}</b> · індивідуальний урок\n📅 {day_label}"
        back = [btn("👤 Картка", f"sc:{lesson['student_id']}")]
    else:
        text = f"<b>{esc(lesson['group_name'])}</b> · групове заняття\n📅 {day_label}"
        back = [btn("📊 До відвідуваності", f"av:{lesson['group_id']}:{month}")]

    if lesson["status"] != "completed":
        await show(update, text + "\n\n↩️ Заняття скасовано, уроки повернуто на баланс.", [back])
        return

    keyboard = []
    if lesson["lesson_type"] == "group":
        present = sum(1 for r in rows if r["attendance_status"] == "present")
        text += f"\nПрисутні: <b>{present}</b> з {len(rows)}\n\nТисни на учня, щоб виправити відмітку. Баланс при цьому не змінюється."
        buttons = [
            btn(
                f"{'✅' if r['attendance_status'] == 'present' else '❌'} {r['name']}",
                f"lt:{lesson['id']}:{r['student_id']}",
            )
            for r in rows
        ]
        keyboard = chunk(buttons, 1 if len(buttons) <= 10 else 2)

    keyboard.append([btn("↩️ Скасувати заняття", f"ul:{lesson['id']}")])
    keyboard.append(back)
    await show(update, text, keyboard)


async def lesson_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, lesson_id: str, student_id: str) -> None:
    _, rows = get_lesson(int(lesson_id))
    current = next((r for r in rows if r["student_id"] == int(student_id)), None)
    if current:
        set_attendance(int(lesson_id), int(student_id), current["attendance_status"] != "present")
    await lesson_view(update, context, lesson_id)


async def lesson_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, lesson_id: str) -> None:
    lesson, rows = get_lesson(int(lesson_id))
    if not lesson or lesson["status"] != "completed":
        await alert(update, "Це заняття вже скасовано.")
        return

    title = lesson["group_name"] or lesson["student_name"]
    refund = sum(1 for r in rows if r["lesson_written_off"])
    await show(
        update,
        f"Скасувати заняття «{esc(title)}» за {fmt_date(lesson['lesson_date'])}?\n"
        f"Урок повернеться на баланс: {refund} уч.",
        [[btn("Так, скасувати", f"uL:{lesson_id}")], [btn("Ні, залишити", f"le:{lesson_id}")]],
    )


async def lesson_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, lesson_id: str) -> None:
    refunded = cancel_lesson(int(lesson_id))
    if refunded is None:
        await alert(update, "Це заняття вже скасовано.")
        return
    await show(
        update,
        f"↩️ Заняття скасовано. Повернуто уроків на баланс: {refunded}.",
        [[btn("✅ Провести урок", "L")]],
    )


# ───────────────────────── Вибір учня ─────────────────────────

PICK_TARGET = {"pay": "ps", "card": "sc"}
PICK_TITLE = {"pay": "💳 <b>Оплата</b> — обери учня", "card": "👥 <b>Учні</b>"}


async def picker(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = "card", scope: str = "") -> None:
    target = PICK_TARGET[action]
    students = get_all_students()
    rows = []
    text = PICK_TITLE[action]
    context.user_data["await"] = {"kind": "search", "action": action}

    if scope == "x":
        inactive = get_inactive_students()
        text = "🗃 <b>Неактивні учні</b>" if inactive else "Неактивних учнів немає."
        rows = chunk([student_button(s, "sc") for s in inactive], 1 if len(inactive) <= 10 else 2)
        rows.append([btn("◀ Назад", f"pk:{action}")])
    elif not scope and len(students) > 10:
        groups = get_all_groups()
        for group in groups:
            if group["students_count"]:
                rows.append([btn(f"👥 {group['name']} ({group['students_count']})", f"pk:{action}:g{group['id']}")])
        individual_count = sum(1 for s in students if s["student_type"] == "individual")
        if individual_count:
            rows.append([btn(f"👤 Індивідуальні ({individual_count})", f"pk:{action}:i")])
        orphans = [s for s in students if s["student_type"] == "group" and not s["group_name"]]
        rows += [[student_button(s, target)] for s in orphans]
        text += "\nОбери групу або просто напиши частину імені."
    else:
        if scope.startswith("g"):
            students = [s for s in students if s["group_id"] == int(scope[1:])]
        elif scope == "i":
            students = [s for s in students if s["student_type"] == "individual"]
        if not students:
            text += "\nУчнів поки немає."
        buttons = [student_button(s, target) for s in students]
        rows = chunk(buttons, 1 if len(buttons) <= 10 else 2)
        if scope:
            rows.append([btn("◀ Назад", f"pk:{action}")])
        else:
            text += "\nЦифра біля імені — залишок уроків."

    if action == "card" and scope != "x":
        rows.append([btn("➕ Додати учня", "as"), btn("🗃 Неактивні", "pk:card:x")])

    await show(update, text, rows)


async def search_students(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str, action: str = "card") -> None:
    found = search_students_by_name(query_text)
    if not found:
        await update.effective_chat.send_message(
            f"За запитом «{query_text}» учнів не знайдено.\n"
            "Розділи бота — на клавіатурі нижче.",
            reply_markup=main_keyboard(),
        )
        return

    target = PICK_TARGET.get(action, "sc")
    if len(found) == 1:
        await ROUTES[target](update, context, str(found[0]["id"]))
        return

    buttons = []
    for student in found[:30]:
        label = f"{bal_icon(student['remaining_lessons'])} {student['name']} · {student_place(student)}"
        if not student["is_active"]:
            label = f"🗃 {student['name']} · неактивний"
        buttons.append([btn(label, f"{target}:{student['id']}")])
    await show(update, f"Знайдено за «{esc(query_text)}»:", buttons)


# ───────────────────────── Оплати ─────────────────────────

async def payment_packages(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    if not student:
        await alert(update, "Учня не знайдено.")
        return

    balance = student["remaining_lessons"]
    rows = [
        [btn(f"{lessons} ур. · {fmt_money(amount)} грн", f"pa:{student['id']}:{lessons}:{num(amount)}")]
        for lessons, amount in suggest_packages(student["id"])
    ]
    rows.append([btn("✍️ Ввести вручну", f"pm:{student['id']}")])
    rows.append([btn("👤 Картка", f"sc:{student['id']}")])

    text = (
        f"💳 <b>Оплата · {esc(student['name'])}</b>\n"
        f"{esc(student_place(student))} · {bal_icon(balance)} {bal_text(balance)}\n\n"
    )
    text += "Обери пакет або введи вручну." if len(rows) > 2 else "Натисни «Ввести вручну»."
    await show(update, text, rows)


async def payment_manual(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    context.user_data["await"] = {"kind": "pay", "sid": int(student_id)}
    await show(
        update,
        "Введи кількість уроків і суму через пробіл.\nНаприклад: <code>8 2400</code>",
        [[btn("✖ Скасувати", f"ps:{student_id}")]],
    )


async def payment_save(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str, lessons: str, amount: str) -> None:
    sid, lessons_paid, value = int(student_id), int(lessons), float(amount)

    # Захист від подвійного натискання.
    fingerprint = (sid, lessons_paid, value)
    last = context.user_data.get("last_payment")
    if last and last[0] == fingerprint and time.monotonic() - last[1] < 5:
        return
    context.user_data["last_payment"] = (fingerprint, time.monotonic())

    student = get_student_by_id(sid)
    result = add_payment(sid, value, lessons_paid, stamp())
    if not student or not result:
        await alert(update, "Учня не знайдено.")
        return

    balance = result["balance"]
    await show(
        update,
        f"💳 <b>Оплату додано</b>\n"
        f"{esc(student['name'])}\n\n"
        f"+{lessons_paid} ур. · {fmt_money(value)} грн\n"
        f"{bal_icon(balance)} {bal_text(balance).capitalize()}",
        [
            [btn("↩️ Скасувати оплату", f"up:{result['payment_id']}"), btn("👤 Картка", f"sc:{sid}")],
            [btn("💳 Ще одна оплата", "P")],
        ],
    )


async def payment_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_id: str) -> None:
    payment = get_payment(int(payment_id))
    if not payment or payment["is_cancelled"]:
        await alert(update, "Цю оплату вже скасовано.")
        return
    await show(
        update,
        f"Скасувати оплату?\n{esc(payment['student_name'])} · {fmt_date(payment['payment_date'])} · "
        f"{payment['lessons_paid']} ур. · {fmt_money(payment['amount'])} грн\n"
        "Ці уроки знімуться з балансу.",
        [[btn("Так, скасувати", f"uP:{payment_id}")], [btn("Ні, залишити", f"sc:{payment['student_id']}")]],
    )


async def payment_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_id: str) -> None:
    result = cancel_payment(int(payment_id))
    if not result:
        await alert(update, "Цю оплату вже скасовано.")
        return
    await show(
        update,
        f"↩️ Оплату скасовано.\n{esc(result['student_name'])}: {bal_text(result['balance'])}.",
    )


# ───────────────────────── Картка учня ─────────────────────────

async def student_card(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    if not student:
        await alert(update, "Учня не знайдено.")
        return

    sid = student["id"]
    balance = student["remaining_lessons"]
    stats = get_student_attendance_stats(sid)

    text = f"<b>{esc(student['name'])}</b>"
    if not student["is_active"]:
        text += " · 🗃 неактивний"
    text += "\n"

    if student["student_type"] == "group":
        schedule = schedule_text(student["group_lesson_day"], student["group_lesson_time"])
        text += f"Група: {esc(student['group_name'] or '—')} ({esc(schedule)})\n"
    else:
        text += f"Індивідуально · {esc(schedule_text(student['lesson_day'], student['lesson_time']))}\n"

    text += f"\n{bal_icon(balance)} <b>{bal_text(balance).capitalize()}</b>\n"
    text += f"Остання оплата: {fmt_date(student['last_payment_date'])}\n"
    if stats["total"]:
        percent = round(100 * stats["present"] / stats["total"])
        text += f"Відвідуваність: {stats['present']} з {stats['total']} ({percent}%)\n"
    if student["notes"]:
        text += f"📝 {esc(student['notes'])}\n"

    rows = []
    first = [btn("💳 Оплата", f"ps:{sid}")]
    if student["student_type"] == "individual" and student["is_active"]:
        first.append(btn("✅ Списати урок", f"il:{sid}"))
    rows.append(first)
    rows.append([btn("🧾 Історія", f"sh:{sid}"), btn("✏️ Баланс", f"sb:{sid}")])

    third = [btn("🔁 Перевести", f"sm:{sid}")]
    if student["student_type"] == "individual":
        third.insert(0, btn("📅 Розклад", f"ss:{sid}"))
    rows.append(third)
    rows.append([btn("✏️ Ім'я", f"sn:{sid}"), btn("📝 Нотатка", f"so:{sid}")])

    if student["is_active"]:
        rows.append([btn("🚫 Деактивувати", f"sd:{sid}")])
    else:
        rows.append([btn("♻️ Повернути в активні", f"sa:{sid}")])
    rows.append([btn("◀ До списку учнів", "pk:card")])

    await show(update, text, rows)


async def student_history(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    if not student:
        await alert(update, "Учня не знайдено.")
        return

    payments = get_payments_by_student(student["id"], limit=10)
    lessons = get_student_lessons(student["id"], limit=20)

    text = f"🧾 <b>Історія · {esc(student['name'])}</b>\n\n<b>Оплати</b>\n"
    if payments:
        for payment in payments:
            text += f"{fmt_date(payment['payment_date'])} · {payment['lessons_paid']} ур. · {fmt_money(payment['amount'])} грн\n"
    else:
        text += "оплат ще не було\n"

    text += "\n<b>Уроки</b> (останні)\n"
    if lessons:
        for lesson in lessons:
            mark = "✅" if lesson["attendance_status"] == "present" else "❌"
            note = "" if lesson["lesson_written_off"] else " · не списано"
            text += f"{mark} {fmt_date(lesson['lesson_date'])}{note}\n"
    else:
        text += "уроків ще не було\n"

    rows = []
    if payments:
        last = payments[0]
        rows.append([btn(f"↩️ Скасувати оплату {fmt_date(last['payment_date'], False)}", f"up:{last['id']}")])
    rows.append([btn("◀ Картка", f"sc:{student['id']}")])
    await show(update, text, rows)


async def ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, prompt: str, back: str) -> None:
    context.user_data["await"] = state
    await show(update, prompt, [[btn("✖ Скасувати", back)]])


async def student_balance_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    await ask_text(
        update, context, {"kind": "balance", "sid": int(student_id)},
        f"<b>{esc(student['name'])}</b> · зараз на балансі: {student['remaining_lessons']}\n\n"
        "Введи:\n"
        "<code>+2</code> або <code>-1</code> — додати / зняти уроки\n"
        "<code>5</code> — встановити рівно 5\n"
        "<code>=-2</code> — встановити борг 2",
        f"sc:{student_id}",
    )


async def student_rename_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    await ask_text(update, context, {"kind": "rename_s", "sid": int(student_id)}, "Введи нове ім'я учня:", f"sc:{student_id}")


async def student_note_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    await ask_text(
        update, context, {"kind": "note_s", "sid": int(student_id)},
        "Введи нотатку (телефон батьків, домовленості тощо).\nЩоб очистити — надішли <code>-</code>",
        f"sc:{student_id}",
    )


async def student_deactivate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    await show(
        update,
        f"Деактивувати учня <b>{esc(student['name'])}</b>?\n"
        "Він зникне зі списків і групових занять. Історія та баланс збережуться, повернути можна будь-коли.",
        [[btn("Так, деактивувати", f"sD:{student_id}")], [btn("Ні", f"sc:{student_id}")]],
    )


async def student_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    set_student_active(int(student_id), False)
    await student_card(update, context, student_id)


async def student_activate(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    set_student_active(int(student_id), True)
    await student_card(update, context, student_id)


async def student_move_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    student = get_student_by_id(int(student_id))
    rows = [
        [btn(f"👥 {group['name']}", f"sM:{student_id}:{group['id']}")]
        for group in get_all_groups()
        if group["id"] != student["group_id"]
    ]
    if student["student_type"] == "group":
        rows.append([btn("👤 Зробити індивідуальним", f"sM:{student_id}:0")])
    rows.append([btn("◀ Картка", f"sc:{student_id}")])
    await show(update, f"Куди перевести <b>{esc(student['name'])}</b>?\nБаланс уроків збережеться.", rows)


async def student_move(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str, group_id: str) -> None:
    if group_id == "0":
        make_student_individual(int(student_id))
    else:
        move_student_to_group(int(student_id), int(group_id))
    await student_card(update, context, student_id)


async def add_student_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = [[btn(f"👥 {group['name']}", f"an:g{group['id']}")] for group in get_all_groups()]
    rows.append([btn("👤 Індивідуальний учень", "an:i")])
    rows.append([btn("◀ Назад", "pk:card")])
    await show(update, "➕ <b>Новий учень</b>\nКуди додати?", rows)


async def add_student_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str) -> None:
    group_id = int(scope[1:]) if scope.startswith("g") else None
    place = "індивідуальні"
    if group_id:
        place = f"група «{esc(get_group_by_id(group_id)['name'])}»"
    await ask_text(
        update, context, {"kind": "add_students", "gid": group_id},
        f"➕ Новий учень · {place}\n\nВведи ім'я та прізвище.\n"
        "Можна додати кількох одразу — кожного з нового рядка.",
        f"gc:{group_id}" if group_id else "pk:card",
    )


# ───────────────────────── Розклад (учень або група) ─────────────────────────

async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, item_id: str) -> None:
    if kind == "s":
        item = get_student_by_id(int(item_id))
    else:
        item = get_group_by_id(int(item_id))
    context.user_data["sched"] = {
        "kind": kind,
        "id": int(item_id),
        "name": item["name"],
        "days": set(parse_days(item["lesson_day"])),
        "time": parse_time(item["lesson_time"] or ""),
    }
    await schedule_render(update, context)


async def schedule_student(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: str) -> None:
    await schedule_start(update, context, "s", student_id)


async def schedule_group(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    await schedule_start(update, context, "g", group_id)


def schedule_back(draft: dict) -> str:
    return f"{'sc' if draft['kind'] == 's' else 'gc'}:{draft['id']}"


async def schedule_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("sched")
    if not draft:
        await show(update, "Редагування розкладу вже неактуальне. Відкрий картку ще раз.")
        return

    buttons = [
        btn(f"{'✅' if index in draft['days'] else '▫️'} {name}", f"dy:{index}")
        for index, name in enumerate(DAYS)
    ]
    rows = [buttons[:4], buttons[4:]]
    rows.append([btn("Далі → час", "dY")])
    rows.append([btn("✖ Скасувати", schedule_back(draft))])
    await show(update, f"📅 <b>Розклад · {esc(draft['name'])}</b>\nПознач дні занять:", rows)


async def schedule_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, day: str) -> None:
    draft = context.user_data.get("sched")
    if draft:
        draft["days"] ^= {int(day)}
    await schedule_render(update, context)


async def schedule_time_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("sched")
    if not draft:
        await schedule_render(update, context)
        return

    context.user_data["await"] = {"kind": "sched_time"}
    rows = []
    if draft["time"]:
        rows.append([btn(f"Залишити {draft['time']}", "dK")])
    rows.append([btn("✖ Скасувати", schedule_back(draft))])
    await show(
        update,
        f"Дні: {format_days(draft['days']) or 'не обрано'}\nВведи час занять, наприклад <code>16:00</code>",
        rows,
    )


async def schedule_save(update: Update, context: ContextTypes.DEFAULT_TYPE, time_text: str | None = None) -> None:
    draft = context.user_data.pop("sched", None)
    if not draft:
        await schedule_render(update, context)
        return

    days = format_days(draft["days"])
    lesson_time = time_text or draft["time"] or ""
    if draft["kind"] == "s":
        update_individual_schedule(draft["id"], days, lesson_time)
        await student_card(update, context, str(draft["id"]))
    else:
        update_group_schedule(draft["id"], days, lesson_time)
        await group_card(update, context, str(draft["id"]))


# ───────────────────────── Групи ─────────────────────────

async def groups_list(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str = "") -> None:
    if scope == "x":
        archived = get_archived_groups()
        rows = [[btn(f"♻️ {group['name']}", f"gr:{group['id']}")] for group in archived]
        rows.append([btn("◀ Назад", "G")])
        await show(update, "🗃 <b>Архів груп</b>\nНатисни на групу, щоб повернути її." if archived else "Архів порожній.", rows)
        return

    groups = get_all_groups()
    rows = [
        [btn(
            f"👥 {group['name']} · {schedule_text(group['lesson_day'], group['lesson_time'])} · {group['students_count']} уч.",
            f"gc:{group['id']}",
        )]
        for group in groups
    ]
    last = [btn("➕ Нова група", "ag")]
    if get_archived_groups():
        last.append(btn("🗃 Архів", "G:x"))
    rows.append(last)
    await show(update, "🗂 <b>Групи</b>" if groups else "Груп поки немає.", rows)


async def group_card(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    group = get_group_by_id(int(group_id))
    if not group:
        await alert(update, "Групу не знайдено.")
        return

    students = get_students_by_group_id(group["id"])
    text = (
        f"<b>{esc(group['name'])}</b>\n"
        f"📅 {esc(schedule_text(group['lesson_day'], group['lesson_time']))}\n"
        f"Учнів: {len(students)}\n\n"
    )
    for student in students:
        balance = student["remaining_lessons"]
        text += f"{bal_icon(balance)} {esc(student['name'])} — {balance}\n"
    if not students:
        text += "У групі поки немає учнів.\n"

    gid = group["id"]
    rows = [
        [btn("✅ Провести заняття", f"gl:{gid}")],
        [btn("📊 Відвідуваність", f"av:{gid}"), btn("➕ Додати учнів", f"an:g{gid}")],
        [btn("📅 Розклад", f"gy:{gid}"), btn("✏️ Назва", f"gn:{gid}")],
        [btn("🗃 В архів", f"gx:{gid}")],
        [btn("◀ До груп", "G")],
    ]
    await show(update, text, rows)


async def group_add_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_text(update, context, {"kind": "add_group"}, "➕ <b>Нова група</b>\nВведи назву групи:", "G")


async def group_rename_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    await ask_text(update, context, {"kind": "rename_g", "gid": int(group_id)}, "Введи нову назву групи:", f"gc:{group_id}")


async def group_archive_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    group = get_group_by_id(int(group_id))
    await show(
        update,
        f"Перенести групу <b>{esc(group['name'])}</b> в архів?\n"
        "Вона зникне зі списків, уся історія збережеться. Учні залишаться активними — переведи або деактивуй їх за потреби.",
        [[btn("Так, в архів", f"gX:{group_id}")], [btn("Ні", f"gc:{group_id}")]],
    )


async def group_archive(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    set_group_active(int(group_id), False)
    await groups_list(update, context)


async def group_restore(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    set_group_active(int(group_id), True)
    await group_card(update, context, group_id)


# ───────────────────────── Відвідуваність ─────────────────────────

async def attendance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    groups = get_all_groups()
    if not groups:
        await show(update, "Груп поки немає.")
        return
    if len(groups) == 1:
        await attendance_view(update, context, str(groups[0]["id"]))
        return
    rows = [[btn(f"👥 {group['name']}", f"av:{group['id']}")] for group in groups]
    await show(update, "📊 <b>Відвідуваність</b>\nОбери групу:", rows)


def short_name(name: str, width: int = 12) -> str:
    """'Іваненко Андрій' → 'Іваненко А.' якщо не влазить."""
    if len(name) <= width:
        return name
    parts = name.split()
    if len(parts) > 1:
        candidate = f"{parts[0]} {parts[1][0]}."
        if len(candidate) <= width:
            return candidate
    return name[:width - 1] + "…"


def attendance_table(lessons, students, marks) -> tuple[str, int, int]:
    """Таблиця моноширинним шрифтом: рядок — учень, стовпчик — заняття."""
    width = max((len(short_name(s["name"])) for s in students), default=4)
    days = [row["lesson_date"][8:10] for row in lessons]

    lines = [
        " " * (width + 1) + "".join(day[0] for day in days),
        " " * (width + 1) + "".join(day[1] for day in days),
    ]
    present_total = marked_total = 0

    for student in students:
        cells = ""
        present = marked = 0
        for lesson in lessons:
            status = marks.get((lesson["id"], student["id"]))
            if status is None:
                cells += "·"
                continue
            marked += 1
            if status == "present":
                present += 1
                cells += "+"
            else:
                cells += "-"
        present_total += present
        marked_total += marked
        lines.append(f"{short_name(student['name']).ljust(width)} {cells} {present}/{marked}")

    return "\n".join(lines), present_total, marked_total


async def attendance_view(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str, month: str = "") -> None:
    group = get_group_by_id(int(group_id))
    if not group:
        await alert(update, "Групу не знайдено.")
        return

    month = month or current_month()
    lessons, students, marks = get_group_attendance(group["id"], month)

    text = f"📊 <b>{esc(group['name'])}</b> · {month_title(month)}\n"
    rows = []

    if lessons:
        table, present, marked = attendance_table(lessons, students, marks)
        percent = round(100 * present / marked) if marked else 0
        text += (
            f"Занять: {len(lessons)} · середня відвідуваність: {percent}%\n\n"
            f"<pre>{esc(table)}</pre>\n"
            "Зверху — число місяця (читай згори вниз).\n"
            "+ був · - не був · «·» ще не був у групі\n\n"
            "Натисни на дату, щоб відкрити заняття і виправити відмітки:"
        )
        date_buttons = [btn(fmt_date(row["lesson_date"], False), f"le:{row['id']}") for row in lessons]
        rows += chunk(date_buttons, 4)
    else:
        text += "\nЗа цей місяць занять не було."

    rows.append([
        btn(f"◀ {MONTHS[int(shift_month(month, -1)[5:]) - 1]}", f"av:{group['id']}:{shift_month(month, -1)}"),
        btn(f"{MONTHS[int(shift_month(month, 1)[5:]) - 1]} ▶", f"av:{group['id']}:{shift_month(month, 1)}"),
    ])
    if lessons:
        rows.append([btn("📄 Таблиця файлом (Excel)", f"ax:{group['id']}:{month}")])
    rows.append([btn("◀ Група", f"gc:{group['id']}")])

    await show(update, text, rows)


async def attendance_export(update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str, month: str) -> None:
    group = get_group_by_id(int(group_id))
    lessons, students, marks = get_group_attendance(int(group_id), month)
    if not group or not lessons:
        await alert(update, "Немає даних для експорту.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Учень"] + [fmt_date(row["lesson_date"], False) for row in lessons] + ["Був", "Занять", "%"])
    for student in students:
        cells, present, marked = [], 0, 0
        for lesson in lessons:
            status = marks.get((lesson["id"], student["id"]))
            if status is None:
                cells.append("")
                continue
            marked += 1
            present += status == "present"
            cells.append("+" if status == "present" else "н")
        percent = round(100 * present / marked) if marked else 0
        writer.writerow([student["name"]] + cells + [present, marked, percent])

    data = buffer.getvalue().encode("utf-8-sig")
    safe_name = re.sub(r"[^\w\-]+", "_", group["name"]).strip("_") or "group"
    await update.effective_chat.send_document(
        document=data,
        filename=f"vidviduvanist_{safe_name}_{month}.csv",
        caption=f"Відвідуваність · {group['name']} · {month_title(month)}",
    )


# ───────────────────────── Баланси та звіт ─────────────────────────

def build_summary_text() -> str:
    rows = get_low_balance_students(LOW_BALANCE)
    if not rows:
        return "⚠️ <b>Баланси</b>\n\nУсе добре: у всіх активних учнів є оплачені уроки."

    sections = [
        ("🔴 <b>Борг</b>", [r for r in rows if r["remaining_lessons"] < 0]),
        ("🟠 <b>Уроки закінчились</b>", [r for r in rows if r["remaining_lessons"] == 0]),
        ("🟡 <b>Залишився 1 урок</b>", [r for r in rows if r["remaining_lessons"] > 0]),
    ]

    text = "⚠️ <b>Баланси</b>\n"
    for title, items in sections:
        if not items:
            continue
        text += f"\n{title}\n"
        for row in sorted(items, key=lambda r: (name_key(r["group_name"] or "яяя"), name_key(r["name"]))):
            place = row["group_name"] if row["student_type"] == "group" else "інд."
            line = f"• {esc(row['name'])} ({esc(place or 'без групи')})"
            if row["remaining_lessons"] < 0:
                line += f" — {abs(row['remaining_lessons'])} ур."
            elif row["remaining_lessons"] > 1:
                line += f" — {row['remaining_lessons']}"
            text += line + "\n"
    return text


async def balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, build_summary_text(), [[btn("💳 Додати оплату", "P")]])


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE, month: str = "") -> None:
    month = month or current_month()
    data = get_month_report(month)
    totals = data["totals"]

    text = (
        f"📈 <b>Звіт · {month_title(month)}</b>\n\n"
        f"💳 Оплат: {totals['payments_count']} на <b>{fmt_money(totals['amount'])} грн</b> "
        f"({totals['lessons_paid']} ур.)\n"
        f"✅ Занять: групових {data['lessons'].get('group', 0)}, "
        f"індивідуальних {data['lessons'].get('individual', 0)}\n"
        f"Списано уроків з балансів: {data['written_off']}\n"
    )

    if data["by_group"]:
        text += "\n<b>Оплати по групах</b>\n"
        for row in data["by_group"]:
            text += f"• {esc(row['title'])} — {fmt_money(row['amount'])} грн ({row['payments_count']})\n"

    if data["attendance"]:
        text += "\n<b>Відвідуваність груп</b>\n"
        for row in data["attendance"]:
            percent = round(100 * row["present"] / row["total"]) if row["total"] else 0
            text += f"• {esc(row['title'])} — занять {row['lessons_count']}, {percent}%\n"

    rows = [[
        btn(f"◀ {MONTHS[int(shift_month(month, -1)[5:]) - 1]}", f"R:{shift_month(month, -1)}"),
        btn(f"{MONTHS[int(shift_month(month, 1)[5:]) - 1]} ▶", f"R:{shift_month(month, 1)}"),
    ]]
    await show(update, text, rows)


# ───────────────────────── Налаштування, бекап, фонові задачі ─────────────────────────

def ensure_default_settings() -> None:
    defaults = {
        "auto_reminders_enabled": "0",
        "auto_reminders_hour": "09",
        "auto_reminders_minute": "00",
        "last_auto_reminder_date": "",
        "weekly_backup_enabled": "1",
        "last_backup_date": "",
    }
    for key, value in defaults.items():
        if get_setting(key) is None:
            set_setting(key, value)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reminders = get_setting("auto_reminders_enabled", "0") == "1"
    weekly = get_setting("weekly_backup_enabled", "1") == "1"
    clock = f"{get_setting('auto_reminders_hour', '09')}:{get_setting('auto_reminders_minute', '00')}"
    last_backup = get_setting("last_backup_date", "") or "ще не було"

    text = (
        "⚙️ <b>Налаштування</b>\n\n"
        f"🔔 Щоденне зведення по балансах: <b>{'увімкнено' if reminders else 'вимкнено'}</b>, о {clock}\n"
        f"💾 Щотижневий бекап у цей чат: <b>{'увімкнено' if weekly else 'вимкнено'}</b>\n"
        f"Останній автобекап: {last_backup}"
    )
    rows = [
        [btn(f"🔔 {'Вимкнути' if reminders else 'Увімкнути'} зведення", "tr"), btn("⏰ Змінити час", "tt")],
        [btn(f"💾 {'Вимкнути' if weekly else 'Увімкнути'} автобекап", "tw")],
        [btn("📥 Надіслати бекап бази зараз", "tb")],
    ]
    await show(update, text, rows)


async def settings_toggle_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    enabled = get_setting("auto_reminders_enabled", "0") == "1"
    set_setting("auto_reminders_enabled", "0" if enabled else "1")
    await settings_menu(update, context)


async def settings_toggle_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    enabled = get_setting("weekly_backup_enabled", "1") == "1"
    set_setting("weekly_backup_enabled", "0" if enabled else "1")
    await settings_menu(update, context)


async def settings_time_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_text(update, context, {"kind": "reminder_time"}, "О котрій надсилати зведення? Наприклад <code>09:30</code>", "T")


async def send_backup(app: Application, chat_id: int, tag: str) -> None:
    path = await asyncio.to_thread(backup_db, tag)
    if not path:
        await app.bot.send_message(chat_id, "Базу даних ще не створено.")
        return
    with open(path, "rb") as file:
        await app.bot.send_document(
            chat_id,
            document=file,
            filename=path.name,
            caption=f"💾 Бекап бази · {now():%d.%m.%Y %H:%M}\nЗбережи цей файл — з нього можна відновити всі дані.",
        )


async def settings_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_backup(context.application, update.effective_chat.id, "manual")


async def background_tick(app: Application) -> None:
    chat_id = report_chat_id()
    if not chat_id:
        return

    current = now()
    today = current.strftime("%Y-%m-%d")

    if get_setting("auto_reminders_enabled", "0") == "1" and get_setting("last_auto_reminder_date", "") != today:
        hour = int(get_setting("auto_reminders_hour", "9"))
        minute = int(get_setting("auto_reminders_minute", "0"))
        if (current.hour, current.minute) >= (hour, minute):
            if get_low_balance_students(LOW_BALANCE):
                await app.bot.send_message(chat_id, build_summary_text(), parse_mode=ParseMode.HTML)
            set_setting("last_auto_reminder_date", today)

    if get_setting("weekly_backup_enabled", "1") == "1" and current.hour >= 9:
        last = get_setting("last_backup_date", "")
        due = True
        if last:
            try:
                due = (current.date() - date.fromisoformat(last)).days >= 7
            except ValueError:
                due = True
        if due:
            await send_backup(app, chat_id, "weekly")
            set_setting("last_backup_date", today)


async def background_loop(app: Application) -> None:
    try:
        while True:
            try:
                await background_tick(app)
            except Exception:
                logger.exception("Помилка у фоновій задачі")
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Фонову задачу зупинено")
        raise


async def post_init(app: Application) -> None:
    ensure_default_settings()
    app.bot_data[BACKGROUND_TASK_KEY] = asyncio.create_task(background_loop(app))


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get(BACKGROUND_TASK_KEY)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ───────────────────────── Обробка тексту ─────────────────────────

async def reply(update: Update, text: str, rows=None) -> None:
    await update.effective_chat.send_message(
        text, reply_markup=markup(rows) if rows else None, parse_mode=ParseMode.HTML
    )


async def handle_awaited(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, text: str) -> None:
    kind = state["kind"]

    def again() -> None:
        context.user_data["await"] = state

    if kind == "search":
        await search_students(update, context, text, state.get("action", "card"))

    elif kind == "pay":
        numbers = re.findall(r"\d+(?:[.,]\d+)?", text)
        if len(numbers) != 2:
            again()
            await reply(update, "Потрібно два числа: кількість уроків і сума. Наприклад: <code>8 2400</code>")
            return
        lessons_paid = int(float(numbers[0].replace(",", ".")))
        amount = float(numbers[1].replace(",", "."))
        if lessons_paid <= 0 or amount < 0:
            again()
            await reply(update, "Кількість уроків має бути більшою за 0.")
            return
        await payment_save(update, context, str(state["sid"]), str(lessons_paid), str(amount))

    elif kind == "balance":
        student = get_student_by_id(state["sid"])
        compact = text.replace(" ", "").replace("−", "-").replace("–", "-")
        if re.fullmatch(r"[+-]\d+", compact):
            new_balance = student["remaining_lessons"] + int(compact)
        elif re.fullmatch(r"=?-?\d+", compact):
            new_balance = int(compact.lstrip("="))
        else:
            again()
            await reply(update, "Не зрозумів. Приклади: <code>+2</code>, <code>-1</code>, <code>5</code>, <code>=-2</code>")
            return
        old_balance = update_student_balance(state["sid"], new_balance, stamp(), "ручна зміна")
        await reply(update, f"✏️ Баланс змінено: {old_balance} → <b>{new_balance}</b>")
        await student_card(update, context, str(state["sid"]))

    elif kind == "rename_s":
        rename_student(state["sid"], text)
        await student_card(update, context, str(state["sid"]))

    elif kind == "note_s":
        update_student_notes(state["sid"], "" if text == "-" else text)
        await student_card(update, context, str(state["sid"]))

    elif kind == "add_students":
        names = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        group_id = state.get("gid")
        if group_id:
            existing = {s["name"].casefold() for s in get_students_by_group_id(group_id)}
        else:
            existing = {s["name"].casefold() for s in get_individual_students()}

        created, skipped = [], []
        for name in names:
            if name.casefold() in existing:
                skipped.append(name)
                continue
            existing.add(name.casefold())
            new_id = create_group_student(name, group_id) if group_id else create_individual_student(name)
            created.append((new_id, name))

        message = ""
        if created:
            message += f"➕ Додано ({len(created)}): " + ", ".join(esc(name) for _, name in created)
        if skipped:
            message += "\nВже є в списку, пропущено: " + ", ".join(esc(name) for name in skipped)

        rows = [[btn(f"👤 {name}", f"sc:{new_id}")] for new_id, name in created[:8]]
        rows.append([btn("➕ Додати ще", f"an:g{group_id}" if group_id else "an:i")])
        if group_id:
            rows.append([btn("◀ До групи", f"gc:{group_id}")])
        elif created:
            message += "\n\nВідкрий картку учня, щоб задати розклад і додати оплату."
        await reply(update, message or "Не додано жодного учня.", rows)

    elif kind == "add_group":
        group_id = create_group(" ".join(text.split()))
        await reply(update, "➕ Групу створено. Задай розклад і додай учнів:")
        await group_card(update, context, str(group_id))

    elif kind == "rename_g":
        rename_group(state["gid"], " ".join(text.split()))
        await group_card(update, context, str(state["gid"]))

    elif kind == "sched_time":
        lesson_time = parse_time(text)
        if not lesson_time:
            again()
            await reply(update, "Не схоже на час. Введи у форматі <code>16:00</code>")
            return
        await schedule_save(update, context, lesson_time)

    elif kind == "reminder_time":
        value = parse_time(text)
        if not value:
            again()
            await reply(update, "Не схоже на час. Введи у форматі <code>09:30</code>")
            return
        hour, minute = value.split(":")
        set_setting("auto_reminders_hour", hour)
        set_setting("auto_reminders_minute", minute)
        set_setting("last_auto_reminder_date", "")
        await settings_menu(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return

    if text in MENU_ACTIONS:
        context.user_data.pop("await", None)
        context.user_data.pop("sched", None)
        await MENU_ACTIONS[text](update, context)
        return

    if text in OLD_BUTTONS:
        await start(update, context)
        return

    state = context.user_data.pop("await", None)
    if state:
        await handle_awaited(update, context, state, text)
    else:
        await search_students(update, context, text)


# ───────────────────────── Маршрутизація кнопок ─────────────────────────

async def menu_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await picker(update, context, "pay")


async def menu_students(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await picker(update, context, "card")


async def schedule_keep_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await schedule_save(update, context)


MENU_ACTIONS = {
    "✅ Провести урок": lesson_hub,
    "💳 Оплата": menu_payment,
    "👥 Учні": menu_students,
    "🗂 Групи": groups_list,
    "📊 Відвідуваність": attendance_menu,
    "⚠️ Баланси": balances,
    "📈 Звіт": report,
    "⚙️ Налаштування": settings_menu,
}

ROUTES = {
    # уроки
    "L": lesson_hub,
    "gl": group_lesson_start,
    "gt": group_lesson_toggle,
    "ga": group_lesson_all,
    "gd": group_lesson_dates,
    "gD": group_lesson_set_date,
    "gb": group_lesson_render,
    "gs": group_lesson_save,
    "il": individual_lesson,
    "le": lesson_view,
    "lt": lesson_toggle,
    "ul": lesson_cancel_confirm,
    "uL": lesson_cancel,
    # оплати
    "P": menu_payment,
    "ps": payment_packages,
    "pm": payment_manual,
    "pa": payment_save,
    "up": payment_cancel_confirm,
    "uP": payment_cancel,
    # учні
    "pk": picker,
    "sc": student_card,
    "sh": student_history,
    "sb": student_balance_ask,
    "sn": student_rename_ask,
    "so": student_note_ask,
    "sd": student_deactivate_confirm,
    "sD": student_deactivate,
    "sa": student_activate,
    "sm": student_move_menu,
    "sM": student_move,
    "ss": schedule_student,
    "as": add_student_menu,
    "an": add_student_ask,
    # розклад
    "dy": schedule_toggle,
    "dY": schedule_time_ask,
    "dK": schedule_keep_time,
    # групи
    "G": groups_list,
    "gc": group_card,
    "gy": schedule_group,
    "gn": group_rename_ask,
    "gx": group_archive_confirm,
    "gX": group_archive,
    "gr": group_restore,
    "ag": group_add_ask,
    # відвідуваність, звіт
    "av": attendance_view,
    "ax": attendance_export,
    "R": report,
    # налаштування
    "T": settings_menu,
    "tr": settings_toggle_reminders,
    "tw": settings_toggle_backup,
    "tt": settings_time_ask,
    "tb": settings_backup_now,
}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, *args = (query.data or "").split(":")
    handler = ROUTES.get(action)

    try:
        if handler:
            context.user_data.pop("await", None)
            await handler(update, context, *args)
    finally:
        try:
            await query.answer()
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        # Railway під час redeploy кілька секунд тримає дві копії бота одночасно.
        logger.warning("Тимчасовий Conflict під час polling (накладення двох інстансів при redeploy).")
        return

    logger.exception("Помилка під час обробки оновлення", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(
                "Сталася помилка, дію не виконано. Спробуй ще раз або натисни /start."
            )
        except Exception:
            pass


def build_application() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не знайдено")

    init_db()
    ensure_default_settings()

    app = build_application()
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
