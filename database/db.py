"""Підключення до SQLite, створення таблиць, безпечні міграції та бекапи.

Правило міграцій: дані НІКОЛИ не видаляються і таблиці не перестворюються.
Дозволені лише CREATE TABLE IF NOT EXISTS, ALTER TABLE ... ADD COLUMN
та CREATE INDEX IF NOT EXISTS. Перед першою зміною схеми робиться бекап.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_DB_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "school_bot.db"

ENV_DB_PATH = os.getenv("DB_PATH", "").strip()
DB_PATH = Path(ENV_DB_PATH) if ENV_DB_PATH else DEFAULT_DB_PATH
DB_DIR = DB_PATH.parent
BACKUP_DIR = DB_DIR / "backups"
KEEP_BACKUPS = 15


@contextmanager
def get_connection():
    """Одна транзакція: commit при успіху, rollback при помилці, завжди close."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
_UK_ORDER = {char: index for index, char in enumerate(UK_ALPHABET)}


def name_key(name: str):
    """Ключ сортування за українським алфавітом (І, Ї, Є, Ґ стають на свої місця).

    SQLite і звичайне сортування Python ставлять ці літери в кінець списку.
    """
    return [(1, _UK_ORDER[c]) if c in _UK_ORDER else (0, ord(c)) for c in (name or "").casefold()]


def backup_db(tag: str = "manual") -> Path | None:
    """Робить цілісну копію бази у data/backups/. Повертає шлях до файлу."""
    if not DB_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"school_bot_{stamp}_{tag}.db"

    source = sqlite3.connect(DB_PATH, timeout=10)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    backups = sorted(BACKUP_DIR.glob("school_bot_*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Не вдалося видалити старий бекап %s", old)

    return target


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


# (таблиця, колонка, SQL-визначення). Лише додавання колонок.
ADDED_COLUMNS = [
    ("groups", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("payments", "is_cancelled", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db() -> None:
    db_existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0

    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lesson_day TEXT,
            lesson_time TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            student_type TEXT NOT NULL CHECK(student_type IN ('individual', 'group')),
            group_id INTEGER,
            lesson_day TEXT,
            lesson_time TEXT,
            remaining_lessons INTEGER NOT NULL DEFAULT 0,
            last_payment_date TEXT,
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (group_id) REFERENCES groups(id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            amount REAL NOT NULL,
            lessons_paid INTEGER NOT NULL,
            notes TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL CHECK(lesson_type IN ('individual', 'group')),
            student_id INTEGER,
            group_id INTEGER,
            lesson_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('scheduled', 'completed', 'cancelled', 'rescheduled')),
            notes TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id),
            FOREIGN KEY (group_id) REFERENCES groups(id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            attendance_status TEXT NOT NULL CHECK(attendance_status IN ('present', 'absent', 'cancelled', 'excused')),
            lesson_written_off INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            FOREIGN KEY (lesson_id) REFERENCES lessons(id),
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        missing = [
            (table, column, definition)
            for table, column, definition in ADDED_COLUMNS
            if column not in _columns(conn, table)
        ]

    if missing and db_existed:
        backup = backup_db("before_migration")
        logger.info("Бекап перед міграцією: %s", backup)

    with get_connection() as conn:
        for table, column, definition in missing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info("Міграція: додано %s.%s", table, column)

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS balance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            changed_at TEXT NOT NULL,
            old_balance INTEGER NOT NULL,
            new_balance INTEGER NOT NULL,
            reason TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE INDEX IF NOT EXISTS idx_students_group ON students(group_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_payments_student ON payments(student_id);
        CREATE INDEX IF NOT EXISTS idx_lessons_group_date ON lessons(group_id, lesson_date);
        CREATE INDEX IF NOT EXISTS idx_lessons_student_date ON lessons(student_id, lesson_date);
        CREATE INDEX IF NOT EXISTS idx_attendance_lesson ON attendance(lesson_id);
        CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id);
        """)
