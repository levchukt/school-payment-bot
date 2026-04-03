import os
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_DB_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "school_bot.db"

ENV_DB_PATH = os.getenv("DB_PATH", "").strip()
DB_PATH = Path(ENV_DB_PATH) if ENV_DB_PATH else DEFAULT_DB_PATH
DB_DIR = DB_PATH.parent


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            lesson_day TEXT,
            lesson_time TEXT,
            notes TEXT
        )
        """)

        cursor.execute("""
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
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            amount REAL NOT NULL,
            lessons_paid INTEGER NOT NULL,
            notes TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
        """)

        cursor.execute("""
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
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            attendance_status TEXT NOT NULL CHECK(attendance_status IN ('present', 'absent', 'cancelled', 'excused')),
            lesson_written_off INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            FOREIGN KEY (lesson_id) REFERENCES lessons(id),
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        conn.commit()