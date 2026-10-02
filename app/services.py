"""领域服务层：权限、预约、改期、请假、视频、纪要、待办。

所有写方法在单个 SQLite 连接内提交；关键并发规则由数据库部分唯一索引兜底
（同一时段一份有效预约、一个学生一轮一份有效预约）。
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, date, timedelta

from .adapters import MockNotificationAdapter, MockVideoAdapter

ACTIVE_BOOKING_STATES = ("pending", "confirmed", "reschedule_requested", "teacher_reschedule")


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return date.today().isoformat()


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M")


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return _parse(start_a) < _parse(end_b) and _parse(start_b) < _parse(end_a)


class Service:
    def __init__(self, conn: sqlite3.Connection,
                 notifier: MockNotificationAdapter | None = None,
                 video: MockVideoAdapter | None = None):
        self.conn = conn
        self.notifier = notifier or MockNotificationAdapter(conn)
        self.video = video or MockVideoAdapter(conn)

    # ------------------------------------------------------------------ 基础

    def _log(self, user: sqlite3.Row | None, action: str, entity_type: str,
             entity_id, granted: bool, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO access_logs(user_id, username, action, entity_type, entity_id, granted, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (user["id"] if user else None, user["username"] if user else None,
             action, entity_type, str(entity_id) if entity_id is not None else None,
             1 if granted else 0, detail),
        )
        self.conn.commit()

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    # ------------------------------------------------------------------ 认证

    def login(self, username: str, password: str) -> tuple[str, sqlite3.Row]:
        user = self.user_by_name(username)
        if not user or user["password"] != password:
            self._log(user, "login", "user", username, False, "用户名或密码错误")
            raise ServiceError("用户名或密码错误", "auth_failed")
        token = secrets.token_hex(16)
        self.conn.execute(
            "INSERT INTO sessions(token, user_id) VALUES (?,?)", (token, user["id"])
        )
        self.conn.commit()
        self._log(user, "login", "user", user["id"], True)
        return token, user

    def logout(self, token: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        self.conn.commit()

    def current_user(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        return row

    # ------------------------------------------------------------------ 权限

    def teacher_id_of(self, user: sqlite3.Row) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM teachers WHERE user_id=?", (user["id"],)
        ).fetchone()
        return row["id"] if row else None

    def is_class_teacher(self, user: sqlite3.Row, class_id: int) -> bool:
        if user["role"] != "teacher":
            return False
        row = self.conn.execute(
            "SELECT 1 FROM class_teachers ct JOIN teachers t ON t.id=ct.teacher_id "
            "WHERE t.user_id=? AND ct.class_id=?",
            (user["id"], class_id),
        ).fetchone()
        return row is not None

    def classes_for_teacher(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.* FROM classes c JOIN class_teachers ct ON ct.class_id=c.id "
            "JOIN teachers t ON t.id=ct.teacher_id WHERE t.user_id=? ORDER BY c.id",
            (user["id"],),
        ).fetchall()

    def active_guardianship(self, user_id: int, student_id: int) -> sqlite3.Row | None:
        """未撤销的监护授权；撤销后返回 None。"""
        return self.conn.execute(
            "SELECT * FROM guardianships WHERE guardian_user_id=? AND student_id=? AND revoked=0",
            (user_id, student_id),
        ).fetchone()

    def is_guardian_of(self, user: sqlite3.Row, student_id: int) -> bool:
        if user["role"] != "guardian":
            return False
        return self.active_guardianship(user["id"], student_id) is not None

    def guardians_of(self, student_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT u.*, g.relation FROM guardianships g JOIN users u ON u.id=g.guardian_user_id "
            "WHERE g.student_id=? AND g.revoked=0 ORDER BY u.id",
            (student_id,),
        ).fetchall()

    def students_for_guardian(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, g.relation FROM students s "
            "JOIN guardianships g ON g.student_id=s.id "
            "WHERE g.guardian_user_id=? AND g.revoked=0 ORDER BY s.id",
            (user["id"],),
        ).fetchall()

    def homeroom_teacher_users(self, class_id: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT u.* FROM class_teachers ct JOIN teachers t ON t.id=ct.teacher_id "
            "JOIN users u ON u.id=t.user_id WHERE ct.class_id=? AND ct.is_homeroom=1",
            (class_id,),
        ).fetchall()
        if rows:
            return rows
        return self.conn.execute(
            "SELECT u.* FROM class_teachers ct JOIN teachers t ON t.id=ct.teacher_id "
            "JOIN users u ON u.id=t.user_id WHERE ct.class_id=?",
            (class_id,),
        ).fetchall()

    def require_class_teacher(self, user: sqlite3.Row, class_id: int) -> None:
        if not self.is_class_teacher(user, class_id):
            self._log(user, "denied", "class", class_id, False, "非本班任课教师")
            raise ServiceError("只有本班任课教师可以执行此操作", "forbidden")

    def require_guardian(self, user: sqlite3.Row, student_id: int) -> None:
        if self.active_guardianship(user["id"], student_id) is None:
            self._log(user, "denied", "student", student_id, False, "监护授权无效或已撤销")
            raise ServiceError("您不是该学生的有效监护人（授权可能已撤销）", "forbidden")

    # ------------------------------------------------------------------ 窗口/时段

    def publish_round(self, user: sqlite3.Row, class_id: int, title: str,
                      open_at: str, close_at: str) -> int:
        self.require_class_teacher(user, class_id)
        if _parse(close_at) <= _parse(open_at):
            raise ServiceError("窗口结束时间必须晚于开始时间", "invalid_time")
        cur = self.conn.execute(
            "INSERT INTO rounds(class_id, title, open_at, close_at, created_by)"
            " VALUES (?,?,?,?,?)",
            (class_id, title, open_at, close_at, user["id"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def create_slot(self, user: sqlite3.Row, round_id: int, teacher_id: int,
                    start_at: str, end_at: str, location: str = "本班教室") -> int:
        rnd = self.conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
        if not rnd:
            raise ServiceError("预约窗口不存在", "not_found")
        self.require_class_teacher(user, rnd["class_id"])
        t = self.conn.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        if not t:
            raise ServiceError("教师不存在", "not_found")
        if _parse(end_at) <= _parse(start_at):
            raise ServiceError("结束时间必须晚于开始时间", "invalid_time")
        clash = self.conn.execute(
            "SELECT * FROM time_slots WHERE teacher_id=? AND status!='cancelled' AND status!='teacher_leave'",
            (teacher_id,),
        ).fetchall()
        for s in clash:
            if overlaps(start_at, end_at, s["start_at"], s["end_at"]):
                raise ServiceError(
                    f"时段冲突：{t['name']} 在 {s['start_at']}~{s['end_at']} 已有安排",
                    "slot_conflict")
        cur = self.conn.execute(
            "INSERT INTO time_slots(round_id, class_id, teacher_id, start_at, end_at, location)"
            " VALUES (?,?,?,?,?,?)",
            (round_id, rnd["class_id"], teacher_id, start_at, end_at, location),
        )
        self.conn.commit()
        return cur.lastrowid

    def calendar(self, round_id: int) -> list[sqlite3.Row]:
        """教师端日历：按教师与时间排列。"""
        return self.conn.execute(
            "SELECT ts.*, t.name AS teacher_name, t.subject, "
            "  b.id AS booking_id, b.status AS booking_status, b.mode, b.topics,"
            "  s.name AS student_name, u.display_name AS guardian_name"
            " FROM time_slots ts"
            " JOIN teachers t ON t.id=ts.teacher_id"
            " LEFT JOIN bookings b ON b.slot_id=ts.id AND b.status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "')"
            " LEFT JOIN students s ON s.id=b.student_id"
            " LEFT JOIN users u ON u.id=b.guardian_user_id"
            " WHERE ts.round_id=? ORDER BY t.id, ts.start_at",
            (round_id,),
        ).fetchall()

    def open_slots(self, round_id: int) -> list[sqlite3.Row]:
        """家长端：只展示可选时段（按教师、时间排列）。"""
        return self.conn.execute(
            "SELECT ts.*, t.name AS teacher_name, t.subject FROM time_slots ts"
            " JOIN teachers t ON t.id=ts.teacher_id"
            " WHERE ts.round_id=? AND ts.status='open' ORDER BY t.id, ts.start_at",
            (round_id,),
        ).fetchall()

    def get_round(self, round_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()

    def rounds_for_class(self, class_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rounds WHERE class_id=? ORDER BY id DESC", (class_id,)
        ).fetchall()

    def latest_round(self, class_id: int) -> sqlite3.Row | None:
        rows = self.rounds_for_class(class_id)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ 预约

    def book_slot(self, user: sqlite3.Row, slot_id: int, student_id: int,
                  mode: str, topics: str, now: str | None = None) -> int:
        now = now or _now()
        if mode not in ("offline", "video"):
            raise ServiceError("会谈方式必须是线下或视频", "invalid_mode")
        self.require_guardian(user, student_id)
        slot = self.conn.execute(
            "SELECT * FROM time_slots WHERE id=?", (slot_id,)
        ).fetchone()
        if not slot:
            raise ServiceError("时段不存在", "not_found")
        rnd = self.get_round(slot["round_id"])
        student = self.conn.execute(
            "SELECT * FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not student or student["class_id"] != rnd["class_id"]:
            self._log(user, "book", "slot", slot_id, False, "学生不属于该班级")
            raise ServiceError("只能为本班学生预约", "forbidden")
        if not (rnd["open_at"] <= now <= rnd["close_at"]):
            raise ServiceError("当前不在预约窗口开放时间内", "window_closed")
        if slot["status"] != "open":
            raise ServiceError("该时段已不可约", "slot_unavailable")

        # 应用层预检查（数据库部分唯一索引同时兜底并发抢占）
        holder = self.conn.execute(
            "SELECT b.*, s.name AS sname FROM bookings b JOIN students s ON s.id=b.student_id"
            " WHERE b.slot_id=? AND b.status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "')",
            (slot_id,),
        ).fetchone()
        if holder is not None:
            raise ServiceError(f"该时段已被 {holder['sname']} 的监护人抢走，请换一个时段",
                               "slot_taken")
        dup = self.conn.execute(
            "SELECT 1 FROM bookings WHERE student_id=? AND round_id=? AND status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "')",
            (student_id, slot["round_id"]),
        ).fetchone()
        if dup is not None:
            raise ServiceError("一个学生同一轮家长会只能保留一份有效预约",
                               "duplicate_booking")

        try:
            cur = self.conn.execute(
                "INSERT INTO bookings(slot_id, round_id, student_id, guardian_user_id, mode, topics)"
                " VALUES (?,?,?,?,?,?)",
                (slot_id, slot["round_id"], student_id, user["id"], mode, topics.strip()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            msg = str(e)
            if "ux_booking_active_slot" in msg:
                raise ServiceError("该时段刚被其他监护人抢走，请换一个时段", "slot_taken")
            if "ux_booking_student_round" in msg:
                raise ServiceError("一个学生同一轮家长会只能保留一份有效预约", "duplicate_booking")
            raise ServiceError("预约失败", "db_error")

        booking_id = cur.lastrowid
        self._log(user, "book", "slot", slot_id, True,
                  f"booking={booking_id} student={student_id} mode={mode}")
        for tch in self.homeroom_teacher_users(rnd["class_id"]):
            self.notifier.send(tch["id"], "booking_new", "收到新的家长会预约",
                               f"{student['name']} 的监护人申请 {slot['start_at']} 时段")
        return booking_id

    def get_booking(self, booking_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT b.*, s.name AS student_name, s.class_id FROM bookings b "
            "JOIN students s ON s.id=b.student_id WHERE b.id=?",
            (booking_id,),
        ).fetchone()

    def active_booking_for_student(self, student_id: int, round_id: int) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_BOOKING_STATES)
        return self.conn.execute(
            f"SELECT * FROM bookings WHERE student_id=? AND round_id=? AND status IN ({placeholders})",
            (student_id, round_id, *ACTIVE_BOOKING_STATES),
        ).fetchone()

    def bookings_for_guardian(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT b.*, s.name AS student_name, ts.start_at, ts.end_at, ts.location,"
            " t.name AS teacher_name, c.name AS class_name"
            " FROM bookings b JOIN students s ON s.id=b.student_id"
            " JOIN time_slots ts ON ts.id=b.slot_id"
            " JOIN teachers t ON t.id=ts.teacher_id"
            " JOIN classes c ON c.id=s.class_id"
            " WHERE b.guardian_user_id=? ORDER BY ts.start_at DESC",
            (user["id"],),
        ).fetchall()

    def confirm_booking(self, user: sqlite3.Row, booking_id: int) -> None:
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        self.require_class_teacher(user, b["class_id"])
        if b["status"] != "pending":
            raise ServiceError(f"当前状态（{b['status']}）不能确认", "bad_state")
        self.conn.execute(
            "UPDATE bookings SET status='confirmed', decided_at=? WHERE id=?",
            (_now(), booking_id),
        )
        self.conn.execute("UPDATE time_slots SET status='held' WHERE id=?", (b["slot_id"],))
        self.conn.commit()
        if b["mode"] == "video":
            self.video.create_entry(booking_id)
        slot = self.conn.execute(
            "SELECT * FROM time_slots WHERE id=?", (b["slot_id"],)
        ).fetchone()
        self.notifier.send(b["guardian_user_id"], "booking_confirmed", "预约已确认",
                           f"时间：{slot['start_at']}~{slot['end_at']}，方式：{'视频' if b['mode']=='video' else '线下'}")
        self._log(user, "confirm", "booking", booking_id, True)

    def cancel_booking(self, user: sqlite3.Row, booking_id: int, reason: str = "") -> None:
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        is_teacher = user["role"] == "teacher" and self.is_class_teacher(user, b["class_id"])
        is_owner_guardian = (user["id"] == b["guardian_user_id"]
                             and self.active_guardianship(user["id"], b["student_id"]) is not None)
        if not (is_teacher or is_owner_guardian):
            self._log(user, "denied", "booking", booking_id, False, "取消预约")
            raise ServiceError("无权取消该预约", "forbidden")
        if b["status"] in ("completed", "cancelled"):
            raise ServiceError("该预约已结束，不能取消", "bad_state")
        self._deactivate_booking(booking_id, b["slot_id"], cancelled_by=user, reason=reason)

    def _deactivate_booking(self, booking_id: int, old_slot_id: int,
                            cancelled_by: sqlite3.Row | None = None, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND status!='completed'",
            (booking_id,),
        )
        self.conn.execute(
            "UPDATE reschedule_queue SET status='cancelled', decided_at=? "
            "WHERE booking_id=? AND status='pending'",
            (_now(), booking_id),
        )
        # 只有当该时段上没有其他有效预约时才释放
        other = self.conn.execute(
            "SELECT 1 FROM bookings WHERE slot_id=? AND status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "') AND id!=?",
            (old_slot_id, booking_id),
        ).fetchone()
        slot = self.conn.execute(
            "SELECT status FROM time_slots WHERE id=?", (old_slot_id,)
        ).fetchone()
        if not other and slot and slot["status"] == "held":
            self.conn.execute("UPDATE time_slots SET status='open' WHERE id=?", (old_slot_id,))
        self.conn.commit()
        if cancelled_by is not None:
            self._log(cancelled_by, "cancel", "booking", booking_id, True, reason)

    # ------------------------------------------------------------------ 改期

    def guardian_request_reschedule(self, user: sqlite3.Row, booking_id: int,
                                    target_slot_id: int | None, reason: str = "") -> int:
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        self.require_guardian(user, b["student_id"])
        if user["id"] != b["guardian_user_id"]:
            raise ServiceError("只有预约发起监护人可以申请改期", "forbidden")
        if b["status"] not in ("confirmed", "pending"):
            raise ServiceError(f"当前状态（{b['status']}）不能申请改期", "bad_state")
        if target_slot_id is not None:
            self._validate_target_slot(target_slot_id, b["round_id"], b["class_id"], b["student_id"])
        cur = self.conn.execute(
            "INSERT INTO reschedule_queue(booking_id, source, requested_by, reason, target_slot_id)"
            " VALUES (?, 'guardian', ?, ?, ?)",
            (booking_id, user["id"], reason.strip(), target_slot_id),
        )
        self.conn.execute(
            "UPDATE bookings SET status='reschedule_requested' WHERE id=?", (booking_id,)
        )
        self.conn.commit()
        for tch in self.homeroom_teacher_users(b["class_id"]):
            self.notifier.send(tch["id"], "reschedule_request", "监护人申请改期",
                               f"{b['student_name']}：{reason or '（未填写原因）'}")
        self._log(user, "reschedule_request", "booking", booking_id, True, reason)
        return cur.lastrowid

    def _validate_target_slot(self, slot_id: int, round_id: int, class_id: int,
                              student_id: int) -> sqlite3.Row:
        target = self.conn.execute(
            "SELECT * FROM time_slots WHERE id=?", (slot_id,)
        ).fetchone()
        if not target:
            raise ServiceError("目标时段不存在", "not_found")
        if target["round_id"] != round_id or target["class_id"] != class_id:
            raise ServiceError("目标时段不在同一轮家长会中", "bad_slot")
        holder = self.conn.execute(
            "SELECT b.*, s.name FROM bookings b JOIN students s ON s.id=b.student_id"
            " WHERE b.slot_id=? AND b.status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "')",
            (slot_id,),
        ).fetchone()
        if holder is not None and holder["student_id"] != student_id:
            raise ServiceError(f"目标时段已被 {holder['name']} 的监护人预约", "slot_taken")
        if target["status"] != "open":
            raise ServiceError("目标时段已不可约", "slot_unavailable")
        return target

    def reschedule_queue(self, class_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT q.*, b.student_id, s.name AS student_name, s.class_id,"
               " b.mode, ts1.start_at AS old_start, ts2.start_at AS target_start,"
               " u.display_name AS requester_name"
               " FROM reschedule_queue q"
               " JOIN bookings b ON b.id=q.booking_id"
               " JOIN students s ON s.id=b.student_id"
               " JOIN time_slots ts1 ON ts1.id=b.slot_id"
               " LEFT JOIN time_slots ts2 ON ts2.id=q.target_slot_id"
               " LEFT JOIN users u ON u.id=q.requested_by")
        args: list = []
        if class_id is not None:
            sql += " WHERE s.class_id=?"
            args.append(class_id)
        sql += " ORDER BY CASE q.status WHEN 'pending' THEN 0 ELSE 1 END, q.created_at"
        return self.conn.execute(sql, args).fetchall()

    def guardian_reschedule_queue(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.*, s.name AS student_name, ts1.start_at AS old_start,"
            " ts2.start_at AS target_start"
            " FROM reschedule_queue q JOIN bookings b ON b.id=q.booking_id"
            " JOIN students s ON s.id=b.student_id"
            " JOIN time_slots ts1 ON ts1.id=b.slot_id"
            " LEFT JOIN time_slots ts2 ON ts2.id=q.target_slot_id"
            " WHERE b.guardian_user_id=? ORDER BY q.created_at DESC",
            (user["id"],),
        ).fetchall()

    def approve_reschedule(self, user: sqlite3.Row, queue_id: int,
                           target_slot_id: int | None = None) -> None:
        q = self.conn.execute("SELECT * FROM reschedule_queue WHERE id=?", (queue_id,)).fetchone()
        if not q:
            raise ServiceError("改期申请不存在", "not_found")
        b = self.get_booking(q["booking_id"])
        self.require_class_teacher(user, b["class_id"])
        if q["status"] != "pending":
            raise ServiceError("该改期申请已处理", "bad_state")
        target_id = target_slot_id or q["target_slot_id"]
        if not target_id:
            raise ServiceError("请指定新时段", "need_target")
        target = self._validate_target_slot(target_id, b["round_id"], b["class_id"], b["student_id"])
        self._move_booking(b, target, approver=user)
        self.conn.execute(
            "UPDATE reschedule_queue SET status='approved', target_slot_id=?, decided_at=? WHERE id=?",
            (target_id, _now(), queue_id),
        )
        self.conn.commit()
        self.notifier.send(b["guardian_user_id"], "reschedule_approved", "改期已通过",
                           f"新时间：{target['start_at']}~{target['end_at']}")

    def reject_reschedule(self, user: sqlite3.Row, queue_id: int, note: str = "") -> None:
        q = self.conn.execute("SELECT * FROM reschedule_queue WHERE id=?", (queue_id,)).fetchone()
        if not q:
            raise ServiceError("改期申请不存在", "not_found")
        b = self.get_booking(q["booking_id"])
        self.require_class_teacher(user, b["class_id"])
        if q["status"] != "pending":
            raise ServiceError("该改期申请已处理", "bad_state")
        new_status = "confirmed" if q["source"] == "guardian" else "teacher_reschedule"
        self.conn.execute(
            "UPDATE bookings SET status=? WHERE id=?", (new_status, b["id"])
        )
        self.conn.execute(
            "UPDATE reschedule_queue SET status='rejected', note=?, decided_at=? WHERE id=?",
            (note, _now(), queue_id),
        )
        self.conn.commit()
        self.notifier.send(b["guardian_user_id"], "reschedule_rejected", "改期未通过",
                           note or "教师暂无法安排，请重新选择时段")

    def guardian_pick_new_slot(self, user: sqlite3.Row, queue_id: int,
                               target_slot_id: int) -> None:
        """教师请假后，监护人在可选时段中自助重选，系统自动通过。"""
        q = self.conn.execute("SELECT * FROM reschedule_queue WHERE id=?", (queue_id,)).fetchone()
        if not q:
            raise ServiceError("改期队列项不存在", "not_found")
        b = self.get_booking(q["booking_id"])
        self.require_guardian(user, b["student_id"])
        if user["id"] != b["guardian_user_id"]:
            raise ServiceError("只有预约发起监护人可以重选时段", "forbidden")
        if q["source"] != "teacher_leave" or q["status"] != "pending":
            raise ServiceError("该队列项不能自助重选", "bad_state")
        if b["status"] != "teacher_reschedule":
            raise ServiceError("预约状态异常", "bad_state")
        target = self._validate_target_slot(target_slot_id, b["round_id"], b["class_id"], b["student_id"])
        self._move_booking(b, target, approver=user, new_status="confirmed")
        self.conn.execute(
            "UPDATE reschedule_queue SET status='approved', target_slot_id=?,"
            " note='监护人自助重选', decided_at=? WHERE id=?",
            (target_slot_id, _now(), queue_id),
        )
        self.conn.commit()
        self.notifier.send(b["guardian_user_id"], "reschedule_approved", "已改期成功",
                           f"新时间：{target['start_at']}~{target['end_at']}")

    def _move_booking(self, b: sqlite3.Row, target: sqlite3.Row,
                      approver: sqlite3.Row, new_status: str = "confirmed") -> None:
        old_slot_id = b["slot_id"]
        was_video = b["mode"] == "video"
        self.conn.execute(
            "UPDATE bookings SET slot_id=?, status=? WHERE id=?",
            (target["id"], new_status, b["id"]),
        )
        self.conn.execute("UPDATE time_slots SET status='held' WHERE id=?", (target["id"],))
        slot = self.conn.execute(
            "SELECT status FROM time_slots WHERE id=?", (old_slot_id,)
        ).fetchone()
        if slot and slot["status"] == "held":
            self.conn.execute("UPDATE time_slots SET status='open' WHERE id=?", (old_slot_id,))
        # 视频预约改期后重新生成会议入口
        if was_video:
            self.video.rotate_entry(b["id"])
        self._log(approver, "reschedule_move", "booking", b["id"], True,
                  f"{old_slot_id}->{target['id']}")

    # ------------------------------------------------------------------ 教师请假

    def report_teacher_leave(self, user: sqlite3.Row, teacher_id: int,
                             start_at: str, end_at: str, reason: str = "") -> dict:
        """登记请假：受影响的有效预约进入改期队列（source=teacher_leave）。"""
        t = self.conn.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        if not t:
            raise ServiceError("教师不存在", "not_found")
        # 班主任本人可登记自己；管理员可登记任意教师
        if user["role"] == "teacher" and self.teacher_id_of(user) != teacher_id:
            if not self.classes_for_teacher(user):
                raise ServiceError("只能为自己登记请假", "forbidden")
        if _parse(end_at) <= _parse(start_at):
            raise ServiceError("结束时间必须晚于开始时间", "invalid_time")

        self.conn.execute(
            "INSERT INTO teacher_leave(teacher_id, start_at, end_at, reason) VALUES (?,?,?,?)",
            (teacher_id, start_at, end_at, reason.strip()),
        )
        affected = self.conn.execute(
            "SELECT ts.*, b.id AS booking_id FROM time_slots ts"
            " JOIN bookings b ON b.slot_id=ts.id AND b.status IN ('"
            + "','".join(ACTIVE_BOOKING_STATES) + "')"
            " WHERE ts.teacher_id=? AND ts.status!='cancelled'",
            (teacher_id,),
        ).fetchall()
        queued, cancelled_slots = [], []
        for s in affected:
            if not overlaps(start_at, end_at, s["start_at"], s["end_at"]):
                continue
            self.conn.execute(
                "UPDATE bookings SET status='teacher_reschedule' WHERE id=?",
                (s["booking_id"],),
            )
            self.conn.execute(
                "UPDATE time_slots SET status='teacher_leave' WHERE id=?", (s["id"],)
            )
            cur = self.conn.execute(
                "INSERT INTO reschedule_queue(booking_id, source, reason) VALUES (?, 'teacher_leave', ?)",
                (s["booking_id"], reason.strip() or "教师请假"),
            )
            queued.append(cur.lastrowid)
            bk = self.get_booking(s["booking_id"])
            self.notifier.send(
                bk["guardian_user_id"], "teacher_leave", "教师请假，需要改期",
                f"{t['name']} 于 {s['start_at']} 的时段因请假取消，请在可选时段中重新选择")

        empty_slots = self.conn.execute(
            "SELECT * FROM time_slots ts WHERE ts.teacher_id=? AND ts.status='open'",
            (teacher_id,),
        ).fetchall()
        for s in empty_slots:
            if overlaps(start_at, end_at, s["start_at"], s["end_at"]):
                self.conn.execute(
                    "UPDATE time_slots SET status='cancelled' WHERE id=?", (s["id"],)
                )
                cancelled_slots.append(s["id"])
        self.conn.commit()
        self._log(user, "teacher_leave", "teacher", teacher_id, True,
                  f"queue={queued} cancelled_slots={cancelled_slots}")
        return {"teacher": t["name"], "queued_bookings": queued,
                "cancelled_slots": cancelled_slots}

    # ------------------------------------------------------------------ 视频

    def join_video(self, user: sqlite3.Row, booking_id: int, now: str | None = None) -> dict:
        now = now or _now()
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        allowed = (self.is_class_teacher(user, b["class_id"])
                   or (user["id"] == b["guardian_user_id"]
                       and self.active_guardianship(user["id"], b["student_id"]) is not None))
        if not allowed:
            self._log(user, "video_denied", "booking", booking_id, False)
            raise ServiceError("无权进入该视频会议", "forbidden")
        if b["status"] not in ("confirmed", "completed"):
            raise ServiceError("预约未确认，暂无视频入口", "bad_state")
        entry = self.video.active_entry(booking_id)
        if entry is None:
            self._log(user, "video_expired", "booking", booking_id, False, "链接失效")
            raise ServiceError("视频链接已失效，请联系教师重新生成", "link_expired")
        entry = self.video.join(entry["id"], now)
        self._log(user, "video_join", "meeting", entry["id"], True)
        return dict(entry)

    def rotate_video(self, user: sqlite3.Row, booking_id: int) -> dict:
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        self.require_class_teacher(user, b["class_id"])
        entry = self.video.rotate_entry(booking_id)
        self.notifier.send(b["guardian_user_id"], "video_new_link", "视频会议链接已更新",
                           f"新链接：{entry['join_url']}")
        self._log(user, "video_rotate", "booking", booking_id, True)
        return dict(entry)

    # ------------------------------------------------------------------ 完成/纪要

    def complete_booking(self, user: sqlite3.Row, booking_id: int,
                         now: str | None = None) -> int:
        """结束会谈：教师锁定时段。返回超时分钟数。"""
        now = now or _now()
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        self.require_class_teacher(user, b["class_id"])
        if b["status"] not in ("confirmed", "reschedule_requested"):
            raise ServiceError(f"当前状态（{b['status']}）不能结束会谈", "bad_state")
        slot = self.conn.execute(
            "SELECT * FROM time_slots WHERE id=?", (b["slot_id"],)
        ).fetchone()
        overtime = max(0, int((_parse(now) - _parse(slot["end_at"])).total_seconds() // 60))
        self.conn.execute(
            "UPDATE bookings SET status='completed', decided_at=? WHERE id=?",
            (now, booking_id),
        )
        self.conn.execute("UPDATE time_slots SET status='completed' WHERE id=?", (b["slot_id"],))
        self.conn.commit()
        self._log(user, "complete", "booking", booking_id, True, f"overtime={overtime}")
        return overtime

    def save_minutes(self, user: sqlite3.Row, booking_id: int, conclusions: str,
                     follow_ups: list[dict], overtime_minutes: int | None = None,
                     finalize: bool = False) -> int:
        """记录/修改纪要与后续待办。

        只有本班任课教师（role 必须为 teacher）可以写入——
        教师不能以普通家长身份修改会谈结论。finalized 后锁定。
        """
        b = self.get_booking(booking_id)
        if not b:
            raise ServiceError("预约不存在", "not_found")
        if user["role"] != "teacher":
            self._log(user, "minutes_denied", "booking", booking_id, False,
                      f"role={user['role']} 试图写入纪要")
            raise ServiceError("只有教师可以记录会谈纪要", "forbidden")
        self.require_class_teacher(user, b["class_id"])

        minute = self.conn.execute(
            "SELECT * FROM minutes WHERE booking_id=?", (booking_id,)
        ).fetchone()
        if minute and minute["finalized"]:
            raise ServiceError("纪要已锁定，不能再修改", "locked")

        for f in follow_ups:
            if not f.get("content") or not f.get("owner_label") or not f.get("due_date"):
                raise ServiceError("每条后续事项必须写明内容、负责人和期限", "invalid_followup")
            datetime.strptime(f["due_date"], "%Y-%m-%d")

        if minute:
            minute_id = minute["id"]
            self.conn.execute(
                "UPDATE minutes SET conclusions=?, overtime_minutes=COALESCE(?, overtime_minutes),"
                " finalized=?, created_by=?, updated_at=? WHERE id=?",
                (conclusions.strip(), overtime_minutes, 1 if finalize else 0,
                 user["id"], _now(), minute_id),
            )
            self.conn.execute("DELETE FROM follow_ups WHERE minute_id=?", (minute_id,))
        else:
            if b["status"] not in ("completed", "confirmed", "reschedule_requested"):
                raise ServiceError("会谈尚未开始，不能记录纪要", "bad_state")
            cur = self.conn.execute(
                "INSERT INTO minutes(booking_id, student_id, class_id, conclusions,"
                " overtime_minutes, finalized, created_by)"
                " VALUES (?,?,?,?,?,?,?)",
                (booking_id, b["student_id"], b["class_id"], conclusions.strip(),
                 overtime_minutes or 0, 1 if finalize else 0, user["id"]),
            )
            minute_id = cur.lastrowid
        for f in follow_ups:
            owner_uid = f.get("owner_user_id")
            self.conn.execute(
                "INSERT INTO follow_ups(minute_id, content, owner_label, owner_user_id, due_date)"
                " VALUES (?,?,?,?,?)",
                (minute_id, f["content"].strip(), f["owner_label"].strip(),
                 owner_uid if owner_uid else None, f["due_date"]),
            )
        self.conn.commit()
        self._log(user, "minutes_write", "minute", minute_id, True,
                  f"finalized={finalize} follow_ups={len(follow_ups)}")
        for g in self.guardians_of(b["student_id"]):
            self.notifier.send(g["id"], "minutes_ready", "会谈纪要已更新",
                               "请登录家长端查看纪要与后续待办")
        return minute_id

    def finalize_minutes(self, user: sqlite3.Row, minute_id: int) -> None:
        m = self.get_minute_record(minute_id)
        self.require_class_teacher(user, m["class_id"])
        if user["role"] != "teacher":
            raise ServiceError("只有教师可以锁定纪要", "forbidden")
        self.conn.execute(
            "UPDATE minutes SET finalized=1, updated_at=? WHERE id=?", (_now(), minute_id)
        )
        self.conn.commit()

    def get_minute_record(self, minute_id: int) -> sqlite3.Row:
        m = self.conn.execute("SELECT * FROM minutes WHERE id=?", (minute_id,)).fetchone()
        if not m:
            raise ServiceError("纪要不存在", "not_found")
        return m

    def get_minute_by_booking(self, booking_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM minutes WHERE booking_id=?", (booking_id,)
        ).fetchone()

    def view_minute(self, user: sqlite3.Row, minute_id: int) -> sqlite3.Row:
        """读取单份纪要，访问被拒时写 access_logs 并抛错。"""
        m = self.get_minute_record(minute_id)
        if self.is_class_teacher(user, m["class_id"]):
            self._log(user, "minutes_view", "minute", minute_id, True, "teacher")
            return m
        g = self.active_guardianship(user["id"], m["student_id"])
        if user["role"] == "guardian" and g is not None:
            self._log(user, "minutes_view", "minute", minute_id, True,
                      f"guardian relation={g['relation']}")
            return m
        self._log(user, "minutes_denied", "minute", minute_id, False,
                  f"role={user['role']}")
        raise ServiceError("纪要只对本班任课教师和监护人开放", "forbidden")

    def follow_ups_for_minute(self, minute_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM follow_ups WHERE minute_id=? ORDER BY due_date, id", (minute_id,)
        ).fetchall()

    def minutes_visible_to(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        """家长端只展示自己的纪要；教师端展示本班纪要。"""
        if user["role"] == "teacher":
            return self.conn.execute(
                "SELECT m.*, s.name AS student_name, c.name AS class_name,"
                " b.mode, ts.start_at, t.name AS teacher_name"
                " FROM minutes m JOIN students s ON s.id=m.student_id"
                " JOIN classes c ON c.id=m.class_id"
                " JOIN bookings b ON b.id=m.booking_id"
                " JOIN time_slots ts ON ts.id=b.slot_id"
                " JOIN teachers t ON t.id=ts.teacher_id"
                " WHERE EXISTS (SELECT 1 FROM class_teachers ct JOIN teachers t2 ON t2.id=ct.teacher_id"
                " WHERE t2.user_id=? AND ct.class_id=m.class_id)"
                " ORDER BY ts.start_at DESC",
                (user["id"],),
            ).fetchall()
        # guardian：只取“当前仍有效”的监护关系——撤销后旧账号看不到任何纪要
        rows = self.conn.execute(
            "SELECT m.*, s.name AS student_name, c.name AS class_name,"
            " b.mode, ts.start_at, t.name AS teacher_name, g.relation"
            " FROM minutes m JOIN students s ON s.id=m.student_id"
            " JOIN classes c ON c.id=m.class_id"
            " JOIN bookings b ON b.id=m.booking_id"
            " JOIN time_slots ts ON ts.id=b.slot_id"
            " JOIN teachers t ON t.id=ts.teacher_id"
            " JOIN guardianships g ON g.student_id=m.student_id AND g.revoked=0"
            " WHERE g.guardian_user_id=? ORDER BY ts.start_at DESC",
            (user["id"],),
        ).fetchall()
        self._log(user, "minutes_list", "minute", "-", True, f"count={len(rows)}")
        return rows

    def my_follow_ups(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT f.*, m.student_id, s.name AS student_name FROM follow_ups f"
            " JOIN minutes m ON m.id=f.minute_id JOIN students s ON s.id=m.student_id"
            " WHERE f.owner_user_id=? ORDER BY f.status, f.due_date",
            (user["id"],),
        ).fetchall()

    def complete_follow_up(self, user: sqlite3.Row, follow_up_id: int) -> None:
        f = self.conn.execute("SELECT * FROM follow_ups WHERE id=?", (follow_up_id,)).fetchone()
        if not f:
            raise ServiceError("待办不存在", "not_found")
        m = self.get_minute_record(f["minute_id"])
        if not (self.is_class_teacher(user, m["class_id"]) or f["owner_user_id"] == user["id"]):
            self._log(user, "followup_denied", "follow_up", follow_up_id, False)
            raise ServiceError("只有负责人或本班教师可以完成该待办", "forbidden")
        self.conn.execute(
            "UPDATE follow_ups SET status='done', completed_at=? WHERE id=? AND status='open'",
            (_today(), follow_up_id),
        )
        self.conn.commit()

    def overdue_follow_ups(self, today: str | None = None) -> list[sqlite3.Row]:
        """后续事项未完成且已过期。"""
        today = today or _today()
        return self.conn.execute(
            "SELECT f.*, s.name AS student_name FROM follow_ups f"
            " JOIN minutes m ON m.id=f.minute_id JOIN students s ON s.id=m.student_id"
            " WHERE f.status='open' AND f.due_date < ? ORDER BY f.due_date",
            (today,),
        ).fetchall()

    def notify_overdue_follow_ups(self, today: str | None = None) -> int:
        count = 0
        for f in self.overdue_follow_ups(today):
            if f["owner_user_id"]:
                self.notifier.send(
                    f["owner_user_id"], "followup_overdue", "后续事项已逾期",
                    f"{f['student_name']}：{f['content']}（期限 {f['due_date']}）")
                count += 1
        return count

    # ------------------------------------------------------------------ 授权撤销

    def revoke_guardianship(self, actor: sqlite3.Row, student_id: int,
                            guardian_user_id: int) -> None:
        g = self.conn.execute(
            "SELECT * FROM guardianships WHERE student_id=? AND guardian_user_id=?",
            (student_id, guardian_user_id),
        ).fetchone()
        if not g:
            raise ServiceError("监护授权不存在", "not_found")
        student = self.conn.execute(
            "SELECT * FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if actor["role"] != "admin" and not self.is_class_teacher(actor, student["class_id"]):
            raise ServiceError("只有班主任/管理员可以撤销监护授权", "forbidden")
        if g["revoked"]:
            return
        self.conn.execute(
            "UPDATE guardianships SET revoked=1, revoked_at=? WHERE id=?",
            (_now(), g["id"]),
        )
        self.conn.commit()
        self._log(actor, "guardianship_revoke", "student", student_id, True,
                  f"guardian_user={guardian_user_id}")

    # ------------------------------------------------------------------ 附件

    def register_attachment(self, user: sqlite3.Row, student_id: int, filename: str,
                            local_path: str, minute_id: int | None = None) -> int:
        if not (user["role"] == "admin" or self.is_class_teacher(user,
                self.conn.execute("SELECT class_id FROM students WHERE id=?",
                                  (student_id,)).fetchone()["class_id"])):
            raise ServiceError("只有本班教师可以上传学生资料", "forbidden")
        cur = self.conn.execute(
            "INSERT INTO attachments(student_id, minute_id, filename, local_path, uploaded_by)"
            " VALUES (?,?,?,?,?)",
            (student_id, minute_id, filename, local_path, user["id"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def view_attachment(self, user: sqlite3.Row, attachment_id: int) -> sqlite3.Row:
        a = self.conn.execute(
            "SELECT * FROM attachments WHERE id=?", (attachment_id,)
        ).fetchone()
        if not a:
            raise ServiceError("附件不存在", "not_found")
        student = self.conn.execute(
            "SELECT * FROM students WHERE id=?", (a["student_id"],)
        ).fetchone()
        if self.is_class_teacher(user, student["class_id"]):
            self._log(user, "attachment_view", "attachment", attachment_id, True, "teacher")
            return a
        if self.active_guardianship(user["id"], a["student_id"]) is not None:
            self._log(user, "attachment_view", "attachment", attachment_id, True, "guardian")
            return a
        self._log(user, "attachment_denied", "attachment", attachment_id, False)
        raise ServiceError("学生资料只对本班任课教师和监护人开放", "forbidden")

    def attachments_for_student(self, user: sqlite3.Row, student_id: int) -> list[sqlite3.Row]:
        student = self.conn.execute(
            "SELECT class_id FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not (self.is_class_teacher(user, student["class_id"])
                or self.active_guardianship(user["id"], student_id) is not None):
            self._log(user, "attachment_list_denied", "student", student_id, False)
            raise ServiceError("无权查看该学生资料", "forbidden")
        return self.conn.execute(
            "SELECT * FROM attachments WHERE student_id=? ORDER BY id", (student_id,)
        ).fetchall()

    # ------------------------------------------------------------------ 通知

    def notifications_for(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (user["id"],),
        ).fetchall()

    def mark_notifications_read(self, user: sqlite3.Row) -> None:
        self.conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
        self.conn.commit()
