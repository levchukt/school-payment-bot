from database.db import get_connection


def create_group(name: str, lesson_day: str = "", lesson_time: str = "", notes: str = "") -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO groups (name, lesson_day, lesson_time, notes) VALUES (?, ?, ?, ?)",
            (name, lesson_day, lesson_time, notes),
        )
        return cursor.lastrowid


def get_all_groups(include_archived: bool = False):
    """Групи з кількістю активних учнів (поле students_count)."""
    query = """
        SELECT g.*,
               (SELECT COUNT(*) FROM students s
                 WHERE s.group_id = g.id AND s.is_active = 1) AS students_count
        FROM groups g
    """
    if not include_archived:
        query += " WHERE g.is_active = 1"
    query += " ORDER BY g.id"

    with get_connection() as conn:
        return conn.execute(query).fetchall()


def get_archived_groups():
    with get_connection() as conn:
        return conn.execute("SELECT * FROM groups WHERE is_active = 0 ORDER BY id").fetchall()


def get_group_by_id(group_id: int):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def rename_group(group_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))


def update_group_schedule(group_id: int, lesson_day: str, lesson_time: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE groups SET lesson_day = ?, lesson_time = ? WHERE id = ?",
            (lesson_day, lesson_time, group_id),
        )


def set_group_active(group_id: int, is_active: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE groups SET is_active = ? WHERE id = ?", (int(is_active), group_id))
