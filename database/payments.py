from datetime import datetime

from database.db import get_connection


def add_payment(student_id: int, amount: float, lessons_paid: int, notes: str = ""):
    payment_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO payments (student_id, payment_date, amount, lessons_paid, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (student_id, payment_date, amount, lessons_paid, notes))

        cursor.execute("""
            UPDATE students
            SET remaining_lessons = remaining_lessons + ?,
                last_payment_date = ?
            WHERE id = ?
        """, (lessons_paid, payment_date, student_id))

        conn.commit()


def get_payments_by_student(student_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT *
            FROM payments
            WHERE student_id = ?
            ORDER BY id DESC
        """, (student_id,))
        return cursor.fetchall()


def get_low_balance_students():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.id,
                s.name,
                s.student_type,
                s.remaining_lessons,
                g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.is_active = 1
              AND s.remaining_lessons <= 1
            ORDER BY s.remaining_lessons ASC, s.name ASC
        """)
        return cursor.fetchall()


def get_debt_students():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.id,
                s.name,
                s.student_type,
                s.remaining_lessons,
                g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.is_active = 1
              AND s.remaining_lessons < 0
            ORDER BY s.remaining_lessons ASC, s.name ASC
        """)
        return cursor.fetchall()


def get_grouped_payment_alerts():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                s.id,
                s.name,
                s.student_type,
                s.remaining_lessons,
                g.id AS group_id,
                g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.is_active = 1
              AND s.student_type = 'group'
              AND s.remaining_lessons <= 1
            ORDER BY g.name, s.remaining_lessons ASC, s.name ASC
        """)
        rows = cursor.fetchall()

    grouped = {}
    for row in rows:
        group_name = row["group_name"] or "Без групи"
        if group_name not in grouped:
            grouped[group_name] = []
        grouped[group_name].append(row)

    return grouped


def get_individual_payment_alerts():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                id,
                name,
                remaining_lessons
            FROM students
            WHERE is_active = 1
              AND student_type = 'individual'
              AND remaining_lessons <= 1
            ORDER BY remaining_lessons ASC, name ASC
        """)
        return cursor.fetchall()