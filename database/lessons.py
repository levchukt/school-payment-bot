"""Уроки та відвідуваність.

Групове заняття: урок списується з УСІХ активних учнів групи, а відмітка
present/absent зберігає лише факт присутності.
Скасування заняття нічого не видаляє: урок отримує статус 'cancelled',
списані уроки повертаються на баланс.
"""
from database.db import get_connection, name_key


def lessons_on_date(day: str):
    """Що вже проведено за дату YYYY-MM-DD: ({group_id}, {student_id})."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT lesson_type, student_id, group_id
            FROM lessons
            WHERE status = 'completed' AND substr(lesson_date, 1, 10) = ?
        """, (day,)).fetchall()

    groups = {r["group_id"] for r in rows if r["lesson_type"] == "group"}
    students = {r["student_id"] for r in rows if r["lesson_type"] == "individual"}
    return groups, students


def complete_individual_lesson(student_id: int, lesson_date: str, notes: str = ""):
    with get_connection() as conn:
        student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student:
            return {"success": False, "message": "Учня не знайдено"}

        cursor = conn.execute("""
            INSERT INTO lessons (lesson_type, student_id, group_id, lesson_date, status, notes)
            VALUES ('individual', ?, NULL, ?, 'completed', ?)
        """, (student_id, lesson_date, notes))
        lesson_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO attendance (lesson_id, student_id, attendance_status, lesson_written_off, notes)
            VALUES (?, ?, 'present', 1, ?)
        """, (lesson_id, student_id, notes))

        conn.execute(
            "UPDATE students SET remaining_lessons = remaining_lessons - 1 WHERE id = ?",
            (student_id,),
        )

        return {
            "success": True,
            "lesson_id": lesson_id,
            "student_name": student["name"],
            "remaining_lessons": student["remaining_lessons"] - 1,
        }


def complete_group_lesson(group_id: int, present_student_ids, lesson_date: str, notes: str = ""):
    """Списує урок з усіх активних учнів групи та зберігає відвідуваність."""
    present_ids = set(present_student_ids)

    with get_connection() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not group:
            return {"success": False, "message": "Групу не знайдено"}

        students = conn.execute(
            "SELECT * FROM students WHERE is_active = 1 AND group_id = ?", (group_id,)
        ).fetchall()
        if not students:
            return {"success": False, "message": "У групі немає учнів"}

        cursor = conn.execute("""
            INSERT INTO lessons (lesson_type, student_id, group_id, lesson_date, status, notes)
            VALUES ('group', NULL, ?, ?, 'completed', ?)
        """, (group_id, lesson_date, notes))
        lesson_id = cursor.lastrowid

        conn.executemany("""
            INSERT INTO attendance (lesson_id, student_id, attendance_status, lesson_written_off, notes)
            VALUES (?, ?, ?, 1, ?)
        """, [
            (lesson_id, s["id"], "present" if s["id"] in present_ids else "absent", notes)
            for s in students
        ])

        conn.execute("""
            UPDATE students
            SET remaining_lessons = remaining_lessons - 1
            WHERE is_active = 1 AND group_id = ?
        """, (group_id,))

        rows = [
            {
                "id": s["id"],
                "name": s["name"],
                "present": s["id"] in present_ids,
                "balance": s["remaining_lessons"] - 1,
            }
            for s in sorted(students, key=lambda r: name_key(r["name"]))
        ]

        return {
            "success": True,
            "lesson_id": lesson_id,
            "group_name": group["name"],
            "students": rows,
        }


def get_lesson(lesson_id: int):
    """Урок + рядки відвідуваності з іменами та поточними балансами."""
    with get_connection() as conn:
        lesson = conn.execute("""
            SELECT l.*, g.name AS group_name, s.name AS student_name
            FROM lessons l
            LEFT JOIN groups g ON g.id = l.group_id
            LEFT JOIN students s ON s.id = l.student_id
            WHERE l.id = ?
        """, (lesson_id,)).fetchone()
        if not lesson:
            return None, []

        rows = conn.execute("""
            SELECT a.*, s.name, s.remaining_lessons
            FROM attendance a JOIN students s ON s.id = a.student_id
            WHERE a.lesson_id = ?
        """, (lesson_id,)).fetchall()

    return lesson, sorted(rows, key=lambda r: name_key(r["name"]))


def set_attendance(lesson_id: int, student_id: int, present: bool) -> None:
    """Змінює лише відмітку присутності. Баланс не чіпає."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE attendance SET attendance_status = ?
            WHERE lesson_id = ? AND student_id = ?
        """, ("present" if present else "absent", lesson_id, student_id))


def cancel_lesson(lesson_id: int):
    """Скасовує проведене заняття і повертає списані уроки. Повертає кількість повернень."""
    with get_connection() as conn:
        lesson = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if not lesson or lesson["status"] != "completed":
            return None

        refunded = conn.execute("""
            UPDATE students
            SET remaining_lessons = remaining_lessons + 1
            WHERE id IN (SELECT student_id FROM attendance
                         WHERE lesson_id = ? AND lesson_written_off = 1)
        """, (lesson_id,)).rowcount

        conn.execute("UPDATE attendance SET lesson_written_off = 0 WHERE lesson_id = ?", (lesson_id,))
        conn.execute("UPDATE lessons SET status = 'cancelled' WHERE id = ?", (lesson_id,))
        return refunded


def get_group_attendance(group_id: int, month: str):
    """Відвідуваність групи за місяць YYYY-MM.

    Повертає (lessons, students, marks):
      lessons  — заняття за зростанням дати,
      students — [{id, name}] усі, хто має відмітки або зараз у групі,
      marks    — {(lesson_id, student_id): 'present' | 'absent' | ...}
    """
    with get_connection() as conn:
        lessons = conn.execute("""
            SELECT id, lesson_date FROM lessons
            WHERE group_id = ? AND lesson_type = 'group' AND status = 'completed'
              AND substr(lesson_date, 1, 7) = ?
            ORDER BY lesson_date, id
        """, (group_id, month)).fetchall()

        records = conn.execute("""
            SELECT a.lesson_id, a.student_id, a.attendance_status, s.name
            FROM attendance a
            JOIN lessons l ON l.id = a.lesson_id
            JOIN students s ON s.id = a.student_id
            WHERE l.group_id = ? AND l.lesson_type = 'group' AND l.status = 'completed'
              AND substr(l.lesson_date, 1, 7) = ?
        """, (group_id, month)).fetchall()

        current = conn.execute(
            "SELECT id, name FROM students WHERE group_id = ? AND is_active = 1", (group_id,)
        ).fetchall()

    names = {r["id"]: r["name"] for r in current}
    names.update({r["student_id"]: r["name"] for r in records})
    students = sorted(
        ({"id": sid, "name": name} for sid, name in names.items()),
        key=lambda r: name_key(r["name"]),
    )
    marks = {(r["lesson_id"], r["student_id"]): r["attendance_status"] for r in records}
    return lessons, students, marks


def get_student_lessons(student_id: int, limit: int = 15):
    with get_connection() as conn:
        return conn.execute("""
            SELECT l.id, l.lesson_date, l.lesson_type, a.attendance_status, a.lesson_written_off
            FROM attendance a JOIN lessons l ON l.id = a.lesson_id
            WHERE a.student_id = ? AND l.status = 'completed'
            ORDER BY l.lesson_date DESC, l.id DESC
            LIMIT ?
        """, (student_id, limit)).fetchall()


def get_student_attendance_stats(student_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(a.attendance_status = 'present'), 0) AS present
            FROM attendance a JOIN lessons l ON l.id = a.lesson_id
            WHERE a.student_id = ? AND l.status = 'completed'
        """, (student_id,)).fetchone()
