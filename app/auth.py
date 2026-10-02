"""身份与权限：任课教师、在权监护人的判定，以及访问日志。

权限模型：
* 涉及某学生的资料（预约详情、纪要、附件）只对
  - 本班任课教师（teachers 表中绑定该班的教师），以及
  - 该学生当前在权（guardianships.active=1）的监护人开放。
* 监护人授权撤销后，旧账号立即不可见。
* 纪要的写入/修改只有本班任课教师可以；即使该教师本人同时拥有
  家长身份，系统也按“本次操作的资源归属班级”判定，不允许以普通
  家长身份修改会谈结论。
"""
import sqlite3

from .errors import PermissionError_

TEACHER = "teacher"
GUARDIAN = "guardian"


def get_user(conn: sqlite3.Connection, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_login(conn: sqlite3.Connection, login: str):
    return conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()


def roles(user) -> set[str]:
    return set((user["roles"] or "").split(",")) if user else set()


def teaches(conn: sqlite3.Connection, user_id: int, class_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM teachers WHERE user_id=? AND class_id=?",
        (user_id, class_id),
    ).fetchone()
    return row is not None


def class_teacher_ids(conn: sqlite3.Connection, class_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM teachers WHERE class_id=?", (class_id,))]


def guardianship_active(conn: sqlite3.Connection, user_id: int,
                        student_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM guardianships WHERE user_id=? AND student_id=? AND active=1",
        (user_id, student_id),
    ).fetchone()
    return row is not None


def active_guardian_ids(conn: sqlite3.Connection, student_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM guardianships WHERE student_id=? AND active=1",
        (student_id,))]


def student_class(conn: sqlite3.Connection, student_id: int):
    return conn.execute(
        "SELECT * FROM students WHERE id=?", (student_id,)).fetchone()


def can_access_student(conn: sqlite3.Connection, user_id: int,
                       student_id: int) -> bool:
    stu = student_class(conn, student_id)
    if stu is None:
        return False
    return (teaches(conn, user_id, stu["class_id"])
            or guardianship_active(conn, user_id, student_id))


def log_access(conn, user_id, action, ok, now, minutes_id=None):
    conn.execute(
        "INSERT INTO access_logs(user_id, minutes_id, action, ok, at) "
        "VALUES (?,?,?,?,?)",
        (user_id, minutes_id, action, 1 if ok else 0, now),
    )


def require_teacher_of_class(conn, user, class_id):
    if user is None or TEACHER not in roles(user) \
            or not teaches(conn, user["id"], class_id):
        raise PermissionError_("只有本班任课教师可以执行此操作")


def require_guardian_of_student(conn, user, student_id):
    if user is None or not guardianship_active(conn, user["id"], student_id):
        raise PermissionError_("您不是该学生的在权监护人")
