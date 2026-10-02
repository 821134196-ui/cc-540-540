"""时段冲突测试：教师时间重叠、同时段抢占、窗口时间。"""
from tests.base import AppCase
from app.services import ServiceError


class SlotConflictTest(AppCase):
    def test_overlapping_slots_same_teacher_rejected(self):
        self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        # 部分重叠
        with self.assertRaises(ServiceError) as cm:
            self.slot(self.t_wang, "2026-10-09 18:10", "2026-10-09 18:30")
        self.assertEqual(cm.exception.code, "slot_conflict")
        # 包含关系
        with self.assertRaises(ServiceError):
            self.slot(self.t_wang, "2026-10-09 18:05", "2026-10-09 18:15")
        # 结束早于开始
        with self.assertRaises(ServiceError):
            self.slot(self.t_wang, "2026-10-09 19:00", "2026-10-09 18:00")

    def test_adjacent_slots_are_allowed(self):
        # 首尾相接（18:20 = 18:20）不算重叠
        a = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        b = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")
        self.assertNotEqual(a, b)

    def test_same_time_different_teachers_allowed(self):
        self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        self.slot(self.t_li, "2026-10-09 18:00", "2026-10-09 18:20")  # 不报错

    def test_two_bookings_on_same_slot_race(self):
        s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        self.book(self.dad, s)
        # 另一位监护人（哪怕是同一学生）也不能抢同一时段
        with self.assertRaises(ServiceError) as cm:
            self.book(self.mom, s)
        # 同一学生先触发“一轮一份”或“时段被占”，二者都必须是拒绝
        self.assertIn(cm.exception.code, ("slot_taken", "duplicate_booking"))
        # 不同学生的监护人抢同一时段 -> 时段被占
        s2 = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")
        ob = self.book(self.other_parent, s2, student_id=self.sid2)
        with self.assertRaises(ServiceError):
            self.book(self.dad, s2, student_id=self.sid)
        # 双方都取消后时段释放，母亲可再约
        self.svc.cancel_booking(self.other_parent, ob)
        db = self.svc.bookings_for_guardian(self.dad)[0]
        self.svc.cancel_booking(self.dad, db["id"])
        self.book(self.mom, s2)  # 不报错

    def test_held_slot_not_open_for_guardian(self):
        s = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        bid = self.book(self.dad, s)
        self.svc.confirm_booking(self.wang, bid)
        self.assertEqual(
            self.conn.execute("SELECT status FROM time_slots WHERE id=?", (s,)).fetchone()["status"],
            "held")
        self.assertNotIn(s, [r["id"] for r in self.svc.open_slots(self.rid)])

    def test_window_closed(self):
        s = self.slot(self.t_wang, "2026-11-05 18:00", "2026-11-05 18:20")
        with self.assertRaises(ServiceError) as cm:
            self.book(self.dad, s, now="2026-11-06 09:00")
        self.assertEqual(cm.exception.code, "window_closed")

    def test_non_homeroom_teacher_can_still_manage_class_slots(self):
        # 本班数学老师也是任课教师，可以确认预约
        s = self.slot(self.t_li, "2026-10-09 18:00", "2026-10-09 18:20")
        bid = self.book(self.dad, s)
        self.svc.confirm_booking(self.li, bid)  # 不报错
