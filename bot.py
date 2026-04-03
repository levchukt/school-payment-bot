import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from database.db import init_db
from database.groups import create_group, get_all_groups, get_group_by_id
from database.lessons import complete_group_lesson, complete_individual_lesson
from database.payments import (
    add_payment,
    get_debt_students,
    get_grouped_payment_alerts,
    get_individual_payment_alerts,
    get_low_balance_students,
    get_payments_by_student,
)
from database.settings import get_setting, set_setting
from database.students import (
    create_group_student,
    create_individual_student,
    deactivate_student,
    get_all_students,
    get_inactive_students,
    get_individual_students,
    get_student_by_id,
    get_students_by_group_id,
    move_student_to_group,
    search_students_by_name,
    update_individual_schedule,
    update_student_balance,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

GROUP_NAME, GROUP_DAY, GROUP_TIME = range(3)
STUDENT_TYPE, STUDENT_NAME, STUDENT_DAY, STUDENT_TIME, STUDENT_GROUP = range(10, 15)
PAYMENT_STUDENT_ID, PAYMENT_AMOUNT, PAYMENT_LESSONS = range(20, 23)
LESSON_INDIVIDUAL_ID, LESSON_GROUP_ID, LESSON_GROUP_PRESENT = range(30, 33)
CARD_STUDENT_ID, HISTORY_STUDENT_ID, DEACTIVATE_STUDENT_ID = range(40, 43)
BALANCE_STUDENT_ID, BALANCE_NEW_VALUE = range(50, 52)
MOVE_STUDENT_ID, MOVE_GROUP_ID = range(60, 62)
SCHEDULE_STUDENT_ID, SCHEDULE_DAY, SCHEDULE_TIME = range(70, 73)
SEARCH_NAME_QUERY, FILTER_GROUP_ID, AUTO_TIME_VALUE = range(80, 83)

AUTO_REMINDER_TASK_KEY = "auto_reminder_task"


def get_local_now() -> datetime:
    return datetime.now(TIMEZONE)


def ensure_default_settings():
    if get_setting("auto_reminders_enabled") is None:
        set_setting("auto_reminders_enabled", "0")
    if get_setting("auto_reminders_hour") is None:
        set_setting("auto_reminders_hour", "09")
    if get_setting("auto_reminders_minute") is None:
        set_setting("auto_reminders_minute", "00")
    if get_setting("last_auto_reminder_date") is None:
        set_setting("last_auto_reminder_date", "")


def normalize_weekday(day_text: str) -> int | None:
    if not day_text:
        return None

    value = day_text.strip().lower()

    mapping = {
        "понеділок": 0,
        "пн": 0,
        "monday": 0,
        "mon": 0,
        "вівторок": 1,
        "вт": 1,
        "tuesday": 1,
        "tue": 1,
        "середа": 2,
        "ср": 2,
        "wednesday": 2,
        "wed": 2,
        "четвер": 3,
        "чт": 3,
        "thursday": 3,
        "thu": 3,
        "пʼятниця": 4,
        "п'ятниця": 4,
        "пт": 4,
        "friday": 4,
        "fri": 4,
        "субота": 5,
        "сб": 5,
        "saturday": 5,
        "sat": 5,
        "неділя": 6,
        "нд": 6,
        "sunday": 6,
        "sun": 6,
    }

    return mapping.get(value)


def format_student_line(student) -> str:
    if student["student_type"] == "individual":
        return (
            f"{student['id']}. {student['name']} | Індивідуально | "
            f"{student['lesson_day']} {student['lesson_time']} | "
            f"Залишок: {student['remaining_lessons']}"
        )

    return (
        f"{student['id']}. {student['name']} | Група: {student['group_name']} | "
        f"{student['group_lesson_day']} {student['group_lesson_time']} | "
        f"Залишок: {student['remaining_lessons']}"
    )


def build_summary_text() -> str:
    individual_alerts = get_individual_payment_alerts()
    grouped_alerts = get_grouped_payment_alerts()
    debt_students = get_debt_students()

    text = "Загальне зведення по оплатах:\n\n"

    if not individual_alerts and not grouped_alerts and not debt_students:
        return text + "Проблемних учнів зараз немає."

    if individual_alerts:
        text += "Індивідуальні:\n"
        for student in individual_alerts:
            if student["remaining_lessons"] < 0:
                text += f"- {student['name']} — борг {abs(student['remaining_lessons'])} урок(ів)\n"
            elif student["remaining_lessons"] == 0:
                text += f"- {student['name']} — уроки закінчились\n"
            else:
                text += f"- {student['name']} — залишився 1 урок\n"
        text += "\n"

    if grouped_alerts:
        text += "Групи:\n"
        for group_name, students in grouped_alerts.items():
            text += f"\n{group_name}:\n"
            for student in students:
                if student["remaining_lessons"] < 0:
                    text += f"- {student['name']} — борг {abs(student['remaining_lessons'])} урок(ів)\n"
                elif student["remaining_lessons"] == 0:
                    text += f"- {student['name']} — уроки закінчились\n"
                else:
                    text += f"- {student['name']} — залишився 1 урок\n"
        text += "\n"

    if debt_students:
        text += "Боржники:\n"
        for student in debt_students:
            if student["student_type"] == "group":
                text += f"- {student['name']} ({student['group_name']}) — борг {abs(student['remaining_lessons'])} урок(ів)\n"
            else:
                text += f"- {student['name']} — борг {abs(student['remaining_lessons'])} урок(ів)\n"

    return text


async def maybe_send_auto_reminders(app: Application):
    enabled = get_setting("auto_reminders_enabled", "0")
    admin_chat_id = get_setting("admin_chat_id", "")
    hour = int(get_setting("auto_reminders_hour", "9"))
    minute = int(get_setting("auto_reminders_minute", "0"))
    last_date = get_setting("last_auto_reminder_date", "")

    if enabled != "1":
        return

    if not admin_chat_id:
        return

    now = get_local_now()
    today_str = now.strftime("%Y-%m-%d")

    if last_date == today_str:
        return

    if now.hour < hour or (now.hour == hour and now.minute < minute):
        return

    text = build_summary_text()
    await app.bot.send_message(chat_id=int(admin_chat_id), text=text)
    set_setting("last_auto_reminder_date", today_str)


async def auto_reminder_loop(app: Application):
    try:
        while True:
            try:
                await maybe_send_auto_reminders(app)
            except Exception:
                logger.exception("Помилка під час автонагадувань")
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Фонову задачу автонагадувань зупинено")
        raise


async def post_init(app: Application):
    ensure_default_settings()

    await app.bot.delete_webhook(drop_pending_updates=True)

    auto_task = asyncio.create_task(auto_reminder_loop(app))
    app.bot_data[AUTO_REMINDER_TASK_KEY] = auto_task


async def post_shutdown(app: Application):
    auto_task = app.bot_data.get(AUTO_REMINDER_TASK_KEY)

    if auto_task:
        auto_task.cancel()
        try:
            await auto_task
        except asyncio.CancelledError:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_setting("admin_chat_id", str(chat_id))

    keyboard = [
        ["Учні", "Групи"],
        ["Оплати", "Уроки"],
        ["Нагадування", "Швидкі дії"],
        ["Пошук і фільтри"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "Бот запущено. Обери розділ:",
        reply_markup=reply_markup
    )


async def groups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Додати групу"],
        ["Список груп"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Меню груп:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def students_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Додати учня", "Список учнів"],
        ["Картка учня", "Історія оплат"],
        ["Змінити баланс", "Деактивувати учня"],
        ["Перевести в групу", "Змінити розклад"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Меню учнів:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def payments_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Додати оплату"],
        ["Учні з малим залишком"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Меню оплат:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def lessons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Індивідуальний урок"],
        ["Групове заняття"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Меню уроків:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def reminders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled = get_setting("auto_reminders_enabled", "0")
    hour = get_setting("auto_reminders_hour", "09")
    minute = get_setting("auto_reminders_minute", "00")

    status = "увімкнені" if enabled == "1" else "вимкнені"

    keyboard = [
        ["Загальне зведення"],
        ["Індивідуальні нагадування", "Групові нагадування"],
        ["Боржники"],
        ["Увімкнути автонагадування", "Вимкнути автонагадування"],
        ["Змінити час автонагадування"],
        ["Назад"]
    ]
    await update.message.reply_text(
        f"Меню нагадувань:\nАвтонагадування: {status}\nЧас: {hour}:{minute}",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def quick_actions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Сьогоднішні уроки"],
        ["Швидка оплата"],
        ["Швидке індивідуальне списання", "Швидке групове списання"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Швидкі дії:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def search_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Пошук учня", "Учні групи"],
        ["Неактивні учні", "Проблемні баланси"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Пошук і фільтри:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def add_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи назву групи:")
    return GROUP_NAME


async def add_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group_name"] = update.message.text
    await update.message.reply_text("Введи день занять (наприклад: Понеділок):")
    return GROUP_DAY


async def add_group_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group_day"] = update.message.text
    await update.message.reply_text("Введи час занять (наприклад: 16:00):")
    return GROUP_TIME


async def add_group_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data["group_name"]
    day = context.user_data["group_day"]
    time = update.message.text

    create_group(name, day, time)

    await update.message.reply_text(f"Група створена:\n{name} — {day} {time}")
    return ConversationHandler.END


async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = get_all_groups()

    if not groups:
        await update.message.reply_text("Груп поки немає")
        return

    text = "Список груп:\n\n"
    for g in groups:
        text += f"{g['id']}. {g['name']} — {g['lesson_day']} {g['lesson_time']}\n"

    await update.message.reply_text(text)


async def add_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Індивідуальний"],
        ["У групу"],
        ["Назад"]
    ]
    await update.message.reply_text(
        "Оберіть тип учня:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return STUDENT_TYPE


async def add_student_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    student_type = update.message.text

    if student_type == "Індивідуальний":
        context.user_data["student_type"] = "individual"
        await update.message.reply_text("Введи ім'я учня:")
        return STUDENT_NAME

    if student_type == "У групу":
        context.user_data["student_type"] = "group"
        await update.message.reply_text("Введи ім'я учня:")
        return STUDENT_NAME

    if student_type == "Назад":
        await start(update, context)
        return ConversationHandler.END

    await update.message.reply_text("Оберіть варіант кнопкою.")
    return STUDENT_TYPE


async def add_student_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["student_name"] = update.message.text
    student_type = context.user_data["student_type"]

    if student_type == "individual":
        await update.message.reply_text("Введи день занять:")
        return STUDENT_DAY

    groups = get_all_groups()
    if not groups:
        await update.message.reply_text("Спочатку створи хоча б одну групу.")
        return ConversationHandler.END

    text = "Оберіть групу за ID:\n\n"
    for g in groups:
        text += f"{g['id']}. {g['name']} — {g['lesson_day']} {g['lesson_time']}\n"

    await update.message.reply_text(text)
    return STUDENT_GROUP


async def add_student_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["student_day"] = update.message.text
    await update.message.reply_text("Введи час занять:")
    return STUDENT_TIME


async def add_student_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_individual_student(
        context.user_data["student_name"],
        context.user_data["student_day"],
        update.message.text
    )
    await update.message.reply_text("Індивідуального учня створено.")
    return ConversationHandler.END


async def add_student_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        group_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID групи.")
        return STUDENT_GROUP

    groups = get_all_groups()
    valid_group_ids = [g["id"] for g in groups]

    if group_id not in valid_group_ids:
        await update.message.reply_text("Такої групи немає.")
        return STUDENT_GROUP

    create_group_student(context.user_data["student_name"], group_id)
    await update.message.reply_text("Учня додано в групу.")
    return ConversationHandler.END


async def list_students(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Учнів поки немає.")
        return

    text = "Список учнів:\n\n"
    for student in students:
        text += format_student_line(student) + "\n"

    await update.message.reply_text(text)


async def show_student_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Учнів поки немає.")
        return ConversationHandler.END

    text = "Введи ID учня для перегляду картки:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']} | Залишок: {s['remaining_lessons']}\n"

    await update.message.reply_text(text)
    return CARD_STUDENT_ID


async def show_student_card_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return CARD_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    text = f"Картка учня:\n\nID: {student['id']}\nІм'я: {student['name']}\nТип: {student['student_type']}\nСтатус: {'Активний' if student['is_active'] == 1 else 'Неактивний'}\n"

    if student["student_type"] == "individual":
        text += f"Розклад: {student['lesson_day']} {student['lesson_time']}\n"
    else:
        text += f"Група: {student['group_name']}\n"
        text += f"Розклад групи: {student['group_lesson_day']} {student['group_lesson_time']}\n"

    text += f"Залишок уроків: {student['remaining_lessons']}\n"
    text += f"Остання оплата: {student['last_payment_date'] or '—'}\n"
    text += f"Нотатки: {student['notes'] or '—'}"

    await update.message.reply_text(text)
    return ConversationHandler.END


async def student_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students(include_inactive=True)

    if not students:
        await update.message.reply_text("Учнів поки немає.")
        return ConversationHandler.END

    text = "Введи ID учня для перегляду історії оплат:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']}\n"

    await update.message.reply_text(text)
    return HISTORY_STUDENT_ID


async def student_history_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return HISTORY_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    payments = get_payments_by_student(student_id)
    if not payments:
        await update.message.reply_text(f"По учню {student['name']} оплат поки немає.")
        return ConversationHandler.END

    text = f"Історія оплат: {student['name']}\n\n"
    for payment in payments:
        text += (
            f"- {payment['payment_date']} | "
            f"Сума: {payment['amount']} | "
            f"Уроків: {payment['lessons_paid']}\n"
        )

    await update.message.reply_text(text)
    return ConversationHandler.END


async def deactivate_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Активних учнів немає.")
        return ConversationHandler.END

    text = "Введи ID учня для деактивації:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']}\n"

    await update.message.reply_text(text)
    return DEACTIVATE_STUDENT_ID


async def deactivate_student_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return DEACTIVATE_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student or student["is_active"] == 0:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    deactivate_student(student_id)
    await update.message.reply_text(f"Учня {student['name']} деактивовано.")
    return ConversationHandler.END


async def update_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Активних учнів немає.")
        return ConversationHandler.END

    text = "Введи ID учня для зміни балансу:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']} | Поточний залишок: {s['remaining_lessons']}\n"

    await update.message.reply_text(text)
    return BALANCE_STUDENT_ID


async def update_balance_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return BALANCE_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student or student["is_active"] == 0:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    context.user_data["balance_student_id"] = student_id
    context.user_data["balance_student_name"] = student["name"]

    await update.message.reply_text(
        f"Введи новий баланс для {student['name']}.\nМожна вводити від'ємні значення."
    )
    return BALANCE_NEW_VALUE


async def update_balance_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_balance = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи ціле число.")
        return BALANCE_NEW_VALUE

    student_id = context.user_data["balance_student_id"]
    student_name = context.user_data["balance_student_name"]

    update_student_balance(student_id, new_balance)
    await update.message.reply_text(
        f"Баланс оновлено.\nУчень: {student_name}\nНовий залишок: {new_balance}"
    )
    return ConversationHandler.END


async def move_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()
    groups = get_all_groups()

    if not students:
        await update.message.reply_text("Активних учнів немає.")
        return ConversationHandler.END

    if not groups:
        await update.message.reply_text("Спочатку створи хоча б одну групу.")
        return ConversationHandler.END

    text = "Введи ID учня для переведення в групу:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']}\n"

    await update.message.reply_text(text)
    return MOVE_STUDENT_ID


async def move_student_choose_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return MOVE_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student or student["is_active"] == 0:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    context.user_data["move_student_id"] = student_id
    context.user_data["move_student_name"] = student["name"]

    groups = get_all_groups()
    text = "Введи ID групи, в яку треба перевести учня:\n\n"
    for g in groups:
        text += f"{g['id']}. {g['name']} | {g['lesson_day']} {g['lesson_time']}\n"

    await update.message.reply_text(text)
    return MOVE_GROUP_ID


async def move_student_choose_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        group_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID групи.")
        return MOVE_GROUP_ID

    groups = get_all_groups()
    valid_group_ids = [g["id"] for g in groups]

    if group_id not in valid_group_ids:
        await update.message.reply_text("Такої групи немає.")
        return MOVE_GROUP_ID

    student_id = context.user_data["move_student_id"]
    student_name = context.user_data["move_student_name"]

    move_student_to_group(student_id, group_id)
    group_name = next(g["name"] for g in groups if g["id"] == group_id)

    await update.message.reply_text(
        f"Учня переведено в групу.\nУчень: {student_name}\nГрупа: {group_name}"
    )
    return ConversationHandler.END


async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Активних учнів немає.")
        return ConversationHandler.END

    text = "Введи ID учня для зміни розкладу:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']}\n"

    await update.message.reply_text(text)
    return SCHEDULE_STUDENT_ID


async def schedule_choose_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return SCHEDULE_STUDENT_ID

    student = get_student_by_id(student_id)
    if not student or student["is_active"] == 0:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    context.user_data["schedule_student_id"] = student_id
    context.user_data["schedule_student_name"] = student["name"]

    await update.message.reply_text("Введи новий день занять:")
    return SCHEDULE_DAY


async def schedule_choose_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["schedule_day"] = update.message.text
    await update.message.reply_text("Введи новий час занять:")
    return SCHEDULE_TIME


async def schedule_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lesson_time = update.message.text
    student_id = context.user_data["schedule_student_id"]
    student_name = context.user_data["schedule_student_name"]
    lesson_day = context.user_data["schedule_day"]

    update_individual_schedule(student_id, lesson_day, lesson_time)

    await update.message.reply_text(
        f"Розклад оновлено.\nУчень: {student_name}\nНовий розклад: {lesson_day} {lesson_time}"
    )
    return ConversationHandler.END


async def add_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_all_students()

    if not students:
        await update.message.reply_text("Спочатку потрібно створити хоча б одного учня.")
        return ConversationHandler.END

    text = "Введи ID учня для додавання оплати:\n\n"
    for s in students:
        text += format_student_line(s) + "\n"

    await update.message.reply_text(text)
    return PAYMENT_STUDENT_ID


async def add_payment_student_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return PAYMENT_STUDENT_ID

    students = get_all_students()
    valid_ids = [s["id"] for s in students]

    if student_id not in valid_ids:
        await update.message.reply_text("Такого учня немає.")
        return PAYMENT_STUDENT_ID

    context.user_data["payment_student_id"] = student_id
    await update.message.reply_text("Введи суму оплати:")
    return PAYMENT_AMOUNT


async def add_payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введи коректну суму.")
        return PAYMENT_AMOUNT

    if amount <= 0:
        await update.message.reply_text("Сума має бути більшою за 0.")
        return PAYMENT_AMOUNT

    context.user_data["payment_amount"] = amount
    await update.message.reply_text("Введи кількість оплачених уроків:")
    return PAYMENT_LESSONS


async def add_payment_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lessons_paid = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи ціле число уроків.")
        return PAYMENT_LESSONS

    if lessons_paid <= 0:
        await update.message.reply_text("Кількість уроків має бути більшою за 0.")
        return PAYMENT_LESSONS

    student_id = context.user_data["payment_student_id"]
    amount = context.user_data["payment_amount"]

    students = get_all_students()
    student = next((s for s in students if s["id"] == student_id), None)

    if not student:
        await update.message.reply_text("Учня не знайдено.")
        return ConversationHandler.END

    add_payment(student_id, amount, lessons_paid)
    new_balance = student["remaining_lessons"] + lessons_paid

    await update.message.reply_text(
        f"Оплату додано:\n"
        f"Учень: {student['name']}\n"
        f"Сума: {amount}\n"
        f"Уроків додано: {lessons_paid}\n"
        f"Новий залишок: {new_balance}"
    )
    return ConversationHandler.END


async def low_balance_students(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_low_balance_students()

    if not students:
        await update.message.reply_text("У всіх учнів залишок більше 1 уроку.")
        return

    group_students = []
    individual_students = []

    for s in students:
        if s["student_type"] == "group":
            group_students.append(s)
        else:
            individual_students.append(s)

    text = "Учні з малим залишком:\n\n"

    if individual_students:
        text += "Індивідуальні:\n"
        for s in individual_students:
            text += f"- {s['name']} — залишок {s['remaining_lessons']}\n"
        text += "\n"

    if group_students:
        grouped = {}
        for s in group_students:
            group_name = s["group_name"] or "Без групи"
            if group_name not in grouped:
                grouped[group_name] = []
            grouped[group_name].append(s)

        text += "Групові:\n"
        for group_name, members in grouped.items():
            text += f"\n{group_name}:\n"
            for s in members:
                text += f"- {s['name']} — залишок {s['remaining_lessons']}\n"

    await update.message.reply_text(text)


async def start_individual_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_individual_students()

    if not students:
        await update.message.reply_text("Індивідуальних учнів поки немає.")
        return ConversationHandler.END

    text = "Введи ID учня для проведення уроку:\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']} | {s['lesson_day']} {s['lesson_time']} | Залишок: {s['remaining_lessons']}\n"

    await update.message.reply_text(text)
    return LESSON_INDIVIDUAL_ID


async def lesson_individual_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        student_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID учня.")
        return LESSON_INDIVIDUAL_ID

    students = get_individual_students()
    valid_ids = [s["id"] for s in students]

    if student_id not in valid_ids:
        await update.message.reply_text("Такого індивідуального учня немає.")
        return LESSON_INDIVIDUAL_ID

    result = complete_individual_lesson(student_id)

    if not result["success"]:
        await update.message.reply_text(result["message"])
        return ConversationHandler.END

    extra = ""
    if result["remaining_lessons"] == 1:
        extra = "\nУвага: залишився 1 урок."
    elif result["remaining_lessons"] == 0:
        extra = "\nУвага: оплачені уроки закінчились."
    elif result["remaining_lessons"] < 0:
        extra = f"\nУвага: учень у боргу на {abs(result['remaining_lessons'])} урок(ів)."

    await update.message.reply_text(
        f"Урок проведено.\n"
        f"Учень: {result['student_name']}\n"
        f"Новий залишок: {result['remaining_lessons']}{extra}"
    )
    return ConversationHandler.END


async def start_group_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = get_all_groups()

    if not groups:
        await update.message.reply_text("Груп поки немає.")
        return ConversationHandler.END

    text = "Введи ID групи для проведення заняття:\n\n"
    for g in groups:
        text += f"{g['id']}. {g['name']} | {g['lesson_day']} {g['lesson_time']}\n"

    await update.message.reply_text(text)
    return LESSON_GROUP_ID


async def lesson_group_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        group_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID групи.")
        return LESSON_GROUP_ID

    groups = get_all_groups()
    valid_group_ids = [g["id"] for g in groups]

    if group_id not in valid_group_ids:
        await update.message.reply_text("Такої групи немає.")
        return LESSON_GROUP_ID

    students = get_students_by_group_id(group_id)

    if not students:
        await update.message.reply_text("У цій групі поки немає учнів.")
        return ConversationHandler.END

    context.user_data["lesson_group_id"] = group_id

    text = "Введи ID учнів, які були присутні, через кому.\n\n"
    for s in students:
        text += f"{s['id']}. {s['name']} | Залишок: {s['remaining_lessons']}\n"

    text += "\nПриклад: 2,3,5"
    await update.message.reply_text(text)
    return LESSON_GROUP_PRESENT


async def lesson_group_present(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data["lesson_group_id"]
    students = get_students_by_group_id(group_id)
    valid_ids = [s["id"] for s in students]

    raw_input = update.message.text.strip()

    try:
        present_ids = [int(x.strip()) for x in raw_input.split(",") if x.strip()]
    except ValueError:
        await update.message.reply_text("Введи тільки ID через кому. Наприклад: 2,3,5")
        return LESSON_GROUP_PRESENT

    invalid_ids = [sid for sid in present_ids if sid not in valid_ids]
    if invalid_ids:
        await update.message.reply_text(
            f"У цій групі немає учнів з ID: {', '.join(map(str, invalid_ids))}"
        )
        return LESSON_GROUP_PRESENT

    result = complete_group_lesson(group_id, present_ids)

    if not result["success"]:
        await update.message.reply_text(result["message"])
        return ConversationHandler.END

    text = f"Групове заняття проведено.\nГрупа: {result['group_name']}\n\n"

    if result["written_off_students"]:
        text += "Списано урок:\n"
        for name in result["written_off_students"]:
            text += f"- {name}\n"
        text += "\n"

    if result["absent_students"]:
        text += "Відсутні:\n"
        for name in result["absent_students"]:
            text += f"- {name}\n"
        text += "\n"

    if result["debt_students"]:
        text += "Пішли в борг після заняття:\n"
        for name in result["debt_students"]:
            text += f"- {name}\n"
        text += "\n"

    alert_names = list(dict.fromkeys(result["low_balance"] + result["zero_balance"]))
    if alert_names:
        text += "Потрібна увага по оплаті:\n"
        for name in alert_names:
            if name in result["zero_balance"]:
                text += f"- {name} — уроки закінчились\n"
            elif name in result["low_balance"]:
                text += f"- {name} — залишився 1 урок\n"

    await update.message.reply_text(text)
    return ConversationHandler.END


async def reminders_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_summary_text())


async def individual_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_individual_payment_alerts()

    if not students:
        await update.message.reply_text("По індивідуальних учнях нагадувань немає.")
        return

    text = "Індивідуальні нагадування:\n\n"
    for student in students:
        if student["remaining_lessons"] < 0:
            text += f"- {student['name']} — борг {abs(student['remaining_lessons'])} урок(ів)\n"
        elif student["remaining_lessons"] == 0:
            text += f"- {student['name']} — уроки закінчились\n"
        else:
            text += f"- {student['name']} — залишився 1 урок\n"

    await update.message.reply_text(text)


async def group_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    grouped_alerts = get_grouped_payment_alerts()

    if not grouped_alerts:
        await update.message.reply_text("По групах нагадувань немає.")
        return

    text = "Групові нагадування:\n"
    for group_name, students in grouped_alerts.items():
        text += f"\n{group_name}:\n"
        for student in students:
            if student["remaining_lessons"] < 0:
                text += f"- {student['name']} — борг {abs(student['remaining_lessons'])} урок(ів)\n"
            elif student["remaining_lessons"] == 0:
                text += f"- {student['name']} — уроки закінчились\n"
            else:
                text += f"- {student['name']} — залишився 1 урок\n"

    await update.message.reply_text(text)


async def debtors_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_debt_students()

    if not students:
        await update.message.reply_text("Боржників зараз немає.")
        return

    text = "Боржники:\n\n"
    for student in students:
        if student["student_type"] == "group":
            text += f"- {student['name']} | {student['group_name']} | борг {abs(student['remaining_lessons'])} урок(ів)\n"
        else:
            text += f"- {student['name']} | Індивідуально | борг {abs(student['remaining_lessons'])} урок(ів)\n"

    await update.message.reply_text(text)


async def enable_auto_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("auto_reminders_enabled", "1")
    await update.message.reply_text("Автонагадування увімкнено.")


async def disable_auto_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("auto_reminders_enabled", "0")
    await update.message.reply_text("Автонагадування вимкнено.")


async def auto_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_hour = get_setting("auto_reminders_hour", "09")
    current_minute = get_setting("auto_reminders_minute", "00")
    await update.message.reply_text(
        f"Поточний час автонагадування: {current_hour}:{current_minute}\n"
        f"Введи новий час у форматі HH:MM"
    )
    return AUTO_TIME_VALUE


async def auto_time_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()

    try:
        hour_str, minute_str = value.split(":")
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError:
        await update.message.reply_text("Неправильний формат. Введи так: 09:30")
        return AUTO_TIME_VALUE

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await update.message.reply_text("Години мають бути 0-23, хвилини 0-59.")
        return AUTO_TIME_VALUE

    set_setting("auto_reminders_hour", f"{hour:02d}")
    set_setting("auto_reminders_minute", f"{minute:02d}")

    await update.message.reply_text(f"Час автонагадування змінено на {hour:02d}:{minute:02d}")
    return ConversationHandler.END


async def today_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_idx = get_local_now().weekday()

    individuals = []
    groups_today = []

    for student in get_all_students():
        if student["student_type"] == "individual":
            if normalize_weekday(student["lesson_day"]) == today_idx:
                individuals.append(student)

    for group in get_all_groups():
        if normalize_weekday(group["lesson_day"]) == today_idx:
            groups_today.append(group)

    if not individuals and not groups_today:
        await update.message.reply_text("Сьогодні запланованих уроків не знайдено.")
        return

    text = "Сьогоднішні уроки:\n\n"

    if individuals:
        text += "Індивідуальні:\n"
        for student in individuals:
            text += f"- {student['name']} — {student['lesson_time']} | залишок {student['remaining_lessons']}\n"
        text += "\n"

    if groups_today:
        text += "Групи:\n"
        for group in groups_today:
            members = get_students_by_group_id(group["id"])
            text += f"- {group['name']} — {group['lesson_time']} | учнів: {len(members)}\n"

    await update.message.reply_text(text)


async def search_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи ім'я або частину імені для пошуку:")
    return SEARCH_NAME_QUERY


async def search_student_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()

    if not query_text:
        await update.message.reply_text("Порожній запит.")
        return ConversationHandler.END

    students = search_students_by_name(query_text)

    if not students:
        await update.message.reply_text("Нічого не знайдено.")
        return ConversationHandler.END

    text = f"Результати пошуку: {query_text}\n\n"
    for student in students:
        status = "Активний" if student["is_active"] == 1 else "Неактивний"
        if student["student_type"] == "individual":
            text += (
                f"{student['id']}. {student['name']} | {status} | "
                f"Індивідуально | {student['lesson_day']} {student['lesson_time']} | "
                f"Залишок: {student['remaining_lessons']}\n"
            )
        else:
            text += (
                f"{student['id']}. {student['name']} | {status} | "
                f"Група: {student['group_name']} | "
                f"Залишок: {student['remaining_lessons']}\n"
            )

    await update.message.reply_text(text)
    return ConversationHandler.END


async def filter_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = get_all_groups()

    if not groups:
        await update.message.reply_text("Груп поки немає.")
        return ConversationHandler.END

    text = "Введи ID групи:\n\n"
    for g in groups:
        text += f"{g['id']}. {g['name']} | {g['lesson_day']} {g['lesson_time']}\n"

    await update.message.reply_text(text)
    return FILTER_GROUP_ID


async def filter_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        group_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Введи числовий ID групи.")
        return FILTER_GROUP_ID

    group = get_group_by_id(group_id)
    if not group:
        await update.message.reply_text("Такої групи немає.")
        return ConversationHandler.END

    students = get_students_by_group_id(group_id)

    if not students:
        await update.message.reply_text(f"У групі {group['name']} активних учнів немає.")
        return ConversationHandler.END

    text = f"Учні групи {group['name']}:\n\n"
    for student in students:
        text += f"- {student['id']}. {student['name']} | Залишок: {student['remaining_lessons']}\n"

    await update.message.reply_text(text)
    return ConversationHandler.END


async def inactive_students_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    students = get_inactive_students()

    if not students:
        await update.message.reply_text("Неактивних учнів немає.")
        return

    text = "Неактивні учні:\n\n"
    for student in students:
        if student["student_type"] == "group":
            text += f"- {student['id']}. {student['name']} | Група: {student['group_name']} | Залишок: {student['remaining_lessons']}\n"
        else:
            text += f"- {student['id']}. {student['name']} | Індивідуально | Залишок: {student['remaining_lessons']}\n"

    await update.message.reply_text(text)


async def problem_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_summary_text())


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "Групи":
        await groups_menu(update, context)
    elif text == "Список груп":
        await list_groups(update, context)

    elif text == "Учні":
        await students_menu(update, context)
    elif text == "Список учнів":
        await list_students(update, context)

    elif text == "Оплати":
        await payments_menu(update, context)
    elif text == "Учні з малим залишком":
        await low_balance_students(update, context)

    elif text == "Уроки":
        await lessons_menu(update, context)

    elif text == "Нагадування":
        await reminders_menu(update, context)
    elif text == "Загальне зведення":
        await reminders_summary(update, context)
    elif text == "Індивідуальні нагадування":
        await individual_reminders(update, context)
    elif text == "Групові нагадування":
        await group_reminders(update, context)
    elif text == "Боржники":
        await debtors_list(update, context)
    elif text == "Увімкнути автонагадування":
        await enable_auto_reminders(update, context)
    elif text == "Вимкнути автонагадування":
        await disable_auto_reminders(update, context)

    elif text == "Швидкі дії":
        await quick_actions_menu(update, context)
    elif text == "Сьогоднішні уроки":
        await today_lessons(update, context)

    elif text == "Пошук і фільтри":
        await search_filters_menu(update, context)
    elif text == "Неактивні учні":
        await inactive_students_list(update, context)
    elif text == "Проблемні баланси":
        await problem_balances(update, context)

    elif text == "Назад":
        await start(update, context)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не знайдено")

    init_db()
    ensure_default_settings()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    group_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Додати групу$"), add_group_start)],
        states={
            GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_name)],
            GROUP_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_day)],
            GROUP_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_time)],
        },
        fallbacks=[]
    )

    student_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Додати учня$"), add_student_start)],
        states={
            STUDENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_student_type)],
            STUDENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_student_name)],
            STUDENT_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_student_day)],
            STUDENT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_student_time)],
            STUDENT_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_student_group)],
        },
        fallbacks=[]
    )

    payment_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex("^Додати оплату$"), add_payment_start),
            MessageHandler(filters.TEXT & filters.Regex("^Швидка оплата$"), add_payment_start),
        ],
        states={
            PAYMENT_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_student_id)],
            PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_amount)],
            PAYMENT_LESSONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment_lessons)],
        },
        fallbacks=[]
    )

    individual_lesson_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex("^Індивідуальний урок$"), start_individual_lesson),
            MessageHandler(filters.TEXT & filters.Regex("^Швидке індивідуальне списання$"), start_individual_lesson),
        ],
        states={
            LESSON_INDIVIDUAL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, lesson_individual_complete)],
        },
        fallbacks=[]
    )

    group_lesson_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex("^Групове заняття$"), start_group_lesson),
            MessageHandler(filters.TEXT & filters.Regex("^Швидке групове списання$"), start_group_lesson),
        ],
        states={
            LESSON_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, lesson_group_choose)],
            LESSON_GROUP_PRESENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lesson_group_present)],
        },
        fallbacks=[]
    )

    card_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Картка учня$"), show_student_card_start)],
        states={
            CARD_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_student_card_finish)],
        },
        fallbacks=[]
    )

    history_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Історія оплат$"), student_history_start)],
        states={
            HISTORY_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_history_finish)],
        },
        fallbacks=[]
    )

    deactivate_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Деактивувати учня$"), deactivate_student_start)],
        states={
            DEACTIVATE_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deactivate_student_finish)],
        },
        fallbacks=[]
    )

    balance_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Змінити баланс$"), update_balance_start)],
        states={
            BALANCE_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_balance_student)],
            BALANCE_NEW_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_balance_finish)],
        },
        fallbacks=[]
    )

    move_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Перевести в групу$"), move_student_start)],
        states={
            MOVE_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_student_choose_student)],
            MOVE_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_student_choose_group)],
        },
        fallbacks=[]
    )

    schedule_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Змінити розклад$"), schedule_start)],
        states={
            SCHEDULE_STUDENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_choose_student)],
            SCHEDULE_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_choose_day)],
            SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_finish)],
        },
        fallbacks=[]
    )

    search_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Пошук учня$"), search_student_start)],
        states={
            SEARCH_NAME_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_student_finish)],
        },
        fallbacks=[]
    )

    filter_group_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Учні групи$"), filter_group_start)],
        states={
            FILTER_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, filter_group_finish)],
        },
        fallbacks=[]
    )

    auto_time_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Змінити час автонагадування$"), auto_time_start)],
        states={
            AUTO_TIME_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_time_finish)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(group_conv_handler)
    app.add_handler(student_conv_handler)
    app.add_handler(payment_conv_handler)
    app.add_handler(individual_lesson_conv_handler)
    app.add_handler(group_lesson_conv_handler)
    app.add_handler(card_handler)
    app.add_handler(history_handler)
    app.add_handler(deactivate_handler)
    app.add_handler(balance_handler)
    app.add_handler(move_handler)
    app.add_handler(schedule_handler)
    app.add_handler(search_handler)
    app.add_handler(filter_group_handler)
    app.add_handler(auto_time_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()