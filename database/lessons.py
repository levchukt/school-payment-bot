from datetime import datetime

from database.db import get_connection


def complete_individual_lesson(student_id: int, notes: str = ""):
    lesson_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        student = cursor.fetchone()

        if not student:
            return {"success": False, "message": "Учня не знайдено"}

        cursor.execute("""
            INSERT INTO lessons (
                lesson_type,
                student_id,
                group_id,
                lesson_date,
                status,
                notes
            )
            VALUES ('individual', ?, NULL, ?, 'completed', ?)
        """, (student_id, lesson_date, notes))

        lesson_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO attendance (
                lesson_id,
                student_id,
                attendance_status,
                lesson_written_off,
                notes
            )
            VALUES (?, ?, 'present', 1, ?)
        """, (lesson_id, student_id, notes))

        cursor.execute("""
            UPDATE students
            SET remaining_lessons = remaining_lessons - 1
            WHERE id = ?
        """, (student_id,))

        conn.commit()

        cursor.execute("SELECT remaining_lessons, name FROM students WHERE id = ?", (student_id,))
        updated_student = cursor.fetchone()

        return {
            "success": True,
            "student_name": updated_student["name"],
            "remaining_lessons": updated_student["remaining_lessons"]
        }


def complete_group_lesson(group_id: int, present_student_ids: list[int], notes: str = ""):
    lesson_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        group = cursor.fetchone()

        if not group:
            return {"success": False, "message": "Групу не знайдено"}

        cursor.execute("""
            SELECT *
            FROM students
            WHERE is_active = 1 AND group_id = ?
            ORDER BY id
        """, (group_id,))
        students = cursor.fetchall()

        if not students:
            return {"success": False, "message": "У групі немає учнів"}

        cursor.execute("""
            INSERT INTO lessons (
                lesson_type,
                student_id,
                group_id,
                lesson_date,
                status,
                notes
            )
            VALUES ('group', NULL, ?, ?, 'completed', ?)
        """, (group_id, lesson_date, notes))

        lesson_id = cursor.lastrowid

        written_off_students = []
        absent_students = []
        debt_students = []

        for student in students:
            student_id = student["id"]
            is_present = student_id in present_student_ids

            if is_present:
                cursor.execute("""
                    INSERT INTO attendance (
                        lesson_id,
                        student_id,
                        attendance_status,
                        lesson_written_off,
                        notes
                    )
                    VALUES (?, ?, 'present', 1, ?)
                """, (lesson_id, student_id, notes))

                cursor.execute("""
                    UPDATE students
                    SET remaining_lessons = remaining_lessons - 1
                    WHERE id = ?
                """, (student_id,))

                cursor.execute("""
                    SELECT remaining_lessons
                    FROM students
                    WHERE id = ?
                """, (student_id,))
                updated_balance = cursor.fetchone()["remaining_lessons"]

                written_off_students.append(student["name"])

                if updated_balance < 0:
                    debt_students.append(student["name"])
            else:
                cursor.execute("""
                    INSERT INTO attendance (
                        lesson_id,
                        student_id,
                        attendance_status,
                        lesson_written_off,
                        notes
                    )
                    VALUES (?, ?, 'absent', 0, ?)
                """, (lesson_id, student_id, notes))

                absent_students.append(student["name"])

        conn.commit()

        low_balance = []
        zero_balance = []

        for student in students:
            cursor.execute("SELECT name, remaining_lessons FROM students WHERE id = ?", (student["id"],))
            updated_student = cursor.fetchone()

            if updated_student["remaining_lessons"] == 1:
                low_balance.append(updated_student["name"])

            if updated_student["remaining_lessons"] == 0:
                zero_balance.append(updated_student["name"])

        return {
            "success": True,
            "group_name": group["name"],
            "written_off_students": written_off_students,
            "absent_students": absent_students,
            "debt_students": debt_students,
            "low_balance": low_balance,
            "zero_balance": zero_balance
        }