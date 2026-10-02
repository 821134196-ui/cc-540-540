"""领域服务：时段、预约、改期、教师请假、视频、纪要与后续待办。

所有方法在需要保证一致性的地方使用 BEGIN IMMEDIATE 事务；数据库的
部分唯一索引兜底“一个学生同一轮一份有效预约”和“一个时段一份有效
预约”，即使高并发的多监护人抢约也不会产生两条有效记录。
"""
import sqlite3
from datetime import datetime

from . import auth
from .adapters import AttachmentStore, NotificationAdapter, VideoAdapter
from .errors import AppError, ConflictError, NotFoundError, PermissionError_

ACTIVE_BOOKING_STATUSES = ("requested", "confirmed", "reschedule_pending")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ConferenceService:
    def __init__(self, conn: sqlite3.Connection, video: VideoAdapter,
                 notifier: NotificationAdapter, files: AttachmentStore,
                 clock=_now):
        self.conn = conn
        self.video = video
        self.notifier = notifier
        self.files = files
        self.now = clock

    # ---------- 基础读取 ----------

    def _user(self, user_id: int):
        u = auth.get_user(self.conn, user_id)
        if u is None:
            raise NotFoundError("用户不存在")
        return u

    def _round(self, round_id: int):
        r = self.conn.execute(
            "SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
        if r is None:
            raise NotFoundError("家长会轮次不存在")
        return r

    def _slot(self, slot_id: int):
        s = self.conn.execute(
            "SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if s is None:
            raise NotFoundError("时段不存在")
        return s

    def _booking(self, booking_id: int):
        b = self.conn.execute(
            "SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if b is None:
            raise NotFoundError("预约不存在")
        return b

    def _student(self, student_id: int):
        s = self.conn.execute(
            "SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if s is None:
            raise NotFoundError("学生不存在")
        return s

    # ---------- 轮次与时段发布 ----------

    def publish_slots(self, user_id: int, round_id: int, slots: list[dict]):
        """教师发布时段：slots=[{start_ts,end_ts,note?}]。"""
        user = self._user(user_id)
        rnd = self._round(round_id)
        auth.require_teacher_of_class(self.conn, user, rnd["class_id"])
        ids = []
        for s in slots:
            if s["end_ts"] <= s["start_ts"]:
                raise AppError("时段结束时间必须晚于开始时间")
            try:
                cur = self.conn.execute(
                    "INSERT INTO slots(teacher_id, round_id, class_id,"
                    " start_ts, end_ts, note) VALUES (?,?,?,?,?,?)",
                    (user_id, round_id, rnd["class_id"],
                     s["start_ts"], s["end_ts"], s.get("note", "")),
                )
                ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                raise ConflictError(
                    f"教师在 {s['start_ts']} 已存在时段，无法重复发布")
        return ids

    def available_slots(self, round_id: int) -> list[dict]:
        """家长端：只展示可选（open）时段，附教师姓名。"""
        rows = self.conn.execute(
            """
            SELECT s.*, u.name AS teacher_name
            FROM slots s JOIN users u ON u.id = s.teacher_id
            WHERE s.round_id=? AND s.status='open'
            ORDER BY s.teacher_id, s.start_ts
            """, (round_id,)).fetchall()
        return [dict(r) for r in rows]

    def teacher_calendar(self, user_id: int, round_id: int) -> list[dict]:
        """教师端日历：按教师与时间排列，含预约与状态。管理员可查看。"""
        user = self._user(user_id)
        rnd = self._round(round_id)
        if "admin" not in auth.roles(user):
            auth.require_teacher_of_class(self.conn, user, rnd["class_id"])
        rows = self.conn.execute(
            """
            SELECT s.*, u.name AS teacher_name,
                   b.id AS booking_id, b.status AS booking_status,
                   b.mode, st.name AS student_name,
                   vm.url AS video_url, vm.active AS video_active
            FROM slots s
            JOIN users u ON u.id = s.teacher_id
            LEFT JOIN bookings b ON b.slot_id = s.id
                AND b.status NOT IN ('cancelled')
            LEFT JOIN students st ON st.id = b.student_id
            LEFT JOIN video_meetings vm ON vm.booking_id = b.id AND vm.active=1
            WHERE s.round_id=?
            ORDER BY s.teacher_id, s.start_ts
            """, (round_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 预约 ----------

    def request_booking(self, guardian_id: int, student_id: int,
                        round_id: int, mode: str, topics: str,
                        slot_id: int | None = None) -> dict:
        """家长提交预约：选线下/视频、填写事项，可指定意向时段。

        多监护人并发时靠唯一索引保证：同一学生一轮只保留一份有效预约、
        同一时段只被一份有效预约占用。
        """
        user = self._user(guardian_id)
        auth.require_guardian_of_student(self.conn, user, student_id)
        stu = self._student(student_id)
        rnd = self._round(round_id)
        if stu["class_id"] != rnd["class_id"]:
            raise AppError("学生不属于该家长会所在班级")
        if rnd["status"] != "open":
            raise AppError("本轮家长会预约窗口未开放")
        if not (rnd["open_from"] <= self.now() <= rnd["open_until"]):
            raise AppError("当前不在预约时间窗口内")
        if mode not in ("offline", "video"):
            raise AppError("会谈方式必须是 offline 或 video")
        if slot_id is not None:
            slot = self._slot(slot_id)
            if slot["round_id"] != round_id or slot["class_id"] != stu["class_id"]:
                raise AppError("时段与本轮家长会不匹配")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self.conn.execute(
                "SELECT 1 FROM bookings WHERE student_id=? AND round_id=? "
                "AND status NOT IN ('cancelled')",
                (student_id, round_id),
            ).fetchone()
            if exists:
                raise ConflictError("该学生在本轮家长会已有一份有效预约")
            if slot_id is not None:
                slot = self.conn.execute(
                    "SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
                if slot["status"] != "open":
                    raise ConflictError("该时段已被占用或已取消")
            cur = self.conn.execute(
                "INSERT INTO bookings(slot_id, round_id, student_id,"
                " guardian_id, mode, topics, status, created_at) "
                "VALUES (?,?,?,?,?,?,'requested',?)",
                (slot_id, round_id, student_id, guardian_id,
                 mode, topics, self.now()),
            )
            booking_id = cur.lastrowid
            if slot_id is not None:
                self.conn.execute(
                    "UPDATE slots SET status='booked' WHERE id=?", (slot_id,))
            video_url = None
            if mode == "video":
                video_url = self.video.create_meeting(booking_id, self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        for tid in auth.class_teacher_ids(self.conn, rnd["class_id"]):
            self.notifier.send(
                tid, "booking_requested",
                f"{stu['name']} 的监护人申请{mode == 'video' and '视频' or '线下'}"
                f"会谈，请确认。", self.now())
        self.notifier.send(
            guardian_id, "booking_submitted", "预约申请已提交，等待教师确认。",
            self.now())
        return {"booking_id": booking_id, "video_url": video_url}

    def confirm_booking(self, teacher_id: int, booking_id: int,
                        slot_id: int | None = None) -> dict:
        """教师确认预约；可为未指定时段的申请直接分配（生成）时段。"""
        teacher = self._user(teacher_id)
        booking = self._booking(booking_id)
        rnd = self._round(booking["round_id"])
        auth.require_teacher_of_class(self.conn, teacher, rnd["class_id"])
        if booking["status"] != "requested":
            raise ConflictError("只有待确认的预约可以确认")

        target = slot_id or booking["slot_id"]
        if target is None:
            raise AppError("请先为该预约分配一个时段")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            slot = self.conn.execute(
                "SELECT * FROM slots WHERE id=?", (target,)).fetchone()
            if slot is None:
                raise NotFoundError("时段不存在")
            if slot["round_id"] != booking["round_id"]:
                raise AppError("时段与本轮家长会不匹配")
            if slot["status"] != "open" and slot["id"] != booking["slot_id"]:
                raise ConflictError("目标时段已被占用")
            self.conn.execute(
                "UPDATE slots SET status='booked' WHERE id=? AND status='open'",
                (target,))
            self.conn.execute(
                "UPDATE bookings SET slot_id=?, status='confirmed',"
                " confirmed_at=? WHERE id=?",
                (target, self.now(), booking_id))
            video_url = self.video.current_url(booking_id)
            if booking["mode"] == "video" and video_url is None:
                video_url = self.video.create_meeting(booking_id, self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        guardians = auth.active_guardian_ids(self.conn, booking["student_id"])
        self.notifier.send_many(
            guardians, "booking_confirmed",
            f"会谈已确认，时段：{slot['start_ts']}。", self.now())
        return {"slot_id": target, "video_url": video_url}

    def reject_request(self, teacher_id: int, booking_id: int,
                       reason: str = "") -> None:
        """教师驳回尚未确认的申请，释放暂占时段。"""
        teacher = self._user(teacher_id)
        booking = self._booking(booking_id)
        rnd = self._round(booking["round_id"])
        auth.require_teacher_of_class(self.conn, teacher, rnd["class_id"])
        if booking["status"] != "requested":
            raise ConflictError("只能驳回待确认的申请")
        self._cancel_booking(booking, teacher_id, reason=reason or "教师驳回申请")

    def cancel_by_guardian(self, guardian_id: int, booking_id: int,
                           reason: str = "") -> None:
        """监护人主动取消预约。"""
        user = self._user(guardian_id)
        booking = self._booking(booking_id)
        auth.require_guardian_of_student(self.conn, user, booking["student_id"])
        if booking["status"] not in ACTIVE_BOOKING_STATUSES:
            raise ConflictError("该预约已结束，无法取消")
        self._cancel_booking(booking, guardian_id, reason=reason or "监护人取消")

    def _cancel_booking(self, booking, actor_id: int, reason: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE bookings SET status='cancelled', cancelled_at=? "
                "WHERE id=?", (self.now(), booking["id"]))
            if booking["slot_id"]:
                self.conn.execute(
                    "UPDATE slots SET status='open' WHERE id=? AND status='booked'",
                    (booking["slot_id"],))
            self.video.invalidate(booking["id"])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        rnd = self._round(booking["round_id"])
        self.notifier.send_many(
            auth.class_teacher_ids(self.conn, rnd["class_id"]),
            "booking_cancelled", f"预约已取消（{reason}）。", self.now())
        self.notifier.send(
            booking["guardian_id"], "booking_cancelled",
            f"您的预约已取消（{reason}）。", self.now())

    # ---------- 改期 ----------

    def request_reschedule(self, guardian_id: int, booking_id: int,
                           reason: str, new_slot_id: int | None = None) -> int:
        """家长临时改期：进入改期队列，等待教师处理。"""
        user = self._user(guardian_id)
        booking = self._booking(booking_id)
        auth.require_guardian_of_student(self.conn, user, booking["student_id"])
        if booking["status"] not in ("confirmed", "reschedule_pending"):
            raise ConflictError("只有已确认的会谈可以申请改期")
        if new_slot_id is not None:
            slot = self._slot(new_slot_id)
            if slot["round_id"] != booking["round_id"] or slot["status"] != "open":
                raise ConflictError("目标时段不可选")
        rid = self._insert_reschedule(
            booking, "guardian", reason, booking["slot_id"],
            new_slot_id, guardian_id)
        rnd = self._round(booking["round_id"])
        self.notifier.send_many(
            auth.class_teacher_ids(self.conn, rnd["class_id"]),
            "reschedule_requested",
            f"{self._student(booking['student_id'])['name']} 的监护人申请改期，"
            "请前往改期队列处理。", self.now())
        return rid

    def _insert_reschedule(self, booking, kind, reason, old_slot_id,
                           new_slot_id, requested_by) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO reschedules(booking_id, kind, reason, old_slot_id,"
                " new_slot_id, requested_by, state, created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (booking["id"], kind, reason, old_slot_id,
                 new_slot_id, requested_by, self.now()),
            )
            self.conn.execute(
                "UPDATE bookings SET status='reschedule_pending' WHERE id=?",
                (booking["id"],))
            rid = cur.lastrowid
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return rid

    def propose_new_slot(self, guardian_id: int, reschedule_id: int,
                         new_slot_id: int) -> None:
        """家长在改期队列中挑选新的意向时段（含教师请假后的重排）。"""
        user = self._user(guardian_id)
        rq = self._reschedule(reschedule_id)
        booking = self._booking(rq["booking_id"])
        auth.require_guardian_of_student(self.conn, user, booking["student_id"])
        if rq["state"] != "pending":
            raise ConflictError("该改期请求已处理")
        slot = self._slot(new_slot_id)
        if slot["round_id"] != booking["round_id"] or slot["status"] != "open":
            raise ConflictError("目标时段不可选")
        self.conn.execute(
            "UPDATE reschedules SET new_slot_id=? WHERE id=?",
            (new_slot_id, reschedule_id))

    def list_reschedules(self, user_id: int, round_id: int,
                         state: str = "pending") -> list[dict]:
        """改期队列（教师视图，可看本班全部；监护人只看自己孩子的）。"""
        user = self._user(user_id)
        rnd = self._round(round_id)
        is_teacher = "teacher" in auth.roles(user) and auth.teaches(
            self.conn, user_id, rnd["class_id"])
        sql = [
            """SELECT rq.*, b.student_id, b.round_id, b.mode,
                      st.name AS student_name,
                      os.start_ts AS old_start, ns.start_ts AS new_start
               FROM reschedules rq
               JOIN bookings b ON b.id = rq.booking_id
               JOIN students st ON st.id = b.student_id
               LEFT JOIN slots os ON os.id = rq.old_slot_id
               LEFT JOIN slots ns ON ns.id = rq.new_slot_id
               WHERE b.round_id=? AND rq.state=?"""]
        args = [round_id, state]
        if not is_teacher:
            sql.append(
                "AND b.student_id IN (SELECT student_id FROM guardianships "
                "WHERE user_id=? AND active=1)")
            args.append(user_id)
        sql.append("ORDER BY rq.created_at")
        return [dict(r) for r in self.conn.execute(" ".join(sql), args)]

    def _reschedule(self, rid: int):
        rq = self.conn.execute(
            "SELECT * FROM reschedules WHERE id=?", (rid,)).fetchone()
        if rq is None:
            raise NotFoundError("改期请求不存在")
        return rq

    def approve_reschedule(self, teacher_id: int, reschedule_id: int,
                           new_slot_id: int | None = None) -> dict:
        """教师批准改期：原子交换时段，视频会谈换新房间链接。"""
        teacher = self._user(teacher_id)
        rq = self._reschedule(reschedule_id)
        booking = self._booking(rq["booking_id"])
        rnd = self._round(booking["round_id"])
        auth.require_teacher_of_class(self.conn, teacher, rnd["class_id"])
        if rq["state"] != "pending":
            raise ConflictError("该改期请求已处理")
        target = new_slot_id or rq["new_slot_id"]
        if target is None:
            raise AppError("请指定改期后的新时段")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            new_slot = self.conn.execute(
                "SELECT * FROM slots WHERE id=?", (target,)).fetchone()
            if new_slot is None:
                raise NotFoundError("新时段不存在")
            if new_slot["round_id"] != booking["round_id"]:
                raise AppError("时段与本轮家长会不匹配")
            if new_slot["status"] != "open":
                raise ConflictError("新时段已被占用")
            # 释放旧时段（教师请假导致旧时段已 cancelled 的不动它）
            self.conn.execute(
                "UPDATE slots SET status='open' WHERE id=? AND status='booked'",
                (rq["old_slot_id"],))
            self.conn.execute(
                "UPDATE slots SET status='booked' WHERE id=?", (target,))
            self.conn.execute(
                "UPDATE bookings SET slot_id=?, status='confirmed' WHERE id=?",
                (target, booking["id"]))
            self.conn.execute(
                "UPDATE reschedules SET state='approved', new_slot_id=?,"
                " decided_at=? WHERE id=?",
                (target, self.now(), reschedule_id))
            video_url = None
            if booking["mode"] == "video":
                video_url = self.video.refresh(booking["id"], self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        guardians = auth.active_guardian_ids(self.conn, booking["student_id"])
        self.notifier.send_many(
            guardians, "reschedule_approved",
            f"改期已通过，新时段：{new_slot['start_ts']}。", self.now())
        return {"new_slot_id": target, "video_url": video_url}

    def reject_reschedule(self, teacher_id: int, reschedule_id: int,
                          reason: str = "") -> None:
        """教师驳回改期：保留原时段，预约恢复 confirmed。"""
        teacher = self._user(teacher_id)
        rq = self._reschedule(reschedule_id)
        booking = self._booking(rq["booking_id"])
        rnd = self._round(booking["round_id"])
        auth.require_teacher_of_class(self.conn, teacher, rnd["class_id"])
        if rq["state"] != "pending":
            raise ConflictError("该改期请求已处理")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # 教师请假的旧时段已取消，驳回无意义；只能另选时段
            if rq["kind"] == "teacher_leave":
                raise AppError("教师已请假，原时段取消，请改选其他时段")
            self.conn.execute(
                "UPDATE reschedules SET state='rejected', decided_at=? "
                "WHERE id=?", (self.now(), reschedule_id))
            self.conn.execute(
                "UPDATE bookings SET status='confirmed' WHERE id=?",
                (booking["id"],))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.notifier.send(
            booking["guardian_id"], "reschedule_rejected",
            f"改期申请未通过（{reason or '无'}），按原时段进行。", self.now())

    # ---------- 教师请假 ----------

    def teacher_leave(self, actor_id: int, teacher_id: int, round_id: int,
                      reason: str) -> dict:
        """教师请假：取消其本轮未来时段，受影响预约进入改期队列。

        操作者须为管理员或本班任课教师。
        """
        actor = self._user(actor_id)
        rnd = self._round(round_id)
        if "admin" not in auth.roles(actor):
            auth.require_teacher_of_class(self.conn, actor, rnd["class_id"])
        absent = self._user(teacher_id)
        now = self.now()

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            slots = self.conn.execute(
                "SELECT * FROM slots WHERE teacher_id=? AND round_id=? "
                "AND status != 'cancelled' AND start_ts > ?",
                (teacher_id, round_id, now),
            ).fetchall()
            affected = []
            for slot in slots:
                self.conn.execute(
                    "UPDATE slots SET status='cancelled', "
                    "note=COALESCE(note,'')||? WHERE id=?",
                    (f" 教师请假：{reason}", slot["id"]))
                bks = self.conn.execute(
                    "SELECT * FROM bookings WHERE slot_id=? "
                    "AND status NOT IN ('cancelled')",
                    (slot["id"],)).fetchall()
                for b in bks:
                    cur = self.conn.execute(
                        "INSERT INTO reschedules(booking_id, kind, reason,"
                        " old_slot_id, new_slot_id, requested_by, state,"
                        " created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                        (b["id"], "teacher_leave",
                         f"教师请假：{reason}", slot["id"], None,
                         actor_id, now),
                    )
                    self.conn.execute(
                        "UPDATE bookings SET status='reschedule_pending' "
                        "WHERE id=?", (b["id"],))
                    self.video.invalidate(b["id"])
                    affected.append({"booking_id": b["id"],
                                     "reschedule_id": cur.lastrowid,
                                     "student_id": b["student_id"]})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        for item in affected:
            self.notifier.send_many(
                auth.active_guardian_ids(self.conn, item["student_id"]),
                "teacher_leave",
                f"{absent['name']} 老师请假，原时段取消，请在改期队列中"
                "选择新时段。", now)
        return {"cancelled_slots": len(slots),
                "affected_bookings": affected}

    # ---------- 视频入口 ----------

    def video_entry(self, user_id: int, booking_id: int) -> dict:
        """获取视频入口；链接失效时返回 active=False，提示刷新。"""
        user = self._user(user_id)
        b = self._booking(booking_id)
        if not auth.can_access_student(self.conn, user_id, b["student_id"]):
            raise PermissionError_("无权查看该会谈的视频入口")
        if b["mode"] != "video":
            raise AppError("该会谈为线下会谈")
        url = self.video.current_url(booking_id)
        return {"active": url is not None, "url": url}

    def refresh_video(self, user_id: int, booking_id: int) -> str:
        """视频链接失效后由教师或监护人重新生成，并通知对方。"""
        user = self._user(user_id)
        b = self._booking(booking_id)
        rnd = self._round(b["round_id"])
        is_teacher = "teacher" in auth.roles(user) and auth.teaches(
            self.conn, user_id, rnd["class_id"])
        is_guardian = auth.guardianship_active(
            self.conn, user_id, b["student_id"])
        if not (is_teacher or is_guardian):
            raise PermissionError_("只有会谈双方可以刷新视频链接")
        if self.video.current_url(booking_id):
            raise ConflictError("当前视频链接仍有效，无需刷新")
        url = self.video.refresh(booking_id, self.now())
        if is_teacher:
            targets = auth.active_guardian_ids(self.conn, b["student_id"])
        else:
            targets = auth.class_teacher_ids(self.conn, rnd["class_id"])
        self.notifier.send_many(
            targets, "video_refreshed",
            f"视频链接已更新：{url}", self.now())
        return url

    # ---------- 纪要与后续待办 ----------

    def complete_meeting(self, teacher_id: int, booking_id: int, summary: str,
                         overtime: bool = False,
                         action_items: list[dict] | None = None,
                         attachments: list[str] | None = None) -> int:
        """会谈结束：教师记录结论与后续事项（每项必须有负责人和期限）。"""
        teacher = self._user(teacher_id)
        booking = self._booking(booking_id)
        rnd = self._round(booking["round_id"])
        auth.require_teacher_of_class(self.conn, teacher, rnd["class_id"])
        if booking["status"] != "confirmed":
            raise ConflictError("只有已确认的会谈可以记录纪要")
        action_items = action_items or []
        for item in action_items:
            if not item.get("content") or not item.get("owner_id") \
                    or not item.get("due_date"):
                raise AppError("每条后续事项都必须写明内容、负责人与期限")
            owner = self._user(item["owner_id"])
            item.setdefault("owner_label", owner["name"])

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO minutes(booking_id, student_id, class_id,"
                " summary, overtime, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (booking_id, booking["student_id"], rnd["class_id"],
                 summary, 1 if overtime else 0, teacher_id, self.now()),
            )
            minutes_id = cur.lastrowid
            for item in action_items:
                self.conn.execute(
                    "INSERT INTO action_items(minutes_id, content, owner_id,"
                    " owner_label, due_date) VALUES (?,?,?,?,?)",
                    (minutes_id, item["content"], item["owner_id"],
                     item["owner_label"], item["due_date"]),
                )
            self.conn.execute(
                "UPDATE bookings SET status='completed' WHERE id=?",
                (booking_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        for fname in attachments or []:
            self.files.add(minutes_id, fname)
        guardians = auth.active_guardian_ids(self.conn, booking["student_id"])
        self.notifier.send_many(
            guardians, "minutes_ready",
            "会谈纪要与后续待办已生成，请查看。", self.now())
        return minutes_id

    def _minutes_row(self, minutes_id: int):
        m = self.conn.execute(
            "SELECT * FROM minutes WHERE id=?", (minutes_id,)).fetchone()
        if m is None:
            raise NotFoundError("纪要不存在")
        return m

    def view_minutes(self, user_id: int, minutes_id: int) -> dict:
        """查看纪要：本班任课教师或在权监护人；所有尝试写入访问记录。"""
        user = self._user(user_id)
        m = self._minutes_row(minutes_id)
        ok = auth.can_access_student(self.conn, user_id, m["student_id"])
        auth.log_access(self.conn, user_id, "view_minutes", ok,
                        self.now(), minutes_id)
        if not ok:
            raise PermissionError_("无权查看该学生的会谈纪要")
        items = self.conn.execute(
            "SELECT * FROM action_items WHERE minutes_id=? ORDER BY due_date",
            (minutes_id,)).fetchall()
        atts = self.conn.execute(
            "SELECT id, filename FROM attachments WHERE minutes_id=?",
            (minutes_id,)).fetchall()
        today = self.now()[:10]
        result = dict(m)
        result["action_items"] = [
            {**dict(i), "overdue": i["state"] == "open" and i["due_date"] < today}
            for i in items]
        result["attachments"] = [dict(a) for a in atts]
        return result

    def edit_minutes(self, user_id: int, minutes_id: int, summary: str) -> None:
        """修改会谈结论：仅本班任课教师。

        即使用户同时拥有家长身份（例如别的班的老师、本班孩子的家长），
        不任教本班就不能修改——无法以普通家长身份改结论。
        """
        user = self._user(user_id)
        m = self._minutes_row(minutes_id)
        auth.require_teacher_of_class(self.conn, user, m["class_id"])
        self.conn.execute(
            "UPDATE minutes SET summary=?, updated_at=? WHERE id=?",
            (summary, self.now(), minutes_id))

    def view_attachment(self, user_id: int, attachment_id: int) -> dict:
        user = self._user(user_id)
        row = self.files.read(attachment_id)
        if row is None:
            raise NotFoundError("附件不存在")
        m = self._minutes_row(row["minutes_id"])
        ok = auth.can_access_student(self.conn, user_id, m["student_id"])
        auth.log_access(self.conn, user_id, "view_attachment", ok,
                        self.now(), m["id"])
        if not ok:
            raise PermissionError_("无权查看该附件")
        return dict(row)

    def mark_action_item(self, user_id: int, item_id: int,
                         done: bool) -> None:
        """完成/重开后续待办：本班教师或负责人本人（须仍为在权监护人）。"""
        user = self._user(user_id)
        item = self.conn.execute(
            "SELECT * FROM action_items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise NotFoundError("待办不存在")
        m = self._minutes_row(item["minutes_id"])
        is_teacher = "teacher" in auth.roles(user) and auth.teaches(
            self.conn, user_id, m["class_id"])
        is_owner = item["owner_id"] == user_id and (
            is_teacher or auth.guardianship_active(
                self.conn, user_id, m["student_id"]))
        if not (is_teacher or is_owner):
            raise PermissionError_("只有负责教师或待办负责人可以更新状态")
        self.conn.execute(
            "UPDATE action_items SET state=?, completed_at=? WHERE id=?",
            ("done" if done else "open",
             self.now() if done else None, item_id))

    def my_bookings(self, guardian_id: int, round_id: int) -> list[dict]:
        """家长端：自己孩子的预约与自己的纪要（不含他人信息）。"""
        user = self._user(guardian_id)
        rows = self.conn.execute(
            """
            SELECT b.*, st.name AS student_name, sl.start_ts, sl.end_ts,
                   tu.name AS teacher_name,
                   m.id AS minutes_id,
                   vm.url AS video_url, vm.active AS video_active
            FROM bookings b
            JOIN students st ON st.id = b.student_id
            LEFT JOIN slots sl ON sl.id = b.slot_id
            LEFT JOIN users tu ON tu.id = sl.teacher_id
            LEFT JOIN minutes m ON m.booking_id = b.id
            LEFT JOIN video_meetings vm ON vm.booking_id = b.id AND vm.active=1
            WHERE b.round_id=? AND b.student_id IN
                  (SELECT student_id FROM guardianships
                   WHERE user_id=? AND active=1)
            ORDER BY b.created_at
            """, (round_id, guardian_id)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 授权管理 ----------

    def revoke_guardianship(self, actor_id: int, student_id: int,
                            guardian_user_id: int) -> None:
        """撤销监护授权：旧账号立刻失去访问权，进行中预约转由其他监护人。"""
        actor = self._user(actor_id)
        stu = self._student(student_id)
        if "admin" not in auth.roles(actor):
            auth.require_teacher_of_class(self.conn, actor, stu["class_id"])
        cur = self.conn.execute(
            "UPDATE guardianships SET active=0 "
            "WHERE student_id=? AND user_id=?",
            (student_id, guardian_user_id))
        if cur.rowcount == 0:
            raise NotFoundError("监护关系不存在")
        auth.log_access(self.conn, guardian_user_id, "guardianship_revoked",
                        False, self.now())

    def access_logs(self, user_id: int, limit: int = 100) -> list[dict]:
        """访问记录（管理员或本班教师可查）。"""
        user = self._user(user_id)
        if "admin" not in auth.roles(user) and not self.conn.execute(
                "SELECT 1 FROM teachers WHERE user_id=?",
                (user_id,)).fetchone():
            raise PermissionError_("只有教师或管理员可以查看访问记录")
        rows = self.conn.execute(
            """SELECT l.*, u.name AS user_name FROM access_logs l
               JOIN users u ON u.id=l.user_id
               ORDER BY l.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]
