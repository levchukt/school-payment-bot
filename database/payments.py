from database.db import get_connection, name_key


def add_payment(student_id: int, amount: float, lessons_paid: int, payment_date: str, notes: str = ""):
    """Додає оплату і поповнює баланс. Повертає {payment_id, balance} або None."""
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM students WHERE id = ?", (student_id,)).fetchone():
            return None

        cursor = conn.execute("""
            INSERT INTO payments (student_id, payment_date, amount, lessons_paid, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (student_id, payment_date, amount, lessons_paid, notes))

        conn.execute("""
            UPDATE students
            SET remaining_lessons = remaining_lessons + ?, last_payment_date = ?
            WHERE id = ?
        """, (lessons_paid, payment_date, student_id))

        balance = conn.execute(
            "SELECT remaining_lessons FROM students WHERE id = ?", (student_id,)
        ).fetchone()["remaining_lessons"]

        return {"payment_id": cursor.lastrowid, "balance": balance}


def get_payment(payment_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT p.*, s.name AS student_name
            FROM payments p JOIN students s ON s.id = p.student_id
            WHERE p.id = ?
        """, (payment_id,)).fetchone()


def cancel_payment(payment_id: int):
    """Скасовує оплату: запис лишається в базі з позначкою, уроки знімаються з балансу."""
    with get_connection() as conn:
        payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not payment or payment["is_cancelled"]:
            return None

        conn.execute("UPDATE payments SET is_cancelled = 1 WHERE id = ?", (payment_id,))
        conn.execute(
            "UPDATE students SET remaining_lessons = remaining_lessons - ? WHERE id = ?",
            (payment["lessons_paid"], payment["student_id"]),
        )

        last = conn.execute("""
            SELECT MAX(payment_date) AS d FROM payments
            WHERE student_id = ? AND is_cancelled = 0
        """, (payment["student_id"],)).fetchone()["d"]
        conn.execute(
            "UPDATE students SET last_payment_date = ? WHERE id = ?",
            (last, payment["student_id"]),
        )

        student = conn.execute(
            "SELECT name, remaining_lessons FROM students WHERE id = ?", (payment["student_id"],)
        ).fetchone()
        return {"student_name": student["name"], "balance": student["remaining_lessons"]}


def get_payments_by_student(student_id: int, limit: int = 50):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM payments
            WHERE student_id = ? AND is_cancelled = 0
            ORDER BY payment_date DESC, id DESC
            LIMIT ?
        """, (student_id, limit)).fetchall()


def suggest_packages(student_id: int) -> list[tuple[int, float]]:
    """Підказки (уроків, сума): остання оплата учня + найчастіші пакети в школі."""
    with get_connection() as conn:
        own = conn.execute("""
            SELECT lessons_paid, amount FROM payments
            WHERE student_id = ? AND is_cancelled = 0
            ORDER BY id DESC LIMIT 1
        """, (student_id,)).fetchall()
        popular = conn.execute("""
            SELECT lessons_paid, amount, COUNT(*) AS n FROM payments
            WHERE is_cancelled = 0
            GROUP BY lessons_paid, amount
            ORDER BY n DESC, MAX(id) DESC
            LIMIT 6
        """).fetchall()

    result: list[tuple[int, float]] = []
    for row in list(own) + list(popular):
        item = (row["lessons_paid"], row["amount"])
        if item not in result:
            result.append(item)
    return result[:4]


def get_low_balance_students(threshold: int = 1):
    """Активні учні із залишком <= threshold, разом із назвою групи."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT s.id, s.name, s.student_type, s.remaining_lessons,
                   g.id AS group_id, g.name AS group_name
            FROM students s
            LEFT JOIN groups g ON s.group_id = g.id
            WHERE s.is_active = 1 AND s.remaining_lessons <= ?
        """, (threshold,)).fetchall()
    return sorted(rows, key=lambda r: (r["remaining_lessons"], name_key(r["name"])))


def get_month_report(month: str):
    """month у форматі YYYY-MM."""
    with get_connection() as conn:
        totals = conn.execute("""
            SELECT COUNT(*) AS payments_count,
                   COALESCE(SUM(amount), 0) AS amount,
                   COALESCE(SUM(lessons_paid), 0) AS lessons_paid
            FROM payments
            WHERE is_cancelled = 0 AND substr(payment_date, 1, 7) = ?
        """, (month,)).fetchone()

        by_group = conn.execute("""
            SELECT COALESCE(g.name, 'Індивідуальні') AS title,
                   COUNT(*) AS payments_count,
                   SUM(p.amount) AS amount
            FROM payments p
            JOIN students s ON s.id = p.student_id
            LEFT JOIN groups g ON g.id = s.group_id
            WHERE p.is_cancelled = 0 AND substr(p.payment_date, 1, 7) = ?
            GROUP BY title
            ORDER BY amount DESC
        """, (month,)).fetchall()

        lessons = conn.execute("""
            SELECT lesson_type, COUNT(*) AS n
            FROM lessons
            WHERE status = 'completed' AND substr(lesson_date, 1, 7) = ?
            GROUP BY lesson_type
        """, (month,)).fetchall()

        attendance = conn.execute("""
            SELECT g.name AS title,
                   COUNT(DISTINCT l.id) AS lessons_count,
                   SUM(a.attendance_status = 'present') AS present,
                   COUNT(a.id) AS total
            FROM lessons l
            JOIN groups g ON g.id = l.group_id
            JOIN attendance a ON a.lesson_id = l.id
            WHERE l.status = 'completed' AND l.lesson_type = 'group'
              AND substr(l.lesson_date, 1, 7) = ?
            GROUP BY g.id
            ORDER BY g.name
        """, (month,)).fetchall()

        written_off = conn.execute("""
            SELECT COUNT(*) AS n
            FROM attendance a JOIN lessons l ON l.id = a.lesson_id
            WHERE l.status = 'completed' AND a.lesson_written_off = 1
              AND substr(l.lesson_date, 1, 7) = ?
        """, (month,)).fetchone()["n"]

    return {
        "totals": totals,
        "by_group": by_group,
        "lessons": {row["lesson_type"]: row["n"] for row in lessons},
        "attendance": attendance,
        "written_off": written_off,
    }
