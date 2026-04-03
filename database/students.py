from database.db import get_connection


def create_individual_student(name: str, lesson_day: str, lesson_time: str, notes: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO students (
                name,
                student_type,
                group_id,
                lesson_day,
                lesson_time,
                remaining_lessons,
                last_payment_date,
                notes,
                is_active
            )
            VALUES (?, 'individual', NULL, ?, ?, 0, NULL, ?, 1)
        """, (name, lesson_day, lesson_time, notes))
        conn.commit()


def create_group_student(name: str, group_id: int, notes: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO students (
                name,
                student_type,
                group_id,
                lesson_day,
                lesson_time,
                remaining_lessons,
                last_payment_date,
                notes,
                is_active
            )
            VALUES (?, 'group', ?, NULL, NULL, 0, NULL, ?, 1)
        """, (name, group_id, notes))
        conn.commit()


def get_all_students(include_inactive: bool = False):
    with get_connection() as conn:
        cursor = conn.cursor()

        query = """
            SELECT 
                s.id,
                s.name,
                s.student_type,
                s.group_id,
                s.lesson_day,
                s.lesson_time,
                s.remaining_lessons,
                s.last_payment_date,
                s.notes,
                s.is_active,
                g.name AS group_name,
                g.lesson_day AS group_lesson_day,
                g.lesson_time AS group_lesson_time
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
        """

        if not include_inactive:
            query += " WHERE s.is_active = 1"

        query += " ORDER BY s.id"

        cursor.execute(query)
        return cursor.fetchall()


def get_student_by_id(student_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.*,
                g.name AS group_name,
                g.lesson_day AS group_lesson_day,
                g.lesson_time AS group_lesson_time
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.id = ?
        """, (student_id,))
        return cursor.fetchone()


def get_individual_students():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT *
            FROM students
            WHERE is_active = 1
              AND student_type = 'individual'
            ORDER BY id
        """)
        return cursor.fetchall()


def get_students_by_group_id(group_id: int, include_inactive: bool = False):
    with get_connection() as conn:
        cursor = conn.cursor()

        query = """
            SELECT 
                s.*,
                g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.group_id = ?
        """

        if not include_inactive:
            query += " AND s.is_active = 1"

        query += " ORDER BY s.id"

        cursor.execute(query, (group_id,))
        return cursor.fetchall()


def get_inactive_students():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.*,
                g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.is_active = 0
            ORDER BY s.id
        """)
        return cursor.fetchall()


def search_students_by_name(query_text: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.*,
                g.name AS group_name,
                g.lesson_day AS group_lesson_day,
                g.lesson_time AS group_lesson_time
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE LOWER(s.name) LIKE LOWER(?)
            ORDER BY s.is_active DESC, s.name ASC
        """, (f"%{query_text}%",))
        return cursor.fetchall()


def deactivate_student(student_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE students
            SET is_active = 0
            WHERE id = ?
        """, (student_id,))
        conn.commit()


def update_student_balance(student_id: int, new_balance: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE students
            SET remaining_lessons = ?
            WHERE id = ?
        """, (new_balance, student_id))
        conn.commit()


def move_student_to_group(student_id: int, group_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE students
            SET student_type = 'group',
                group_id = ?,
                lesson_day = NULL,
                lesson_time = NULL
            WHERE id = ?
        """, (group_id, student_id))
        conn.commit()


def update_individual_schedule(student_id: int, lesson_day: str, lesson_time: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE students
            SET student_type = 'individual',
                group_id = NULL,
                lesson_day = ?,
                lesson_time = ?
            WHERE id = ?
        """, (lesson_day, lesson_time, student_id))
        conn.commit()