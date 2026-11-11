from database.db import get_connection, name_key

STUDENT_SELECT = """
    SELECT s.*,
           g.name AS group_name,
           g.lesson_day AS group_lesson_day,
           g.lesson_time AS group_lesson_time
    FROM students s
    LEFT JOIN groups g ON s.group_id = g.id
"""


def _by_name(rows):
    # SQLite не вміє правильно порівнювати кирилицю без урахування регістру,
    # тому сортуємо і шукаємо в Python.
    return sorted(rows, key=lambda r: name_key(r["name"]))


def create_individual_student(name: str, lesson_day: str = "", lesson_time: str = "", notes: str = "") -> int:
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO students (name, student_type, group_id, lesson_day, lesson_time,
                                  remaining_lessons, last_payment_date, notes, is_active)
            VALUES (?, 'individual', NULL, ?, ?, 0, NULL, ?, 1)
        """, (name, lesson_day, lesson_time, notes))
        return cursor.lastrowid


def create_group_student(name: str, group_id: int, notes: str = "") -> int:
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO students (name, student_type, group_id, lesson_day, lesson_time,
                                  remaining_lessons, last_payment_date, notes, is_active)
            VALUES (?, 'group', ?, NULL, NULL, 0, NULL, ?, 1)
        """, (name, group_id, notes))
        return cursor.lastrowid


def get_all_students(include_inactive: bool = False):
    query = STUDENT_SELECT
    if not include_inactive:
        query += " WHERE s.is_active = 1"
    with get_connection() as conn:
        return _by_name(conn.execute(query).fetchall())


def get_student_by_id(student_id: int):
    with get_connection() as conn:
        return conn.execute(STUDENT_SELECT + " WHERE s.id = ?", (student_id,)).fetchone()


def get_individual_students():
    with get_connection() as conn:
        return _by_name(conn.execute(
            STUDENT_SELECT + " WHERE s.is_active = 1 AND s.student_type = 'individual'"
        ).fetchall())


def get_students_by_group_id(group_id: int, include_inactive: bool = False):
    query = STUDENT_SELECT + " WHERE s.group_id = ?"
    if not include_inactive:
        query += " AND s.is_active = 1"
    with get_connection() as conn:
        return _by_name(conn.execute(query, (group_id,)).fetchall())


def get_inactive_students():
    with get_connection() as conn:
        return _by_name(conn.execute(STUDENT_SELECT + " WHERE s.is_active = 0").fetchall())


def search_students_by_name(query_text: str):
    needle = query_text.casefold().strip()
    rows = get_all_students(include_inactive=True)
    found = [r for r in rows if needle in r["name"].casefold()]
    return sorted(found, key=lambda r: (-r["is_active"], name_key(r["name"])))


def set_student_active(student_id: int, is_active: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE students SET is_active = ? WHERE id = ?", (int(is_active), student_id))


def deactivate_student(student_id: int) -> None:
    set_student_active(student_id, False)


def rename_student(student_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE students SET name = ? WHERE id = ?", (name, student_id))


def update_student_notes(student_id: int, notes: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE students SET notes = ? WHERE id = ?", (notes, student_id))


def update_student_balance(student_id: int, new_balance: int, changed_at: str, reason: str = "") -> int | None:
    """Ручна зміна балансу. Кожна зміна записується в balance_log. Повертає старий баланс."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT remaining_lessons FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        if not row:
            return None

        old_balance = row["remaining_lessons"]
        conn.execute("UPDATE students SET remaining_lessons = ? WHERE id = ?", (new_balance, student_id))
        conn.execute("""
            INSERT INTO balance_log (student_id, changed_at, old_balance, new_balance, reason)
            VALUES (?, ?, ?, ?, ?)
        """, (student_id, changed_at, old_balance, new_balance, reason))
        return old_balance


def move_student_to_group(student_id: int, group_id: int) -> None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE students
            SET student_type = 'group', group_id = ?, lesson_day = NULL, lesson_time = NULL
            WHERE id = ?
        """, (group_id, student_id))


def make_student_individual(student_id: int) -> None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE students
            SET student_type = 'individual', group_id = NULL
            WHERE id = ?
        """, (student_id,))


def update_individual_schedule(student_id: int, lesson_day: str, lesson_time: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE students SET lesson_day = ?, lesson_time = ? WHERE id = ?",
            (lesson_day, lesson_time, student_id),
        )
