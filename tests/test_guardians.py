"""多监护人规则测试。"""
from tests.base import AppCase
from app.services import ServiceError


class MultiGuardianTest(AppCase):
    def setUp(self):
        super().setUp()
        self.s1 = self.slot(self.t_wang, "2026-10-09 18:00", "2026-10-09 18:20")
        self.s2 = self.slot(self.t_wang, "2026-10-09 18:20", "2026-10-09 18:40")
        self.s3 = self.slot(self.t_wang, "2026-10-09 18:40", "2026-10-09 19:00")

    def test_only_one_active_booking_per_student_per_round(self):
        # 父亲约 s1，母亲不能再用另一时段为同一学生预约
        self.book(self.dad, self.s1)
        with self.assertRaises(ServiceError) as cm:
            self.book(self.mom, self.s2)
        self.assertEqual(cm.exception.code, "duplicate_booking")

    def test_two_guardians_same_slot_second_loses(self):
        self.book(self.dad, self.s1)
        with self.assertRaises(ServiceError):
            self.book(self.mom, self.s1)

    def test_cancel_then_other_guardian_can_book(self):
        bid = self.book(self.dad, self.s1)
        self.svc.cancel_booking(self.dad, bid, reason="临时改期")
        # 旧预约取消后，母亲可以新约
        new_bid = self.book(self.mom, self.s1)
        active = self.svc.active_booking_for_student(self.sid, self.rid)
        self.assertEqual(active["id"], new_bid)

    def test_non_guardian_cannot_book(self):
        # 乙妈妈试图为甲预约
        with self.assertRaises(ServiceError) as cm:
            self.book(self.other_parent, self.s2, student_id=self.sid)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_only_booking_owner_can_request_reschedule(self):
        bid = self.book(self.dad, self.s1)
        self.svc.confirm_booking(self.wang, bid)
        # 母亲是监护人但不是预约发起人，不能申请改期
        with self.assertRaises(ServiceError) as cm:
            self.svc.guardian_request_reschedule(self.mom, bid, self.s2, "想换时间")
        self.assertEqual(cm.exception.code, "forbidden")
        # 发起人可以
        qid = self.svc.guardian_request_reschedule(self.dad, bid, self.s2, "临时有事")
        self.assertTrue(qid)

    def test_reschedule_keeps_single_active_booking(self):
        bid = self.book(self.dad, self.s1)
        self.svc.confirm_booking(self.wang, bid)
        qid = self.svc.guardian_request_reschedule(self.dad, bid, self.s2)
        # 改期申请中，母亲依旧不能插空预约（原预约仍占唯一有效名额）
        with self.assertRaises(ServiceError) as cm:
            self.book(self.mom, self.s3)
        self.assertEqual(cm.exception.code, "duplicate_booking")
        # 通过后移动到 s2，s1 释放
        self.svc.approve_reschedule(self.wang, qid)
        b = self.svc.get_booking(bid)
        self.assertEqual(b["slot_id"], self.s2)
        self.assertEqual(b["status"], "confirmed")
        self.assertEqual(
            self.conn.execute("SELECT status FROM time_slots WHERE id=?",
                              (self.s1,)).fetchone()["status"], "open")
        self.assertEqual(
            self.conn.execute("SELECT status FROM time_slots WHERE id=?",
                              (self.s2,)).fetchone()["status"], "held")
        # 仍然只有一份有效预约
        actives = self.conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE student_id=? AND round_id=? "
            "AND status IN ('pending','confirmed','reschedule_requested','teacher_reschedule')",
            (self.sid, self.rid)).fetchone()["c"]
        self.assertEqual(actives, 1)

    def test_reject_reschedule_restores_confirmed(self):
        bid = self.book(self.dad, self.s1)
        self.svc.confirm_booking(self.wang, bid)
        qid = self.svc.guardian_request_reschedule(self.dad, bid, self.s2)
        self.svc.reject_reschedule(self.wang, qid, "该时段不合适")
        self.assertEqual(self.svc.get_booking(bid)["status"], "confirmed")
