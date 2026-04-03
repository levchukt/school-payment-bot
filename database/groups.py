from database.db import get_connection


def create_group(name: str, lesson_day: str, lesson_time: str, notes: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO groups (name, lesson_day, lesson_time, notes)
            VALUES (?, ?, ?, ?)
        """, (name, lesson_day, lesson_time, notes))
        conn.commit()


def get_all_groups():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM groups ORDER BY id")
        return cursor.fetchall()


def get_group_by_id(group_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        return cursor.fetchone()